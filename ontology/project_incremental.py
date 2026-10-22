"""Incremental projection by generation.

``ontology.project.build_store`` projects every capture into a fresh generation each
time. :func:`build_generation` keeps generations in a state directory the caller
passes in and rebuilds only what changed: the projection is cut into **units**
(``ontology.project.binding_unit``: one binding's objects, versions, values,
identifiers and events; ``link_unit``: one link binding's links), each with the
bindings whose captures it depends on (:func:`plan`, from the schema alone). A unit's
key is a digest of the capture fingerprints of those bindings, the schema digest,
``as_known_at``, the normalizer and the code of the projector. A unit whose key is
unchanged is reused from the last generation (rows and build-report part); the rest
are projected again by a partial :class:`ontology.project.Projector` that reads only
the bindings they depend on and writes only their rows into a byte copy of the last
generation. The result is the generation a cold build of the same captures gives
(same rows, links, citations, markings, tiers and build report, so the same content
digest): an unchanged unit's rows are, by its key, the rows a cold build writes.

Dependencies (bindings whose captures a unit's rows can read):

* ``lin(B)``: B; the ends of every link type B's lineage rules hop through
  (``via_link``) and of its ``inherit`` link type, plus ``lin`` of those ends (an
  inherited lineage is the target's lineage); for a binding that ``stands_for`` another,
  that one and the bindings listed before B standing for it (they decide what B holds).
* ``val(B)``: ``lin(B)`` plus the ends of the link types its units/currencies/tax bases
  are read through, and the ``local_currency`` binding of each record holding a currency
  it reads (a blank code reads the local currency there). ``b:B`` = ``val(B)`` plus the ends of the link types its events' qualifiers
  follow (capture events compare B's own values).
* A link unit: its ends (from, to and join binding), ``lin`` of each end (citations
  carry the cited version's lineage) and, for ``same_record``, ``val`` of each end
  (property conflicts compare their values).

A schema whose lineage inheritance is cyclic (a binding inherits, through any chain,
from itself) has an order-dependent lineage, so it is never built incrementally
(every build is cold; ``Generation.mode`` says so).

State directory (the caller owns where it is; this module owns what is inside)::

    <state_dir>/CURRENT                      JSON: the committed generation (atomic)
    <state_dir>/.build.lock                  flock held by the one running build
    <state_dir>/generations/<id>/store.sqlite   the generation (ProjectionStore reads it)
    <state_dir>/generations/<id>/units.json     unit keys and build-report parts

A build writes a new generation directory and commits by replacing ``CURRENT`` (which
records when, ``committed_at``); until then the previous generation is untouched and stays
current. A build that is interrupted (an exception, a ``checkpoint`` that raises, a killed
process) leaves ``CURRENT`` and its generation as they were; the next build removes the
unfinished directory and builds again from the last committed generation. The previous
generation stays on disk after a commit (``KEEP``) so a reader holding it open keeps
reading it; :func:`prune_to_current` removes it from a directory kept between runs
(``operating.warm_store``), where no reader holds one.

Reuse (``build_generation(..., previous=<a generation of another state directory>,
reuse=True)``): when every unit key of the captures equals ``previous``'s, ``previous`` is
returned as it is (``mode`` ``reused``): nothing is projected, copied or written, so its
rows are, by the keys, the rows a build of these captures writes. Its ``post_project``
rows are ``previous``'s: a caller reuses only a generation whose ``post_project`` inputs
(the review and confirmation stores of the state directory it was built over) are its own.
``current_generation(..., committed_since=)`` returns a generation only when it was
committed at or after that instant (a verifier session's start: one session's projection,
never an earlier one's).

Memory: captures are fingerprinted and projected as streams (the projector's bound,
``ontology/project.py``); only unit keys and report parts are held.

Stdlib only (``fcntl``: POSIX).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping

from ontology.project import (_PROJECTED_LINK_KINDS, _REC_FIELDS, DEFAULT_BATCH, POST_PROJECT,
                              CaptureSource, Normalizer, ProjectionError, Projector,
                              _default_normalizer, _get,
                              binding_unit, event_id_of, finish_generation, link_ends,
                              link_unit, merge_reports, open_generation)
from ontology import store_ddl
from ontology.store import StoreBuilder, _fsync_dir, canonical, known_at

FORMAT = "plx-projection-generations/v1"
CURRENT = "CURRENT"
KEEP = 2
_LOCK = ".build.lock"
# Modules whose code decides what a unit holds: a change to any of them changes every key.
_CODE = ("ontology.project", "ontology.project_incremental", "ontology.store",
         "ontology.store_ddl", "ontology.units", "ontology.schema", "ontology.identifiers",
         "integrations.source_mirror")
_EMPTY = {"counts": {}, "gaps": []}


@dataclass(frozen=True)
class Generation:
    """One committed generation. ``store`` is the file ``ProjectionStore`` reads."""

    id: str
    store: Path
    content_digest: str
    report: dict[str, Any] = field(repr=False, default_factory=dict)
    mode: str = "cold"              # cold | incremental | reused (or cold_cyclic_lineage)
    units: int = 0
    units_rebuilt: int = 0
    bindings_read: int = 0
    seconds: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- plan

@dataclass(frozen=True)
class Plan:
    """Units of one schema: ``units[unit]`` = the bindings its rows depend on;
    ``rows[unit]`` = where its rows are (``("binding", id)``, ``("links", link type,
    from binding, to binding, kind)`` or ``("gaps",)``)."""

    units: dict[str, frozenset[str]]
    rows: dict[str, tuple]
    bindings: dict[str, tuple[str, Mapping[str, Any]]]
    acyclic: bool


class _Cycle(Exception):
    pass


def plan(schema: Any) -> Plan:
    """Units and their dependencies, from the schema alone (module docstring)."""
    types = dict(_get(schema, "object_types") or {})
    links = dict(_get(schema, "link_types") or {})
    events = dict(_get(schema, "event_types") or {})
    bindings = {b["id"]: (tid, b) for tid, t in types.items() for b in t.get("bindings", [])}

    def ends(link_id: str | None) -> set[str]:
        out: set[str] = set()
        for lb in (links.get(link_id) or {}).get("bindings", []):
            out |= link_ends(lb, bindings)
        return out

    def targets(link_id: str, bid: str) -> set[str]:
        """Bindings a hop from ``bid``'s versions over ``link_id`` can reach."""
        return {lb["to_binding"] for lb in (links.get(link_id) or {}).get("bindings", [])
                if lb.get("from_binding") == bid and lb.get("to_binding") in bindings}

    lin_memo: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def lin(bid: str) -> frozenset[str]:
        if bid in lin_memo:
            return lin_memo[bid]
        if bid in visiting:
            raise _Cycle(bid)
        visiting.add(bid)
        spec = bindings[bid][1].get("lineage") or {}
        out = {bid}
        for rule in spec.get("rules", []):
            for when in rule.get("when", []):
                if "via_link" in when:
                    out |= ends(when["via_link"])
        if spec.get("inherit"):
            out |= ends(spec["inherit"])
            for target in targets(spec["inherit"], bid):
                out |= lin(target)   # B inheriting from B is a cycle too
        stood = (bindings[bid][1].get("stands_for") or {}).get("binding")
        if stood:   # which records B holds depends on what that one (and earlier) hold
            out.add(stood)
            for other in types[bindings[bid][0]].get("bindings", []):
                if other["id"] == bid:
                    break
                if (other.get("stands_for") or {}).get("binding") == stood:
                    out.add(other["id"])
        visiting.discard(bid)
        lin_memo[bid] = frozenset(out)
        return lin_memo[bid]

    def val(bid: str) -> frozenset[str]:
        tid = bindings[bid][0]
        out = set(lin(bid))
        for prop in types[tid].get("properties", []):
            for source in prop["sources"]:
                if source["binding"] != bid:
                    continue
                for spec in (source, prop):
                    for what in ("unit", "currency", "per_unit", "tax_basis"):
                        ref = spec.get(what + "_from")
                        if isinstance(ref, Mapping):
                            out |= ends(ref.get("via_link"))
                        if what == "currency" and ref is not None:
                            # a blank code reads the local currency (local_currency)
                            holders = targets(ref["via_link"], bid) \
                                if isinstance(ref, Mapping) else {bid}
                            out |= {local["binding"] for h in holders
                                    if (local := bindings[h][1].get("local_currency"))}
        return frozenset(out)

    units: dict[str, frozenset[str]] = {}
    rows: dict[str, tuple] = {}
    try:
        for bid, (tid, _) in sorted(bindings.items()):
            deps = set(val(bid))
            for ev in events.values():
                if ev.get("object_type") == tid:
                    for q in ev.get("qualifiers", []):
                        if q.get("via") != "self":
                            deps |= ends(q.get("via"))
            units[binding_unit(bid)] = frozenset(deps)
            rows[binding_unit(bid)] = ("binding", bid)
        for lid, link in sorted(links.items()):
            for index, lb in enumerate(link.get("bindings", [])):
                unit = link_unit(lid, index, lb, bindings)
                deps = set(units.get(unit, frozenset()))
                if unit.startswith(f"l:{lid}|"):
                    for end in link_ends(lb, bindings):
                        deps |= {end} | lin(end)
                        if lb["kind"] == "same_record":
                            deps |= val(end)
                    rows[unit] = ("links", lid, lb["from_binding"], lb["to_binding"], lb["kind"])
                else:
                    rows[unit] = ("gaps",)
                units[unit] = frozenset(deps)
    except _Cycle:
        return Plan({}, {}, bindings, acyclic=False)
    return Plan(units, rows, bindings, acyclic=True)


# --------------------------------------------------------------------------- fingerprints

class _Pinned:
    """A capture source whose ``records`` is the one snapshot read for its
    fingerprints, so the projector reads exactly what was fingerprinted."""

    def __init__(self, inner: CaptureSource, records: Mapping[str, Any]) -> None:
        self._inner = inner
        self._records = records
        self.source = getattr(inner, "source", None)

    def records(self, relations: Any) -> Mapping[str, Any]:
        return self._records

    def describe(self) -> dict[str, Any]:
        return self._inner.describe()

    def __getattr__(self, name: str) -> Any:   # documents() for the mention step
        return getattr(self._inner, name)


def _stream_fingerprint(records: Iterable[Any]) -> str:
    """Digest of every field of every record, except a current version's
    ``last_seen_at`` (``MirrorCaptures.fingerprint`` says why)."""
    digest = hashlib.sha256()
    for rec in records:
        cells = tuple(None if name == "last_seen_at" and rec.superseded_at is None
                      else getattr(rec, name) for name in _REC_FIELDS)
        digest.update(repr(cells).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _pin(sources: Mapping[str, CaptureSource], the_plan: Plan
         ) -> tuple[dict[str, Any], dict[str, str | None]]:
    """(pinned sources, binding -> fingerprint of its relation; None = not captured)."""
    relations: dict[str, set[str]] = {}
    for _, b in the_plan.bindings.values():
        if b["source"] in sources:
            relations.setdefault(b["source"], set()).add(b["relation"])
    pinned: dict[str, Any] = dict(sources)
    by_relation: dict[tuple[str, str], str] = {}
    for name, wanted in sorted(relations.items()):
        source = sources[name]
        records = source.records(sorted(wanted))
        own = getattr(source, "fingerprint", None)
        if callable(own):
            prints = own(sorted(wanted))
        else:
            prints = {relation: _stream_fingerprint(records[relation])
                      for relation in sorted(wanted) if relation in records}
        # What a source says it does not hold of a relation (``capture_gap``), and the
        # scopes it holds it whole in (``whole_scopes``: what stands in for it there),
        # decide its bindings' rows and gaps, so they are part of its fingerprint.
        held = getattr(source, "capture_gap", None)
        whole = getattr(source, "whole_scopes", None)
        if callable(held):
            prints = {relation: _sha(f"{value}\x00{held(relation)}" + (
                          f"\x00{sorted(whole(relation))}" if callable(whole) else ""))
                      for relation, value in prints.items()}
        by_relation.update({(name, relation): value for relation, value in prints.items()})
        pinned[name] = _Pinned(source, records)
    fingerprints = {bid: by_relation.get((b["source"], b["relation"]))
                    for bid, (_, b) in the_plan.bindings.items()}
    return pinned, fingerprints


def _code_digest(names: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        origin = getattr(spec, "origin", None)
        digest.update(f"{name}\n".encode())
        if origin and os.path.isfile(origin):
            digest.update(Path(origin).read_bytes())
    return digest.hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- state dir

def current_generation(state_dir: str | Path, *,
                       committed_since: str | None = None) -> Generation | None:
    """The committed generation of ``state_dir`` (its ``report`` the build report it
    stores), or None (none yet, or ``CURRENT`` names a generation whose files are missing,
    whose store is at another store ``FORMAT`` or whose digest does not match).
    ``committed_since`` (a UTC instant): None too unless ``CURRENT`` was committed at or
    after it (an unreadable time is never)."""
    state = Path(state_dir)
    try:
        current = json.loads((state / CURRENT).read_text(encoding="utf-8"))
        if current.get("format") != FORMAT:
            return None
        if committed_since is not None and not _at_or_after(current.get("committed_at"),
                                                            committed_since):
            return None
        directory = state / "generations" / current["generation"]
        store = directory / "store.sqlite"
        if not (directory / "units.json").is_file() or not store.is_file():
            return None
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute(
                "SELECT key, value FROM store_meta WHERE key IN "
                "('content_digest', 'format', 'build_report')").fetchall())
        finally:
            conn.close()
        report = json.loads(meta.get("build_report") or "{}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        return None
    digest = meta.get("content_digest")
    if digest is None or digest != current.get("content_digest") or \
            meta.get("format") != store_ddl.FORMAT or not isinstance(report, dict):
        return None
    return Generation(current["generation"], store, digest,
                      {**report, "content_digest": digest}, mode=current.get("mode", "cold"))


def _at_or_after(stamp: Any, since: str) -> bool:
    """``stamp`` and ``since`` are UTC instants (ISO text) and ``stamp`` is not earlier."""
    try:
        moments = [datetime.fromisoformat(text.replace("Z", "+00:00"))
                   for text in (stamp, since)]
    except (AttributeError, TypeError, ValueError):
        return False
    if any(moment.tzinfo is None for moment in moments):
        return False
    return moments[0] >= moments[1]


def _units_of(generation: Generation) -> dict[str, Any] | None:
    try:
        units = json.loads((generation.store.parent / "units.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if units.get("format") != FORMAT or units.get("content_digest") != generation.content_digest:
        return None
    return units


def _write_json(path: Path, value: Any) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_dir(path.parent)


class _Lock:
    def __init__(self, state: Path) -> None:
        self.path = state / _LOCK
        self.handle: Any = None

    def __enter__(self) -> "_Lock":
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ProjectionError("generation_build_in_progress", str(self.path)) from None
        return self

    def __exit__(self, *_: Any) -> None:
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _generations(state: Path) -> list[Path]:
    root = state / "generations"
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit()) \
        if root.is_dir() else []


def _sweep(state: Path, keep: set[str]) -> None:
    """Remove unfinished generation directories (no ``units.json``: a build that never
    committed) other than ``keep``. Runs under the build lock."""
    for directory in _generations(state):
        if directory.name not in keep and not (directory / "units.json").is_file():
            shutil.rmtree(directory, ignore_errors=True)


def _prune(state: Path, current: str) -> None:
    finished = [d for d in _generations(state) if (d / "units.json").is_file()]
    stay = {d.name for d in finished[-KEEP:]} | {current}
    for directory in finished:
        if directory.name not in stay:
            shutil.rmtree(directory, ignore_errors=True)


def prune_to_current(state_dir: str | Path) -> bool:
    """Remove every generation of ``state_dir`` but the committed one (a state directory
    kept between runs: no reader holds an earlier one open, so ``KEEP`` is not needed).
    False, removing nothing, while a build holds it or when none is committed."""
    state = Path(state_dir)
    if not (state / "generations").is_dir():
        return False
    try:
        with _Lock(state):
            current = current_generation(state)
            if current is None:
                return False
            for directory in _generations(state):
                if directory.name != current.id:
                    shutil.rmtree(directory, ignore_errors=True)
    except ProjectionError:
        return False
    return True


# --------------------------------------------------------------------------- rows of a unit

def _prefix(the_plan: Plan, binding: str) -> tuple[str, str]:
    """[low, high) of the object ids of one binding (``<Type>:<binding>:<key>``)."""
    tid = the_plan.bindings[binding][0]
    return f"{tid}:{binding}:", f"{tid}:{binding};"


def _delete_unit(conn: sqlite3.Connection, the_plan: Plan, unit: str,
                 events: Mapping[str, list[tuple[str, Mapping[str, Any]]]]) -> None:
    """Delete one unit's rows from a seeded generation (foreign keys deferred: the
    partial build writes the unit's rows again before the commit checks them)."""
    where = the_plan.rows[unit]
    if where[0] == "links":
        _, lid, frm, to, kind = where
        conn.execute("DELETE FROM link_rows WHERE from_object >= ? AND from_object < ? AND "
                     "link_type = ? AND binding_kind = ? AND to_object >= ? AND to_object < ?",
                     (*_prefix(the_plan, frm), lid, kind, *_prefix(the_plan, to)))
        return
    if where[0] != "binding":
        return
    binding = where[1]
    low, high = _prefix(the_plan, binding)
    parts = [(eid, ev["observed_property"] if ev["time_basis"] == "capture_observed"
              else ev["time"]["path"])
             for eid, ev in events.get(the_plan.bindings[binding][0], [])
             if ev["time_basis"] == "capture_observed" or ev["time"]["binding"] == binding]
    if parts:
        # The ids of every event this binding's versions can yield (event_id_of).
        conn.execute("DELETE FROM temp.plx_unit_events")
        cursor = conn.execute("SELECT object_id, l1_handle FROM object_versions "
                              "WHERE object_id >= ? AND object_id < ?", (low, high))
        while True:
            versions = cursor.fetchmany(DEFAULT_BATCH)
            if not versions:
                break
            conn.executemany("INSERT OR IGNORE INTO temp.plx_unit_events VALUES (?)",
                             [(event_id_of(eid, handle, object_id, part),)
                              for object_id, handle in versions for eid, part in parts])
        for table in ("event_objects", "events"):
            conn.execute(f"DELETE FROM {table} WHERE event_id IN "
                         "(SELECT event_id FROM temp.plx_unit_events)")
    # Values by their physical rows (v6: object_values is a view over value_cells and
    # value_sources); the shared fields, kinds and link texts may stay unreferenced.
    conn.execute("DELETE FROM value_cells WHERE src IN (SELECT src FROM value_sources "
                 "WHERE object_id >= ? AND object_id < ?)", (low, high))
    for table in ("identifiers", "value_sources", "object_versions", "object_removals",
                  "objects"):
        conn.execute(f"DELETE FROM {table} WHERE object_id >= ? AND object_id < ?",
                     (low, high))


# --------------------------------------------------------------------------- build

_CURRENT_GENERATION = object()


def build_generation(state_dir: str | Path, schema: Any, sources: Mapping[str, CaptureSource],
                     *, previous: Any = _CURRENT_GENERATION,
                     normalizer: Normalizer | None = None, as_known_at: Any = None,
                     clock: Callable[[], datetime] | None = None,
                     batch_size: int = DEFAULT_BATCH,
                     post_project: Mapping[str, Callable[[StoreBuilder, Projector],
                                                         Mapping[str, Any]]] | None = None,
                     checkpoint: Callable[[], None] | None = None,
                     reuse: bool = False,
                     before_seed: Callable[[Path], None] | None = None,
                     on_stage: Callable[[str, int, int], None] | None = None) -> Generation:
    """Build the next generation of ``state_dir`` from ``sources`` under ``schema`` and
    commit it (``CURRENT``). ``previous``: the generation to reuse unchanged units from
    (default: the committed one; None = a cold build). ``reuse``: when no unit changed
    from ``previous``, return it as it is (module docstring, "Reuse"). ``before_seed``:
    called with the store a partial build is about to copy, before it copies it (a
    caller's disk check: it may raise, and nothing is written). ``on_stage``:
    ``Projector.on_stage`` (measurement only; never in a unit key). Every other
    argument is ``build_store``'s. Raises ``ProjectionError("generation_build_in_progress")``
    while another build holds the state directory; on any failure the committed generation
    is left as it was."""
    state = Path(state_dir)
    (state / "generations").mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with _Lock(state):
        current = current_generation(state)
        base = current if previous is _CURRENT_GENERATION else previous
        _sweep(state, {g.id for g in (current, base) if g is not None})
        options = {"normalizer": normalizer if normalizer is not None else
                   _default_normalizer(), "as_known_at": as_known_at, "clock": clock,
                   "batch_size": batch_size, "post_project": post_project,
                   "checkpoint": checkpoint, "on_stage": on_stage}
        try:
            generation = _build(state, schema, sources, base, reuse=reuse,
                                before_seed=before_seed, **options)
        except _Unattributed:
            # A report entry outside every unit (a projection hook that names none):
            # the rows are right but the report of a partial build would not be, so
            # this build is cold, and so are the next ones until that is fixed.
            generation = _build(state, schema, sources, None, **options)
        if generation.mode != "reused":
            _prune(state, generation.id)
    generation.seconds["total"] = round(time.monotonic() - started, 3)
    return generation


class _Unattributed(Exception):
    pass


def _build(state: Path, schema: Any, sources: Mapping[str, CaptureSource],
           base: Generation | None, *, normalizer: Normalizer | None, as_known_at: Any,
           clock: Callable[[], datetime] | None, batch_size: int,
           post_project: Any, checkpoint: Callable[[], None] | None,
           on_stage: Callable[[str, int, int], None] | None = None,
           reuse: bool = False,
           before_seed: Callable[[Path], None] | None = None) -> Generation:
    started = time.monotonic()
    the_plan = plan(schema)
    pinned, fingerprints = _pin(sources, the_plan)
    seconds = {"fingerprint": round(time.monotonic() - started, 3)}
    started = time.monotonic()
    context = _sha(canonical({
        "format": FORMAT, "schema": _get(schema, "digest"),
        "as_known_at": known_at(as_known_at) if as_known_at is not None else None,
        "normalizer": None if normalizer is None else
        f"{getattr(normalizer, '__module__', '')}.{getattr(normalizer, '__qualname__', '')}",
        "code": _code_digest(list(_CODE) + [type(s).__module__ for s in sources.values()]
                             + [getattr(normalizer, "__module__", "")])}))
    keys = {unit: _sha(context + canonical({b: fingerprints.get(b) for b in sorted(deps)}))
            for unit, deps in the_plan.units.items()}
    units = _units_of(base) if base is not None else None
    reusable = the_plan.acyclic and units is not None and units.get("context") == context
    dirty = {u for u in keys if units["keys"].get(u) != keys[u]} if reusable else set(keys)
    if reuse and reusable and not dirty:
        return replace(base, mode="reused", units=len(keys), units_rebuilt=0, bindings_read=0,
                       seconds=seconds)
    incremental = reusable and len(dirty) < len(keys)
    if incremental and before_seed is not None:
        before_seed(base.store)
    scope: set[str] | None = None
    if incremental:
        scope = set()
        for unit in dirty:
            scope |= the_plan.units[unit]
    names = [d.name for d in _generations(state)]
    gen_id = f"{(int(names[-1]) + 1) if names else 1:08d}"
    directory = state / "generations" / gen_id
    directory.mkdir()
    try:
        projector = Projector(schema, pinned, normalizer=normalizer, as_known_at=as_known_at,
                              batch_size=batch_size, scratch_dir=directory, scope=scope,
                              emit=dirty if incremental else None)
        projector.checkpoint = checkpoint
        projector.on_stage = on_stage
        with StoreBuilder(directory / "store.sqlite",
                          seed=base.store if incremental else None) as builder:
            open_generation(builder)
            if incremental:
                _seed_delete(builder.connection, the_plan, schema, dirty)
            projector.project_into(builder)
            unattributed = projector.report.unattributed
            if unattributed and incremental:
                raise _Unattributed(unattributed)
            parts = {unit: (projector.report.parts[unit].as_dict()
                            if unit in projector.report.parts else _EMPTY)
                     if unit in dirty else units["fragments"].get(unit, _EMPTY)
                     for unit in sorted(keys)}
            seconds["project"] = round(time.monotonic() - started, 3)
            started = time.monotonic()
            # A cold build reports what it projected; an incremental one merges the parts
            # of its units (equal for a cold build: every entry is in exactly one part,
            # and samples are order-independent).
            report = finish_generation(
                builder, projector, merge_reports(parts.values()) if incremental
                else projector.report.as_dict(), post_project=post_project, clock=clock)
        seconds["finish"] = round(time.monotonic() - started, 3)
        mode = ("incremental" if incremental else
                "cold" if the_plan.acyclic else "cold_cyclic_lineage")
        _write_json(directory / "units.json", {
            "format": FORMAT, "content_digest": report["content_digest"],
            # Parts that miss an entry are never reused: no context, no reuse.
            "context": None if unattributed else context,
            "keys": keys, "fragments": {u: p for u, p in parts.items() if p != _EMPTY}})
        _write_json(state / CURRENT, {
            "format": FORMAT, "generation": gen_id, "content_digest": report["content_digest"],
            "mode": mode,
            "committed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return Generation(gen_id, directory / "store.sqlite", report["content_digest"], report,
                      mode=mode, units=len(keys), units_rebuilt=len(dirty),
                      bindings_read=len(scope) if scope is not None else len(the_plan.bindings),
                      seconds=seconds)


def _seed_delete(conn: sqlite3.Connection, the_plan: Plan, schema: Any,
                 dirty: Iterable[str]) -> None:
    """Remove from the seeded copy every row a partial build writes again: the rows of
    the changed units and every link and value no unit owns (the ``post_project`` steps
    write those again)."""
    conn.execute("PRAGMA defer_foreign_keys = ON")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS plx_unit_events "
                 "(event_id TEXT PRIMARY KEY)")
    events: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for eid, ev in sorted((_get(schema, "event_types") or {}).items()):
        events.setdefault(ev["object_type"], []).append((eid, ev))
    kinds = ", ".join(f"'{kind}'" for kind in _PROJECTED_LINK_KINDS)
    conn.execute(f"DELETE FROM link_rows WHERE binding_kind NOT IN ({kinds})")
    conn.execute("DELETE FROM value_cells WHERE kind IN "
                 "(SELECT kind FROM value_kinds WHERE parser = ?)", (POST_PROJECT,))
    conn.execute("DELETE FROM value_sources WHERE NOT EXISTS "
                 "(SELECT 1 FROM value_cells c WHERE c.src = value_sources.src)")
    for unit in sorted(dirty):
        _delete_unit(conn, the_plan, unit, events)
    conn.execute("DROP TABLE temp.plx_unit_events")


__all__ = ["CURRENT", "FORMAT", "Generation", "KEEP", "Plan", "build_generation",
           "current_generation", "plan", "prune_to_current"]
