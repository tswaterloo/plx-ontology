"""Incremental projection by generation (L7).

``ontology.project.build_store`` projects every capture into a fresh generation each
time. :func:`build_generation` keeps generations in a state directory the caller
passes in and rebuilds only what changed: the projection is cut into **units**
(``ontology.project.binding_unit``: one binding's objects, versions, values,
identifiers and events; ``link_unit``: one link binding's links), each with the
bindings whose captures it depends on (:func:`plan`, from the schema alone). A unit's
key is a digest of the capture fingerprints of those bindings, the slice of the schema
they are declared in, ``as_known_at``, the normalizer and the version of the code that
writes rows (``ontology.build_keys``: a change to a step, a reader of captured files, a
capture writer or another type of the schema leaves it as it was). A unit whose key is
unchanged is reused from the last generation (rows and build-report part); the rest
are projected again by a partial :class:`ontology.project.Projector` that reads only
the bindings they depend on and writes only their rows into a byte copy of the last
generation. The result is the generation a cold build of the same captures gives
(same rows, links, citations, markings, tiers and build report, so the same content
digest): an unchanged unit's rows are, by its key, the rows a cold build writes.

Dependencies (bindings whose captures a unit's rows can read):

* ``lin(B)``: B; the ends of the link types B's lineage rules hop through
  (``via_link``) and of its ``inherit`` link type, plus ``lin`` of those ends (an
  inherited lineage is the target's lineage); for a binding that ``stands_for`` another,
  that one and the bindings listed before B standing for it (they decide what B holds).
* ``val(B)``: ``lin(B)`` plus the ends of the link types its units/currencies/tax bases
  are read through, and the ``local_currency`` binding of each record holding a currency
  it reads (a blank code reads the local currency there). ``b:B`` = ``val(B)`` plus the ends of the link types its events' qualifiers
  follow (capture events compare B's own values).
* "The ends of a link type" read from B: the bindings of those of its link bindings that
  have B, a binding B stands for or one standing for B as an end (from, to or join): a
  hop or qualifier from a version of B follows the links whose from or to object is its
  object, and no other link binding writes one. (A BC change no longer rebuilds the
  Portal bindings whose link types also have BC link bindings.)
* A link unit: its ends (from, to and join binding), ``lin`` of each end (citations
  carry the cited version's lineage) and, for ``same_record``, ``val`` of each end
  (property conflicts compare their values).

Objects: a binding whose rows are each a function of one object's own records while link
bindings read it (``object_local``: mail messages, attachments, container members,
SharePoint files, supplier confirmations) keeps, beside its generation (``objects.sqlite``,
``_Objects``), each object's record fingerprint and report part. When its unit changed, the
next build fingerprints its objects again and rewrites only the rows of the objects whose
records changed, are new or are gone (``units.json`` ``objects``: ``delta``); it stages
the binding whole for the link units that read it (rebuilt whole: their keys hold its
print) and merges its unchanged objects' report parts from the base, so its rows and
report are a cold build's. A base without that file for the binding, another basis (build
context, schema slice, reader, the object code ``build_keys.object_code_digest``), more
than half its objects changed, or a cold build projected by worker processes: the binding
is rebuilt whole (``whole`` keeps every object's part for the next build, ``plain`` none).

Mode: a build whose changed units hold more than ``COLD_SHARE`` of the last generation
(by values and objects), or whose incremental space estimate exceeds the cold one, is
cold (a new store beside the committed one, which stays current): rewriting most of a
seeded copy costs more disk and about as much time. A caller's ``space`` guard that
refuses the chosen mode gets the other one; the build fails only when it refuses both
(:func:`space_need`).

A schema change rewrites unit by unit only while every binding of the base keeps its
type and every event type of the base its definition (``units.json`` ``shape``), and a
unit it removes is a shard or a link unit whose rows are found by its id; otherwise the
build is cold. A base built under the earlier keys (one context of whole code files) is
rebuilt cold unless the operator names the commit that built it and that commit proves
every part (``ontology.build_adopt``; ``Generation.need`` ``adopted`` says how or why not).

A report entry made outside every unit (a projection hook naming none) is in no unit's
part: every build leaves it out of its report and counts it (``Generation.unattributed``,
``units.json`` ``unattributed``), so it never makes a build, nor the next one, cold.

A schema whose lineage inheritance is cyclic (a binding inherits, through any chain,
from itself) has an order-dependent lineage, so it is never built incrementally
(every build is cold; ``Generation.mode`` says so).

State directory (the caller owns where it is; this module owns what is inside)::

    <state_dir>/CURRENT                      JSON: the committed generation (atomic)
    <state_dir>/.build.lock                  flock held by the one running build
    <state_dir>/generations/<id>/store.sqlite   the generation (ProjectionStore reads it)
    <state_dir>/generations/<id>/units.json     unit keys and build-report parts
    <state_dir>/readings.sqlite              readings of captured files kept between builds
    <state_dir>/member-readings.sqlite       readings of container members kept between builds

A build writes a new generation directory and commits by replacing ``CURRENT`` (which
records when, ``committed_at``); until then the previous generation is untouched and stays
current. A build that is interrupted (an exception, a ``checkpoint`` that raises, a killed
process) leaves ``CURRENT`` and its generation as they were; the next build removes the
unfinished directory and builds again from the last committed generation. The previous
generation stays on disk after a commit (``KEEP``) so a reader holding it open keeps
reading it; :func:`prune_to_current` removes it from a directory kept between runs
(``operating.warm_store``), where no reader holds one.

Resume: a build's projection is hours of work, and its ``post_project`` steps and its
digest hours more. Once every unit is projected the build commits its rows in its own
file as a checkpoint (``ontology.store.StoreBuilder.keep_to_resume``: synced, a rollback
journal on disk from then on) and writes ``generations/<id>/projected.json`` (the unit
keys, the base generation, the projected report and its counts, what ``units.json``
needs); after each step it commits again, naming the steps done and their reports, and
the commit's checked rows are a last checkpoint before the digest. A failure after the
first checkpoint (a step's exception, a killed process, a restart) leaves that directory
with its file rolled back to the last checkpoint (``CURRENT`` as it was); the next build
whose unit keys, context, projection code (``_RESUME_CODE``, its own digest) and base
generation are the checkpoint's reopens the file and goes on with the first step not done (the steps' code is
not in the unit keys: a fix of a failed step is taken), else removes it and builds as
before. A resumed incremental build resolves every mention again and digests in full (the
changed identifier keys and written segments of the first attempt are not kept).
Disk: while the steps run, the file's rollback journal holds the original of every page
of the checkpoint a step changes (the link, text-index and value tables and their indexes
it writes into); ``space_need`` does not count it, and its size on a full build is not
measured yet.

Reuse (``build_generation(..., previous=<a generation of another state directory>,
reuse=True)``): when every unit key of the captures equals ``previous``'s, ``previous`` is
returned as it is (``mode`` ``reused``): nothing is projected, copied or written, so its
rows are, by the keys, the rows a build of these captures writes. Its ``post_project``
rows are ``previous``'s: a caller reuses only a generation whose ``post_project`` inputs
(the review and confirmation stores of the state directory it was built over) are its own.
``current_generation(..., committed_since=)`` returns a generation only when it was
committed at or after that instant (a verifier session's start: one session's projection,
never an earlier one's).

Fingerprints: a source opens its read snapshot through ``ontology.project.begin_snapshot``
and names the file state that snapshot reads (``watermark()``: device, inode, size and
modification and change times of its database files, taken before and after the snapshot
starts and as the source was opened). ``units.json`` keeps each source's watermark with the
fingerprints read under it and the digest of the code that read them (``readers``:
``ontology.build_keys.reader_digest``); the next build whose source names the same
watermark and reads it with the same code reads the same bytes, so it takes those
fingerprints and reads nothing of it. Any
other source is fingerprinted in full. The relations a source derives from captured bytes
(``documentFields``, ``containerMembers``) are fingerprinted from what they are derived from
(``ontology.project.derived_fingerprint``), never by deriving them.

Digest: the content digest is composed per segment (``ontology.store.content_digest_parts``):
a partial build digests again only the segments its writes touched, the post_project steps'
included, and takes the seed's digest of every other one.

Readings: the text read from captured files (a PDF's pages, the like) is kept between
builds in ``<state_dir>/readings.sqlite`` under the digest of the code that reads it
(``integrations.captured_text.KeptReadings``), so a file is parsed once, not by every build.

Memory: captures are fingerprinted and projected as streams (the projector's bound,
``ontology/project.py``); only unit keys and report parts are held.

Stdlib only (``fcntl``: POSIX).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology.project import (_PROJECTED_LINK_KINDS, _REC_FIELDS, DEFAULT_BATCH, ETL_REINSERTED,
                              POST_PROJECT, SHARD_BUCKETS, CaptureSource, Normalizer,
                              ProjectionError, Projector, _default_normalizer,
                              _fingerprint_cells, _get, binding_key, binding_unit,
                              derivation_code, event_id_of, finish_generation, link_ends,
                              link_unit, merge_reports, object_bucket, object_id_of,
                              object_unit,
                              open_generation, projected_counts, shard_of, shard_unit,
                              stream_fingerprint)
from integrations.captured_text import kept_readings
from integrations.kept_members import MEMBERS_FILE, kept_members
from ontology.document_fields import kept_fields
from ontology import build_adopt, build_keys, build_prints, store_ddl
from ontology.build_timings import BuildTimings
from ontology.finish_digest import ParallelBuilder
from ontology.store import (_DIRTY_TRIGGERS, StoreBuilder, StoreError, _fsync_dir, canonical,
                            known_at, object_segment)
from operating import declared_schema as _schemas
from operating.declared_schema import project_objects as _PROJECT_OBJECTS

FORMAT = "plx-projection-generations/v1"
CURRENT = "CURRENT"
READINGS = "readings.sqlite"   # readings of captured files kept between builds
FIELDS = "fields.sqlite"       # fields of captured documents kept between builds
CHECKPOINT = "projected.json"  # a build's projection, kept to resume it (module docstring)
CHECKPOINT_FORMAT = "plx-projection-checkpoint/v1"
MEMBERS = MEMBERS_FILE   # container member readings kept between builds
KEEP = 2
_LOCK = ".build.lock"
_EMPTY = {"counts": {}, "gaps": []}
# The code a kept build's rows and fingerprints were made by (``_resume``): its own digest,
# never only the unit keys' context, so a resume holds whatever the keys leave out.
_RESUME_CODE = ("ontology.project", "ontology.project_incremental", "ontology.project_parallel",
                "ontology.store", "ontology.store_ddl", "ontology.units", "ontology.schema",
                "ontology.identifiers")


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
    seed: str | None = None         # how an incremental build laid its seed: clone | copy
    need: dict[str, Any] = field(default_factory=dict)   # the space estimate it checked
    # How its content digest was made: ``mode`` full (every row) or segments (the seed's
    # segment digests and those of the segments it wrote, ``segments`` of them).
    digest: dict[str, Any] = field(default_factory=dict)
    # Report entries the projection made outside every unit (a hook naming none): left out
    # of the report in every mode, counted here and in ``units.json`` (``unattributed``).
    unattributed: int = 0


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
    # Bindings whose rows are each a function of one object's own records (``isolated``):
    # the ones a build may cut into shards (``with_shards``).
    isolated: frozenset[str] = frozenset()


class _Cycle(Exception):
    pass


def plan(schema: Any) -> Plan:
    """Units and their dependencies, from the schema alone (module docstring)."""
    types = dict(_get(schema, "object_types") or {})
    links = dict(_get(schema, "link_types") or {})
    events = dict(_get(schema, "event_types") or {})
    bindings = {b["id"]: (tid, b) for tid, t in types.items() for b in t.get("bindings", [])}

    # A binding and those it stands for or that stand for it: the bindings whose objects
    # its records can be (``stands_for``).
    stood = {bid: {(b.get("stands_for") or {}).get("binding")} - {None}
             for bid, (_, b) in bindings.items()}
    kin = {bid: {bid} | stood[bid] | {other for other, of in stood.items() if bid in of}
           for bid in bindings}

    def ends(link_id: str | None, bid: str | None = None) -> set[str]:
        """The bindings the links of ``link_id`` read; with ``bid``, only of the link
        bindings with an end among ``bid``'s kin: a hop, qualifier, unit or currency read
        through ``link_id`` from a version of ``bid`` follows the links whose from or to
        object is that version's object, and only those link bindings write such links."""
        out: set[str] = set()
        for lb in (links.get(link_id) or {}).get("bindings", []):
            found = link_ends(lb, bindings)
            if bid is None or found & kin[bid]:
                out |= found
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
                    out |= ends(when["via_link"], bid)
        if spec.get("inherit"):
            out |= ends(spec["inherit"], bid)
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
                            out |= ends(ref.get("via_link"), bid)
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
                            deps |= ends(q.get("via"), bid)
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
    return Plan(units, rows, bindings, acyclic=True,
                isolated=isolated(units, bindings, types, events, kin))


def isolated(units: Mapping[str, frozenset[str]],
             bindings: Mapping[str, tuple[str, Mapping[str, Any]]],
             types: Mapping[str, Any], events: Mapping[str, Any],
             kin: Mapping[str, set[str]]) -> frozenset[str]:
    """The bindings B whose projected rows and gaps are each a function of one object's
    own records: B's unit reads B alone and no other unit reads B (no link binding has B
    as an end, nothing hops into it or reads its values), no event type is of B's type,
    B neither stands for a binding nor is stood for, no binding (B included) reads a local
    currency from B and B reads none, and B's relation is not ETL re-inserted (its copies
    collapse across objects). Every record names its object by the binding key alone, so
    the rows of the objects of one shard are what a cold build writes for them."""
    readers: dict[str, int] = {}
    for deps in units.values():
        for bid in deps:
            readers[bid] = readers.get(bid, 0) + 1
    typed = {ev.get("object_type") for ev in events.values()}
    locals_ = {(b.get("local_currency") or {}).get("binding") for _, b in bindings.values()}
    out = set()
    for bid, (tid, b) in bindings.items():
        if units.get(binding_unit(bid)) != frozenset({bid}) or readers.get(bid) != 1:
            continue
        if tid in typed or kin[bid] != {bid} or bid in locals_ or b.get("local_currency"):
            continue
        if b["relation"] in ETL_REINSERTED.get(b["source"], {}):
            continue
        out.add(bid)
    return frozenset(out)


def object_local(schema: Any, the_plan: Plan) -> frozenset[str]:
    """The bindings B of ``the_plan`` (``plan(schema)``; none of a cyclic one), none of
    them ``isolated`` (sharded instead), whose own rows (objects, versions, values,
    identifiers, events, removals) and report entries are each a function of one object's own records, while
    link bindings may read them: B's unit reads B alone; B neither stands for a binding
    nor is stood for; no binding reads a local currency from B and B reads none; B's
    relation is not ETL re-inserted; every event type of B's type qualifies only its own
    object (``via: self``: a date event reads its version, a capture event its object's
    previous version); and every other unit reading B is a projected link unit that is
    not ``same_record`` (no conflict writes into B's values) and reads its ends alone
    (each end's lineage is its own, so the links it makes from B's records are a cold
    build's when B is staged whole: ``Projector._stage_whole``). Such a link unit is
    rebuilt whole whenever B's records change (its key holds B's print). Every record
    names its object by the binding key alone. Anything else stays whole (fail closed)."""
    if not the_plan.acyclic:
        return frozenset()
    units, rows, bindings = the_plan.units, the_plan.rows, the_plan.bindings
    links = dict(_get(schema, "link_types") or {})
    events = dict(_get(schema, "event_types") or {})
    stood = {bid: {(b.get("stands_for") or {}).get("binding")} - {None}
             for bid, (_, b) in bindings.items()}
    kin = {bid: {bid} | stood[bid] | {other for other, of in stood.items() if bid in of}
           for bid in bindings}
    readers: dict[str, list[str]] = {}
    for unit, deps in units.items():
        for bid in deps:
            readers.setdefault(bid, []).append(unit)
    locals_ = {(b.get("local_currency") or {}).get("binding") for _, b in bindings.values()}
    unit_ends: dict[str, set[str]] = {}
    for lid, link in links.items():
        for index, lb in enumerate(link.get("bindings", [])):
            unit = link_unit(lid, index, lb, bindings)
            unit_ends.setdefault(unit, set()).update(link_ends(lb, bindings))
    out = set()
    for bid, (tid, b) in bindings.items():
        if bid in the_plan.isolated:
            continue
        if "@" in bid or "#" in bid or units.get(binding_unit(bid)) != frozenset({bid}):
            continue
        if kin[bid] != {bid} or bid in locals_ or b.get("local_currency"):
            continue
        if b["relation"] in ETL_REINSERTED.get(b["source"], {}):
            continue
        if any(q.get("via") != "self" for ev in events.values()
               if ev.get("object_type") == tid for q in ev.get("qualifiers", [])):
            continue
        ok = True
        for unit in readers.get(bid, []):
            if unit == binding_unit(bid):
                continue
            where = rows.get(unit) or ()
            if not where or where[0] != "links" or where[4] == "same_record" or \
                    units[unit] != frozenset(unit_ends.get(unit, ())):
                ok = False
                break
        if ok:
            out.add(bid)
    return frozenset(out)


# A sharded binding has about this many records a shard, and is sharded only from twice
# that many records (``shard_count``).
SHARD_RECORDS = 2048


def shard_count(records: int, held: int | None = None) -> int:
    """How many shards a binding of ``records`` records is cut into: a power of two (1:
    not sharded), at most ``SHARD_BUCKETS``. A count ``held`` from the last build is kept
    until the records outgrow it fourfold, so its shards' keys stay comparable."""
    wanted = 1
    while wanted < SHARD_BUCKETS and wanted * SHARD_RECORDS < records:
        wanted *= 2
    if wanted < 2:
        wanted = 1
    if held and held > 1 and wanted < 4 * held:
        return held
    return wanted


def with_shards(the_plan: Plan, shards: Mapping[str, int]) -> Plan:
    """``the_plan`` with each binding of ``shards`` (binding -> count > 1) cut into its
    shard units (``ontology.project.shard_unit``, rows ``("shard", binding, count, k)``)
    beside its own unit, which then holds no rows: only the binding's gaps and those of
    records naming no object."""
    units, rows = dict(the_plan.units), dict(the_plan.rows)
    for bid, count in shards.items():
        rows[binding_unit(bid)] = ("gaps",)
        for k in range(count):
            units[shard_unit(bid, k)] = frozenset({bid})
            rows[shard_unit(bid, k)] = ("shard", bid, count, k)
    return replace(the_plan, units=units, rows=rows)


# --------------------------------------------------------------------------- objects

# Object-local bindings (``object_local``) of at least this many records are rebuilt object
# by object; smaller ones are rebuilt whole, as before.
OBJECT_RECORDS = 2 * SHARD_RECORDS
# The file of a generation holding, per object of each object-local binding it was built
# with object by object, the fingerprint of its records and its report part (``_Objects``).
OBJECTS = "objects.sqlite"
OBJECTS_FORMAT = "plx-projection-objects/v1"


class _Objects:
    """The per-object fingerprints and report parts of a generation's object-local
    bindings (``<generation>/objects.sqlite``).

    For each binding B it holds (``bindings``) the key of B's unit in that generation and
    B's *basis* (the build context, B's schema slice, its source's reader code and what
    the source says it does not hold of B's relation: everything of B's unit key but its
    records), and per object of B (``objects``): the digest of every field of every record
    naming it (``_fingerprint_cells``, in stream order) and the merge of every report
    entry its rows were projected with (``object_unit``; NULL: none). A later build whose
    B has that basis, over a base whose B key is the one held, knows by the fingerprints
    which objects' records changed, were added or are gone (``changed``): only those
    objects' rows differ from the base's (B's rows are each a function of one object's
    records: ``object_local``), and B's report part is the merge of what B reports as a
    whole (re-projected every time: records naming no object, what the source does not
    hold) and of its objects' parts, the unchanged ones' taken from the base."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.executescript("""
            PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
            """)
        _schemas.apply(self.conn, _PROJECT_OBJECTS)
        self.conn.executescript("""
            CREATE TEMP TABLE lines (binding TEXT, object_id TEXT, seq INTEGER, line BLOB);
            CREATE TEMP TABLE pending (binding TEXT, object_id TEXT, seq INTEGER PRIMARY KEY,
                part TEXT);
            CREATE TEMP TABLE changed (binding TEXT, object_id TEXT,
                PRIMARY KEY (binding, object_id)) WITHOUT ROWID;
            """)
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('format', ?)", (OBJECTS_FORMAT,))
        self._parts: list[tuple[str, str, str]] = []
        self.base = False

    def attach(self, base: Path | None) -> None:
        """Attach the base generation's file (read only) when it is of this format."""
        if base is None or not base.is_file():
            return
        try:
            self.conn.execute("ATTACH DATABASE ? AS base", (f"file:{base}?mode=ro",))
            held = self.conn.execute("SELECT value FROM base.meta WHERE key = 'format'"
                                     ).fetchone()
        except sqlite3.Error:
            held = None
        if held is None or held[0] != OBJECTS_FORMAT:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("DETACH DATABASE base")
            return
        self.base = True

    def held(self, binding: str, key: str | None, basis: str) -> bool:
        """Whether the base holds ``binding``'s objects for the base's own unit key
        ``key`` and this build's ``basis``."""
        if not self.base or key is None:
            return False
        row = self.conn.execute("SELECT key, basis FROM base.bindings WHERE binding = ?",
                                (binding,)).fetchone()
        return row is not None and row[0] == key and row[1] == basis

    def carry(self, binding: str, key: str, basis: str) -> None:
        """An unchanged binding: the base's rows for it, as they are."""
        self.conn.execute("INSERT INTO objects SELECT * FROM base.objects WHERE binding = ?",
                          (binding,))
        self.conn.execute("INSERT OR REPLACE INTO bindings SELECT binding, ?, ?, objects "
                          "FROM base.bindings WHERE binding = ?", (key, basis, binding))

    def read(self, pinned: Mapping[str, Any], the_plan: Plan, bids: Iterable[str]
             ) -> dict[str, int]:
        """Fingerprint each object of ``bids`` from its records (one pass per relation of
        a source, as ``_shard_prints``); binding -> its records naming an object."""
        passes: dict[tuple[str, str], list[str]] = {}
        for bid in sorted(bids):
            b = the_plan.bindings[bid][1]
            passes.setdefault((b["source"], b["relation"]), []).append(bid)
        counts = {bid: 0 for bid in bids}
        seq = 0
        batch: list[tuple[str, str, int, bytes]] = []
        for (name, relation), group in sorted(passes.items()):
            reading = [(bid, *the_plan.bindings[bid]) for bid in group]
            for rec in pinned[name].records([relation])[relation]:
                line = None
                for bid, tid, b in reading:
                    key = binding_key(b, rec.row)
                    if key is None:   # names no object: the binding's own part
                        continue
                    if line is None:
                        line = hashlib.sha256(
                            repr(_fingerprint_cells(rec)).encode("utf-8")).digest()
                    seq += 1
                    counts[bid] += 1
                    batch.append((bid, object_id_of(tid, bid, key), seq, line))
                if len(batch) >= DEFAULT_BATCH:
                    self.conn.executemany("INSERT INTO temp.lines VALUES (?, ?, ?, ?)", batch)
                    batch.clear()
        self.conn.executemany("INSERT INTO temp.lines VALUES (?, ?, ?, ?)", batch)
        self.conn.execute("CREATE INDEX temp.lines_by_object ON lines (binding, object_id, seq)")
        return counts

    def prints(self, binding: str, basis: str, key: str) -> int:
        """Write ``binding``'s object fingerprints (``read``) as this generation's; its
        object count."""
        cursor = self.conn.execute("SELECT object_id, line FROM temp.lines WHERE binding = ? "
                                   "ORDER BY object_id, seq", (binding,))
        rows: list[tuple[str, str, bytes, None]] = []
        count = 0
        current, digest = None, hashlib.sha256()

        def put() -> None:
            nonlocal count
            if current is not None:
                rows.append((binding, current, digest.digest(), None))
                count += 1
        for object_id, line in cursor:
            if object_id != current:
                put()
                current, digest = object_id, hashlib.sha256()
            digest.update(line)
            if len(rows) >= DEFAULT_BATCH:
                self.conn.executemany("INSERT INTO objects VALUES (?, ?, ?, ?)", rows)
                rows.clear()
        put()
        self.conn.executemany("INSERT INTO objects VALUES (?, ?, ?, ?)", rows)
        self.conn.execute("INSERT OR REPLACE INTO bindings VALUES (?, ?, ?, ?)",
                          (binding, key, basis, count))
        return count

    def changed(self, binding: str) -> int:
        """Mark the objects of ``binding`` whose fingerprint differs from the base's, that
        are new or that are gone (``temp.changed``); how many."""
        self.conn.execute(
            "INSERT OR IGNORE INTO temp.changed SELECT n.binding, n.object_id FROM objects n "
            "LEFT JOIN base.objects o ON o.binding = n.binding AND o.object_id = n.object_id "
            "WHERE n.binding = ? AND (o.print IS NULL OR o.print <> n.print)", (binding,))
        self.conn.execute(
            "INSERT OR IGNORE INTO temp.changed SELECT o.binding, o.object_id FROM "
            "base.objects o WHERE o.binding = ? AND NOT EXISTS (SELECT 1 FROM objects n "
            "WHERE n.binding = o.binding AND n.object_id = o.object_id)", (binding,))
        return self.conn.execute("SELECT COUNT(*) FROM temp.changed WHERE binding = ?",
                                 (binding,)).fetchone()[0]

    def changed_ids(self, binding: str) -> frozenset[str]:
        return frozenset(row[0] for row in self.conn.execute(
            "SELECT object_id FROM temp.changed WHERE binding = ?", (binding,)))

    def part(self, binding: str, object_id: str, part: Mapping[str, Any]) -> None:
        """One report part of an object written by this build (``Projector.object_parts``:
        an object may hand on several, merged in ``finish``)."""
        self._parts.append((binding, object_id, canonical(part)))
        if len(self._parts) >= DEFAULT_BATCH:
            self._flush()

    def _flush(self) -> None:
        self.conn.executemany("INSERT INTO temp.pending (binding, object_id, part) "
                              "VALUES (?, ?, ?)", self._parts)
        self._parts.clear()

    def finish(self, binding: str, delta: bool, rest: Mapping[str, Any]) -> dict[str, Any]:
        """Set the parts of ``binding``'s objects (``delta``: the unchanged objects'
        from the base, the changed ones' from this build; else every one from this
        build) and return the binding unit's part: ``rest`` (what it reported as a whole)
        merged with every object's."""
        self._flush()
        if delta:
            self.conn.execute(
                "UPDATE objects SET part = (SELECT o.part FROM base.objects o WHERE "
                "o.binding = objects.binding AND o.object_id = objects.object_id) "
                "WHERE binding = ? AND NOT EXISTS (SELECT 1 FROM temp.changed c WHERE "
                "c.binding = objects.binding AND c.object_id = objects.object_id)",
                (binding,))
        cursor = self.conn.execute("SELECT object_id, part FROM temp.pending WHERE binding = ? "
                                   "ORDER BY object_id, seq", (binding,))
        updates: list[tuple[str, str, str]] = []
        for object_id, group in itertools.groupby(cursor, key=lambda row: row[0]):
            parts = [json.loads(row[1]) for row in group]
            updates.append((canonical(merge_reports(parts)), binding, object_id))
            if len(updates) >= DEFAULT_BATCH:
                self.conn.executemany("UPDATE objects SET part = ? WHERE binding = ? AND "
                                      "object_id = ?", updates)
                updates.clear()
        self.conn.executemany("UPDATE objects SET part = ? WHERE binding = ? AND object_id = ?",
                              updates)
        self.conn.execute("DELETE FROM temp.pending WHERE binding = ?", (binding,))
        # An object part handed on for an object with no fingerprint (no record names it)
        # cannot be: every written object is staged from a record that names it.
        merged = merge_reports(itertools.chain([rest], (
            json.loads(row[0]) for row in self.conn.execute(
                "SELECT part FROM objects WHERE binding = ? AND part IS NOT NULL",
                (binding,)))))
        return merged

    def drop(self, binding: str) -> None:
        """Hold nothing of ``binding`` (rebuilt whole without its objects' parts)."""
        self.conn.execute("DELETE FROM objects WHERE binding = ?", (binding,))
        self.conn.execute("DELETE FROM bindings WHERE binding = ?", (binding,))

    def close(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS temp.lines")
        if self.base:
            self.conn.execute("DETACH DATABASE base")
            self.base = False
        self.conn.close()


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


_stream_fingerprint = stream_fingerprint


def _pin(sources: Mapping[str, CaptureSource], the_plan: Plan,
         held: Mapping[str, Any] | None = None
         ) -> tuple[dict[str, Any], dict[str, str | None], dict[str, Any]]:
    """(pinned sources, binding -> fingerprint of its relation (None = not captured),
    source -> its watermark and relation fingerprints, for the next build).

    ``held``: the last build's watermarks (of the sources read with the same code:
    ``build_keys.reader_digest``). A source whose
    ``watermark()`` (taken as its snapshot opened: ``ontology.project.begin_snapshot``)
    equals the one held reads the same bytes as then, so its relations' fingerprints are
    the ones held and it is not read; any other source is fingerprinted in full."""
    relations: dict[str, set[str]] = {}
    for _, b in the_plan.bindings.values():
        if b["source"] in sources:
            relations.setdefault(b["source"], set()).add(b["relation"])
    pinned: dict[str, Any] = dict(sources)
    by_relation: dict[tuple[str, str], str] = {}
    marks: dict[str, Any] = {}
    for name, wanted in sorted(relations.items()):
        source = sources[name]
        records = source.records(sorted(wanted))
        watermark = getattr(source, "watermark", None)
        mark = watermark() if callable(watermark) else None
        mark = canonical(mark) if mark is not None else None
        kept = (held or {}).get(name) or {}
        # Per relation (a source that keeps change marks: the Portal mirror's counters):
        # a relation whose mark is the one held reads the same rows, so its print is
        # read back and only the others are fingerprinted.
        relation_marks = getattr(source, "relation_marks", None)
        per = relation_marks(sorted(wanted)) if callable(relation_marks) else None
        per = {r: canonical(m) for r, m in per.items()} if per is not None else None
        if mark is not None and kept.get("mark") == mark and \
                set(wanted) <= set(kept.get("prints") or {}):
            prints = {relation: kept["prints"][relation] for relation in sorted(wanted)}
        else:
            reused = {relation: kept["prints"][relation] for relation in sorted(wanted)
                      if per is not None and relation in (kept.get("prints") or {})
                      and (kept.get("relations") or {}).get(relation) == per[relation]}
            rest = sorted(set(wanted) - set(reused))
            own = getattr(source, "fingerprint", None)
            if not rest:
                prints = {}
            elif callable(own):
                prints = own(rest)
            else:
                prints = {relation: stream_fingerprint(records[relation])
                          for relation in rest if relation in records}
            prints.update(reused)
        if mark is not None or per is not None:
            marks[name] = {"mark": mark, "prints": dict(sorted(prints.items()))}
            if per is not None:
                marks[name]["relations"] = dict(sorted(per.items()))
        # What a source says it does not hold of a relation (``capture_gap``), and the
        # scopes it holds it whole in (``whole_scopes``: what stands in for it there),
        # decide its bindings' rows and gaps, so they are part of its fingerprint.
        gap = getattr(source, "capture_gap", None)
        whole = getattr(source, "whole_scopes", None)
        if callable(gap):
            prints = {relation: _sha(f"{value}\x00{gap(relation)}" + (
                          f"\x00{sorted(whole(relation))}" if callable(whole) else ""))
                      for relation, value in prints.items()}
        by_relation.update({(name, relation): value for relation, value in prints.items()})
        pinned[name] = _Pinned(source, records)
    fingerprints = {bid: by_relation.get((b["source"], b["relation"]))
                    for bid, (_, b) in the_plan.bindings.items()}
    return pinned, fingerprints, marks


def _shard_prints(the_plan: Plan, pinned: Mapping[str, Any],
                  fingerprints: Mapping[str, str | None],
                  held: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """binding -> ``{"shards", "relation", "prints"}`` for each isolated binding cut into
    shards (``shard_count`` of the records naming an object): the fingerprint of the
    records of each shard's objects, with what its source says it does not hold of the
    relation. ``held``: the last build's (same shard code and binding:
    ``build_keys.shard_code_digest``); a binding whose relation fingerprint is the one
    held takes its prints and is not read. The bindings over one
    relation of a source are read in one pass of its records (each record read once, not
    once per binding: a derived relation is read from captured documents)."""
    out: dict[str, dict[str, Any]] = {}
    passes: dict[tuple[str, str], list[str]] = {}
    for bid in sorted(the_plan.isolated):
        tid, b = the_plan.bindings[bid]
        relation, print_ = b["relation"], fingerprints.get(bid)
        source = pinned.get(b["source"])
        if source is None or print_ is None:
            continue
        kept = (held or {}).get(bid) or {}
        if kept.get("relation") == print_ and isinstance(kept.get("prints"), list):
            continue
        passes.setdefault((b["source"], relation), []).append(bid)
    read: dict[str, tuple[list[Any], int]] = {}
    for (name, relation), bids in passes.items():
        source = pinned[name]
        gap = getattr(source, "capture_gap", None)
        whole = getattr(source, "whole_scopes", None)
        state = canonical([gap(relation) if callable(gap) else None,
                           sorted(whole(relation)) if callable(whole) else None])
        reading = []
        for bid in bids:
            tid, b = the_plan.bindings[bid]
            buckets = [hashlib.sha256(state.encode("utf-8")) for _ in range(SHARD_BUCKETS)]
            reading.append((bid, tid, b, buckets, [0]))
        for rec in source.records([relation])[relation]:
            line = None
            for bid, tid, b, buckets, count in reading:
                key = binding_key(b, rec.row)
                if key is None:   # names no object: the binding's own unit (its whole print)
                    continue
                count[0] += 1
                if line is None:
                    line = repr(_fingerprint_cells(rec)).encode("utf-8") + b"\n"
                buckets[object_bucket(object_id_of(tid, bid, key))].update(line)
        for bid, _tid, _b, buckets, count in reading:
            read[bid] = (buckets, count[0])
    for bid in sorted(the_plan.isolated):
        _, b = the_plan.bindings[bid]
        print_ = fingerprints.get(bid)
        if pinned.get(b["source"]) is None or print_ is None:
            continue
        kept = (held or {}).get(bid) or {}
        if bid not in read:   # held: its prints taken, not read
            out[bid] = kept
            continue
        buckets, count = read[bid]
        shards = shard_count(count, kept.get("shards"))
        if shards < 2:
            continue
        width = SHARD_BUCKETS // shards
        out[bid] = {"shards": shards, "relation": print_, "prints": [
            _sha("".join(d.hexdigest() for d in buckets[k * width:(k + 1) * width]))
            for k in range(shards)]}
    return out


def _code_digest(names: Iterable[str]) -> str:
    """The bytes of whole modules (``_RESUME_CODE``: the guard of a kept build, which
    must hold whatever the unit keys leave out)."""
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
    committed) other than ``keep`` and the newest one holding a checkpoint to resume
    (:func:`_held_checkpoint`). Runs under the build lock."""
    held = _held_checkpoint(state)
    for directory in _generations(state):
        if directory.name not in keep and not (directory / "units.json").is_file() \
                and (held is None or directory != held[0]):
            shutil.rmtree(directory, ignore_errors=True)
    # A killed build's objects file not yet moved into its generation (``_Objects``):
    # no build but the one holding the lock writes one.
    for stale in state.glob(".objects-*.sqlite*"):
        stale.unlink(missing_ok=True)


def kept_bytes(state_dir: str | Path) -> int:
    """The bytes of the build kept to resume in ``state_dir`` (0: none): the next build
    resumes it, writing little beside it, or removes it before it writes anything, so a
    disk check before a build counts them as free."""
    held = _held_checkpoint(Path(state_dir))
    if held is None:
        return 0
    total = 0
    for path in held[0].iterdir():
        try:
            total += path.stat().st_size if path.is_file() else 0
        except OSError:
            pass
    return total


def _held_checkpoint(state: Path) -> tuple[Path, dict[str, Any]] | None:
    """The newest unfinished generation directory with a checkpoint whose build file is
    there (``CHECKPOINT``), and the checkpoint; None when there is none."""
    for directory in reversed(_generations(state)):
        if (directory / "units.json").is_file() or not (directory / CHECKPOINT).is_file():
            continue
        try:
            held = json.loads((directory / CHECKPOINT).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(held, dict) and held.get("format") == CHECKPOINT_FORMAT \
                and isinstance(held.get("store"), str) and "/" not in held["store"] \
                and isinstance(held.get("keys"), dict) \
                and isinstance(held.get("units"), dict) \
                and all(name in held["units"] for name in ("watermarks", "shards")) \
                and (directory / held["store"]).is_file():
            return directory, held
    return None


def _drop_older(state: Path, keep: set[str]) -> None:
    """Remove every finished generation not in ``keep``. Runs under the build lock."""
    for directory in _generations(state):
        if directory.name not in keep and (directory / "units.json").is_file():
            shutil.rmtree(directory, ignore_errors=True)


def _store_format(directory: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{directory / 'store.sqlite'}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM store_meta WHERE key = 'format'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _prune(state: Path, current: str) -> None:
    # A generation of another store format is never read again by this code: it goes now,
    # not after KEEP more builds (a format change rebuilds the store at a new size).
    finished = [d for d in _generations(state) if (d / "units.json").is_file()
                and (d.name == current or _store_format(d) == store_ddl.FORMAT)]
    for directory in _generations(state):
        if (directory / "units.json").is_file() and directory not in finished:
            shutil.rmtree(directory, ignore_errors=True)
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


# --------------------------------------------------------------------------- space

_CURRENT_GENERATION = object()

# A cold build with no measured store of this format: what it needs is not known here.
UNKNOWN = None
# Above this changed share a build is cold (module docstring, "Mode").
COLD_SHARE = 0.5


def _cold_work(sizes: Mapping[str, Any]) -> int:
    """The working-file peak of the last cold build: an incremental build's own peak is
    only its changed units' share, so it carries the cold one forward."""
    return int(sizes.get("cold_work_peak") or sizes.get("work_peak") or 0)


def can_clone(directory: str | Path) -> bool:
    """Whether a file in ``directory`` can be cloned (pages shared until written): a probe
    of two small files, removed again."""
    from ontology.store import clone_file
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".clone-probe-{os.getpid()}"
    copy = directory / f".clone-probe-{os.getpid()}.copy"
    try:
        probe.write_bytes(b"\0" * 4096)
        return clone_file(probe, copy)
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)
        copy.unlink(missing_ok=True)


def space_need(state_dir: str | Path, base: Generation | None = _CURRENT_GENERATION, *,
               dirty: Iterable[str] | None = None, units: Mapping[str, Any] | None = None,
               clone: bool | None = None, scale: Mapping[str, float] | None = None,
               share: float | None = None) -> dict[str, Any]:
    """The bytes the next build of ``state_dir`` writes beside what is on disk, from the
    committed generation's measured sizes (``units.json`` ``bytes``):

    * incremental (``dirty``: the changed units, ``units``: the base's units.json): the
      seed (none when the file system clones it, else a byte copy of the base store), plus
      twice the changed units' share of the base store (their rows deleted and written
      again inside one transaction) and that share of the last cold working-file peak
      (``share``: a share measured before, in place of ``dirty``'s);
    * cold: the last measured store of this format plus the last cold working-file peak;
      ``needed_bytes`` None when no store of this format was measured (a format change:
      only the running guard of the caller's ``checkpoint`` bounds it).

    ``mode`` names which; ``seed`` is ``clone``, ``copy`` or None."""
    state = Path(state_dir)
    if base is _CURRENT_GENERATION:
        base = current_generation(state)
    measured = (units if units is not None else
                (_units_of(base) if base is not None else None)) or {}
    sizes = measured.get("bytes") if isinstance(measured.get("bytes"), dict) else {}
    base_bytes = base.store.stat().st_size if base is not None and base.store.is_file() else 0
    work = _cold_work(sizes)
    if (dirty is not None or share is not None) and base is not None:
        cloned = can_clone(state / "generations") if clone is None else clone
        dirty = set(dirty or ())
        fragments = measured.get("fragments") or {}
        keys = measured.get("keys") or {}

        def weight(unit: str) -> int:
            counts = (fragments.get(unit) or {}).get("counts") or {}
            return 1 + int(counts.get("values") or 0) + int(counts.get("objects") or 0)
        total = sum(weight(u) for u in keys) or 1
        # A binding rebuilt object by object (``scale``: its changed objects' share)
        # rewrites that share of its rows.
        if share is None:
            share = min(1.0, sum(weight(u) * (scale or {}).get(u, 1.0) for u in dirty
                                 if u in keys) / total) if keys else 1.0
        seed = 0 if cloned else base_bytes
        needed = seed + int(2 * share * base_bytes) + int(share * work)
        return {"mode": "incremental", "seed": "clone" if cloned else "copy",
                "base_bytes": base_bytes, "changed_share": round(share, 4),
                "needed_bytes": needed}
    store = int(sizes.get("store") or 0) if base is not None else 0
    return {"mode": "cold", "seed": None, "base_bytes": base_bytes,
            "needed_bytes": (store + work) if store else UNKNOWN}


def build_context(normalizer: Normalizer | None, as_known_at: Any = None) -> str:
    """What every unit's key shares (``_build``): the format, the keys' scheme, the rows
    code's version, the cut and the normalizer's code."""
    return _sha(canonical({
        "format": FORMAT, "scheme": build_keys.KEY_SCHEME,
        "rows": build_keys.PROJECTION_ROWS_VERSION,
        "as_known_at": known_at(as_known_at) if as_known_at is not None else None,
        "normalizer": None if normalizer is None else
        f"{getattr(normalizer, '__module__', '')}.{getattr(normalizer, '__qualname__', '')}",
        "normalizer_code": build_keys.normalizer_digest(
            normalizer if normalizer is not None else _default_normalizer())}))


def planned_need(state_dir: str | Path, *, schema: Any = None,
                 schema_digest: str | None = None, normalizer: Normalizer | None = None,
                 as_known_at: Any = None, changed_share: float | None = None
                 ) -> dict[str, Any]:
    """The space estimate of the next build of ``state_dir`` before its captures are
    fingerprinted (hours on a large store): the smallest mode that would run. Cold
    (:func:`space_need`) when only a cold build can run: no generation committed, the
    base's keys of another scheme (naming no commit to adopt), another shared context of
    the keys (:func:`build_context` of ``normalizer``, by default the build's, and
    ``as_known_at``), or another schema (``schema``'s digest, or ``schema_digest``,
    against the store's ``schema_digest`` meta) the base cannot be rewritten under unit by
    unit (``_rewritable``; with only a digest, assumed not). Else, when a ``changed_share``
    measured over the same base exceeds ``COLD_SHARE``, the smaller of the cold estimate
    and the incremental one at that share (a build takes the other mode where only that
    fits); else the least an incremental build writes (its seed). ``predicted`` names the
    reason (None: the least)."""
    state = Path(state_dir)
    context = build_context(normalizer if normalizer is not None else _default_normalizer(),
                            as_known_at)
    base = current_generation(state)
    units = _units_of(base) if base is not None else None
    if schema is not None and schema_digest is None:
        schema_digest = _get(schema, "digest") or None
    reason = None
    if base is None:
        reason = "no_generation"
    elif not units or (units.get("scheme") != build_keys.KEY_SCHEME
                       and not build_adopt.code_sha(units)):
        reason = "key_scheme"
    elif units.get("scheme") == build_keys.KEY_SCHEME and units.get("context") != context:
        reason = "build_code"
    elif schema_digest is not None and _schema_digest(base) not in (None, schema_digest) \
            and not _rewritable(units, schema):
        reason = "schema_changed"
    if reason is not None:
        return {**space_need(state, base, units=units), "predicted": reason}
    if changed_share is not None and changed_share > COLD_SHARE:
        cold = space_need(state, base, units=units)
        partial = space_need(state, base, units=units, share=min(1.0, changed_share))
        smaller = partial if cold["needed_bytes"] is UNKNOWN \
            or partial["needed_bytes"] < cold["needed_bytes"] else cold
        return {**smaller, "predicted": "changed_share",
                "estimates": {"cold": cold, "incremental": partial}}
    return {**space_need(state, base, dirty=(), units=units), "predicted": None}


def _rewritable(units: Mapping[str, Any], schema: Any) -> bool:
    """Whether a base of these ``units`` can be rewritten unit by unit under ``schema``
    (as ``_build`` decides: an acyclic plan, ``_gone_units``); False without a schema."""
    if schema is None or units.get("scheme") != build_keys.KEY_SCHEME:
        return False
    try:
        the_plan = plan(schema)
        if not the_plan.acyclic:
            return False
        shape = build_keys.SchemaParts(schema).shape()
        return _gone_units(units, dict.fromkeys(the_plan.units, ""), the_plan,
                           shape) is not None
    except Exception:  # noqa: BLE001 - unknown: the build may be cold
        return False


def _schema_digest(generation: Generation) -> str | None:
    """The schema digest a generation's store was built under, None when unreadable."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{generation.store}?mode=ro",
                                                uri=True, timeout=5)) as conn:
            row = conn.execute("SELECT value FROM store_meta WHERE key = 'schema_digest'"
                               ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], str) else None


def _choose_mode(state: Path, base: Generation | None, dirty: set[str] | None,
                 units: Mapping[str, Any] | None,
                 space: Callable[[Mapping[str, Any]], None] | None,
                 scale: Mapping[str, float] | None = None) -> dict[str, Any]:
    """The space estimate of the mode this build runs in (module docstring, "Mode"):
    ``dirty`` None = only a cold build is possible. Every estimate is in ``estimates``.
    ``space`` refusing the preferred mode gets the other; its first refusal is raised
    when it refuses every mode."""
    cold = space_need(state, base, units=units)
    choices = [cold]
    if dirty is not None:
        partial = space_need(state, base, dirty=dirty, units=units, scale=scale)
        smaller = cold["needed_bytes"] is UNKNOWN or \
            partial["needed_bytes"] <= cold["needed_bytes"]
        choices = ([partial, cold] if smaller and partial["changed_share"] <= COLD_SHARE
                   else [cold, partial])
    estimates = {c["mode"]: c for c in choices}
    refused: Exception | None = None
    for choice in choices:
        need = {**choice, "estimates": estimates, "preferred": choices[0]["mode"]}
        if space is None:
            return need
        try:
            space(need)
            return need
        except Exception as exc:  # noqa: BLE001 - the guard refused this mode
            refused = refused or exc
    assert refused is not None
    raise refused


# --------------------------------------------------------------------------- rows of a unit

def _prefix(the_plan: Plan, binding: str) -> tuple[str, str]:
    """[low, high) of the object ids of one binding (``<Type>:<binding>:<key>``)."""
    tid = the_plan.bindings[binding][0]
    return f"{tid}:{binding}:", f"{tid}:{binding};"


def _delete_unit(conn: sqlite3.Connection, the_plan: Plan, unit: str,
                 events: Mapping[str, list[tuple[str, Mapping[str, Any]]]]) -> None:
    """Delete one binding unit's rows from a seeded generation (foreign keys deferred:
    the partial build writes the unit's rows again before the commit checks them; a link
    unit's are ``_delete_links``')."""
    where = the_plan.rows[unit]
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
    # Values by their physical rows (v10: object_values is a view over value_cells, whose
    # sources name their version by rowid); the shared fields, kinds, layouts and link
    # texts may stay unreferenced.
    versions = "SELECT rowid FROM object_versions WHERE object_id >= ? AND object_id < ?"
    conn.execute("DELETE FROM value_cells WHERE src IN (SELECT src FROM value_sources "
                 f"WHERE ver IN ({versions}))", (low, high))
    conn.execute(f"DELETE FROM value_sources WHERE ver IN ({versions})", (low, high))
    for table in ("identifiers", "object_versions", "object_removals", "objects"):
        conn.execute(f"DELETE FROM {table} WHERE object_id >= ? AND object_id < ?",
                     (low, high))


# --------------------------------------------------------------------------- build


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
                     on_stage: Callable[[str, int, int], None] | None = None,
                     space: Callable[[Mapping[str, Any]], None] | None = None,
                     workers: int | None = None,
                     timings: BuildTimings | None = None,
                     kept: Callable[[Sequence[Any]], Any] | None = None) -> Generation:
    """Build the next generation of ``state_dir`` from ``sources`` under ``schema`` and
    commit it (``CURRENT``). ``previous``: the generation to reuse unchanged units from
    (default: the committed one; None = a cold build). ``reuse``: when no unit changed
    from ``previous``, return it as it is (module docstring, "Reuse"). ``before_seed``:
    called with the store a partial build is about to copy, before it copies it (a
    caller's disk check: it may raise, and nothing is written). ``on_stage``:
    ``Projector.on_stage`` (measurement only; never in a unit key). ``space``: called with
    the space estimate of a mode (:func:`space_need`, now that the changed units are known;
    ``estimates``: every mode's) before anything is written; it may raise (a caller's disk
    guard), and the build then tries the other mode (module docstring, "Mode"). Every other
    argument is ``build_store``'s. ``workers``: the processes a build projects with
    (``ontology.project_parallel``: a cold build's units, an incremental build's changed
    ones; None: ``PLX_BUILD_WORKERS`` or the CPUs, and serial under ``MIN_CAPTURE_BYTES``
    of captures; 1: serial). ``timings``: where each step's start, end and seconds go as
    the build runs (``ontology.build_timings``; a fresh one by default: the generation's
    ``seconds``). ``kept``: called with the sources' blob directories, the context the
    build reads and keeps file readings, member readings and document fields in (default:
    the ones kept in ``state_dir``; a row-delta cycle keeps its own in place,
    ``ontology.delta.kept``). Raises
    ``ProjectionError("generation_build_in_progress")`` while another build holds the
    state directory; on any failure the committed generation
    is left as it was."""
    state = Path(state_dir)
    (state / "generations").mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timings = timings if timings is not None else BuildTimings()
    with _Lock(state):
        current = current_generation(state)
        base = current if previous is _CURRENT_GENERATION else previous
        _sweep(state, {g.id for g in (current, base) if g is not None})
        # Before the build lays down its new store, drop every finished generation but the
        # committed one and the base (a reader holding an earlier one open keeps its file
        # until it closes it): at most two generations are on disk while it builds.
        _drop_older(state, {g.id for g in (current, base) if g is not None})
        options = {"normalizer": normalizer if normalizer is not None else
                   _default_normalizer(), "as_known_at": as_known_at, "clock": clock,
                   "batch_size": batch_size, "post_project": post_project,
                   "checkpoint": checkpoint, "on_stage": on_stage, "workers": workers}
        # The readings of captured files (PDF text and the like) are kept beside the
        # generations, so a build reads only the files no build read before
        # (``integrations.captured_text.KeptReadings``).
        # Documents are parsed by worker processes once the build engages them (``_build``:
        # a build that projects in parallel by choice; ``project_parallel.DocumentPool``).
        # So are the readings of containers' members (a mail's or a zip's files:
        # ``integrations.kept_members``), unless a primer holds them
        # (``ontology.member_primer``); served while their container is in a source's blobs.
        blobs = [s.blobs for s in sources.values() if isinstance(getattr(s, "blobs", None),
                                                                 (str, Path))]
        with contextlib.ExitStack() as stores:
            if kept is None:
                stores.enter_context(kept_readings(state / READINGS))
                stores.enter_context(kept_members(state / MEMBERS, blobs))
                stores.enter_context(kept_fields(state / FIELDS, derivation_code()))
            else:
                stores.enter_context(kept(blobs))
            parse = stores.enter_context(_document_pool(workers))
            generation = _build(state, schema, sources, base, reuse=reuse,
                                before_seed=before_seed, space=space, parse=parse,
                                timings=timings, **options)
        if generation.mode != "reused":
            laid = time.monotonic()
            timings.begin("prune")
            _prune(state, generation.id)
            timings["prune"] = round(time.monotonic() - laid, 3)
    timings["total"] = round(time.monotonic() - started, 3)
    return replace(generation, seconds=dict(timings))


def _build(state: Path, schema: Any, sources: Mapping[str, CaptureSource],
           base: Generation | None, *, normalizer: Normalizer | None, as_known_at: Any,
           clock: Callable[[], datetime] | None, batch_size: int,
           post_project: Any, checkpoint: Callable[[], None] | None,
           on_stage: Callable[[str, int, int], None] | None = None,
           reuse: bool = False,
           before_seed: Callable[[Path], None] | None = None,
           space: Callable[[Mapping[str, Any]], None] | None = None,
           workers: int | None = None, parse: Any = None,
           timings: BuildTimings | None = None) -> Generation:
    started = time.monotonic()
    seconds = timings if timings is not None else BuildTimings()
    seconds.begin("fingerprint")
    the_plan = plan(schema)
    # What every unit's rows share: the scheme and version of the rows code
    # (``ontology.build_keys``), the cut and the normalizer's code. The schema and the
    # captures enter each unit's key by its own slice and fingerprints only.
    context = build_context(normalizer, as_known_at)
    units = _units_of(base) if base is not None else None
    # A base of the earlier keys whose commit the operator names is adopted when every
    # part is proven (``ontology.build_adopt``), else the build is cold as before.
    adoption, adopted = None, None
    if units is not None and units.get("scheme") is None and build_adopt.code_sha(units):
        try:
            adoption = build_adopt.prepare(
                units, context=context, report=base.report, state=state, sources=sources,
                normalizer=normalizer, fmt=FORMAT, plan=plan,
                cut=known_at(as_known_at) if as_known_at is not None else None)
            units = adoption.units
        except build_adopt.NotAdopted as why:
            adopted = {"code_sha": build_adopt.code_sha(units), "refused": str(why)}
    ours = units is not None and units.get("scheme") == build_keys.KEY_SCHEME
    # Held fingerprints: a source's when the code that read and fingerprinted it is the
    # same; a sharded binding's when its binding and the shard code are (``build_keys``).
    readers = {name: build_keys.reader_digest(source) for name, source in sources.items()}
    held_readers = (units.get("readers") or {}) if ours else {}
    held_marks = {name: mark for name, mark in (units.get("watermarks") or {}).items()
                  if readers.get(name) is not None and held_readers.get(name) == readers[name]
                  } if ours else None
    # A build kept to resume (``_resume``) of this code holds the newest fingerprints.
    kept_build = _held_checkpoint(state)
    code = _code_digest(list(_RESUME_CODE) + [type(s).__module__ for s in sources.values()]
                        + [getattr(normalizer, "__module__", "")])
    resumes = kept_build is not None and kept_build[1].get("context") == context \
        and kept_build[1].get("code") == code
    # The relation fingerprints and shard prints are computed by worker processes when
    # the build's are engaged (``ontology.build_prints``: the same functions, the same
    # values); the pool is closed before the projection.
    ahead = build_prints.ahead_marks(sources, the_plan) if parse is not None else {}
    prints_pool = build_prints.PrintPool(_parallel_count(workers, ahead)) \
        if parse is not None else None
    try:
        pinned, captured, marks = build_prints.pinned(
            _pin, sources, the_plan,
            kept_build[1]["units"]["watermarks"] if resumes else held_marks,
            prints_pool, ahead)
    except BaseException:
        if prints_pool is not None:
            prints_pool.close()
        raise
    # A source's reader shapes its records beyond the captured bytes it fingerprints
    # (citations, audiences, keys, removals, which company's records stand in): its code
    # is part of each of its relations' fingerprints (``build_keys.with_reader``). One
    # whose code cannot be read is never equal (a token of this build).
    reader_of = {name: digest if digest is not None else f"unread:{secrets.token_hex(16)}"
                 for name, digest in readers.items()}
    fingerprints = {bid: build_keys.with_reader(print_, reader_of.get(b["source"]))
                    for bid, (_, b) in the_plan.bindings.items()
                    for print_ in [captured.get(bid)]}
    seconds["fingerprint"] = round(time.monotonic() - started, 3)
    if parse is not None:
        parse.engaged = _parallel_count(workers, marks) > 1
        relations: dict[str, set[str]] = {}
        for _, b in the_plan.bindings.values():
            relations.setdefault(b["source"], set()).add(b["relation"])
        parse.know({name: pinned[name] for name in relations if name in pinned}, marks,
                   relations)
    started = time.monotonic()
    seconds.begin("fingerprint.shards")
    shard_code = build_keys.shard_code_digest()

    def spec(bid: str) -> str:
        return _sha(canonical(list(the_plan.bindings[bid])))
    held_shards = {bid: entry for bid, entry in (units.get("shards") or {}).items()
                   if bid in the_plan.bindings and isinstance(entry, Mapping)
                   and entry.get("spec") == spec(bid)} \
        if ours and shard_code is not None and units.get("shard_code") == shard_code else None
    try:
        sharded = build_prints.shard_prints(
            _shard_prints, the_plan, pinned, fingerprints,
            kept_build[1]["units"]["shards"] if resumes else held_shards, prints_pool,
            marks) if the_plan.acyclic else {}
    finally:
        if prints_pool is not None:
            prints_pool.close()
    sharded = {bid: {**entry, "spec": spec(bid)} for bid, entry in sharded.items()}
    the_plan = with_shards(the_plan, {b: s["shards"] for b, s in sharded.items()})
    if parse is not None:   # its workers' memory is the projection's until the steps
        parse.close()
    seconds["fingerprint.shards"] = round(time.monotonic() - started, 3)
    started = time.monotonic()
    schema_parts = build_keys.SchemaParts(schema)
    slices = {unit: build_keys.unit_slice(schema_parts, unit, deps)
              for unit, deps in the_plan.units.items()}
    keys = {unit: _sha(context + canonical({
                "schema": slices[unit], "prints": {b: fingerprints.get(b) for b in sorted(deps)}}))
            for unit, deps in the_plan.units.items()}
    for bid, shards in sharded.items():
        keys[binding_unit(bid)] = _sha(context + canonical(
            {"schema": slices[binding_unit(bid)], bid: fingerprints.get(bid),
             "shards": shards["shards"]}))
        for k, print_ in enumerate(shards["prints"]):
            keys[shard_unit(bid, k)] = _sha(context + canonical(
                {"schema": slices[shard_unit(bid, k)], "binding": bid,
                 "shards": shards["shards"], "shard": k, "print": print_,
                 "reader": reader_of.get(the_plan.bindings[bid][1]["source"])}))
    if adoption is not None:
        units = {**units, "keys": adoption.keys(context, keys, slices, the_plan.units,
                                                captured, sharded)}
        adopted = {"code_sha": adoption.sha, "kept": adoption.kept, "of": len(units["keys"])}
    shape = schema_parts.shape()
    gone = _gone_units(units, keys, the_plan, shape) \
        if the_plan.acyclic and ours and units.get("context") == context else None
    reusable = gone is not None
    if gone:   # their rows are deleted from the seed like a changed unit's
        the_plan = replace(the_plan, rows={**the_plan.rows, **gone})
    dirty = {u for u in keys if units["keys"].get(u) != keys[u]} if reusable else set(keys)
    if reuse and reusable and not dirty and not gone:
        return replace(base, mode="reused", units=len(keys), units_rebuilt=0, bindings_read=0,
                       seconds=seconds)
    if kept_build is not None:
        resumed = _resume(state, kept_build, schema, pinned, context, code, keys, base,
                          sharded, seconds,
                          normalizer=normalizer, as_known_at=as_known_at,
                          batch_size=batch_size, post_project=post_project, clock=clock,
                          workers=workers)
        if resumed is not None:
            return resumed
    incremental = reusable and len(dirty) < len(keys)
    read_from = time.monotonic()
    seconds.begin("fingerprint.objects")
    local = object_local(schema, the_plan)
    sidecar = _Objects(state / f".objects-{secrets.token_hex(8)}.sqlite") if local else None
    try:
        routed, delta = _object_plan(
            sidecar, the_plan, local, pinned, slices, keys, units if reusable else None,
            base if reusable else None, dirty, incremental, context, reader_of,
            serial=_parallel_count(workers, marks) < 2)
        seconds["fingerprint.objects"] = round(time.monotonic() - read_from, 3)
        need = _choose_mode(state, base, dirty if incremental else None, units, space,
                            scale={binding_unit(b): n / max(1, routed[b]["objects"])
                                   for b, n in delta.items()})
    except BaseException:
        _discard(sidecar)
        raise
    if adopted is not None:   # how a base of the earlier keys was taken, or why not
        need = {**need, "adopted": adopted}
    if need["mode"] == "cold":   # every unit projected again, into a new store
        incremental, dirty = False, set(keys)
        for bid in delta:   # rebuilt whole, each object's part kept
            routed[bid]["mode"] = "whole"
        delta = {}
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
    try:
        directory.mkdir()
    except BaseException:
        _discard(sidecar)
        raise
    seconds.attach(directory)
    try:
        objects: dict[str, frozenset[str] | None] = {
            bid: (sidecar.changed_ids(bid) if bid in delta else None)
            for bid, how in routed.items() if how["mode"] in ("delta", "whole")}
        projector = Projector(schema, pinned, normalizer=normalizer, as_known_at=as_known_at,
                              batch_size=batch_size, scratch_dir=directory, scope=scope,
                              emit=dirty if incremental else None,
                              shards={b: s["shards"] for b, s in sharded.items()},
                              objects=objects)
        if sidecar is not None:
            projector.object_parts = sidecar.part
        projector.checkpoint = checkpoint
        work_peak = [0]

        def stage(name: str, work: int, store: int) -> None:
            work_peak[0] = max(work_peak[0], work)
            if on_stage is not None:
                on_stage(name, work, store)
        projector.on_stage = stage
        laid = time.monotonic()
        seconds.begin("project.seed")
        # (the builder whose digest of the segments it wrote is made by the build's
        # workers, ``ontology.finish_digest``)
        with ParallelBuilder(directory / "store.sqlite",
                             seed=base.store if incremental else None) as builder:
            seed = builder.seed_mode
            seconds["project.seed"] = round(time.monotonic() - laid, 3)
            builder.seconds = seconds.prefixed("finish.")
            builder.digest_workers = _digest_workers(workers)
            open_generation(builder)
            parallel = None
            # The projector of what is projected in this process (every unit of a serial
            # build; an incremental build's units no worker projected).
            serial: Projector | None = projector
            if incremental:
                # The changed units projected by worker processes (``_parallel_units``),
                # chosen before the seed is cut: their merge needs the seed's foreign
                # keys off from here on (the commit checks every key).
                chosen = _parallel_units(the_plan, dirty, objects, workers, marks)
                if chosen:
                    _foreign_keys(builder.connection, on=False)
                laid = time.monotonic()
                seconds.begin("project.seed_delete")
                _delta_objects(builder.connection, sidecar, delta)
                _identifier_rows(builder.connection, the_plan, dirty, "before", delta)
                _seed_delete(builder.connection, the_plan, schema, dirty | set(gone or ()),
                             delta)
                seconds["project.seed_delete"] = round(time.monotonic() - laid, 3)
                if chosen:
                    parallel = _project_parallel(
                        builder, schema, pinned, marks,
                        replace(the_plan, units={u: the_plan.units[u] for u in chosen},
                                rows={u: the_plan.rows[u] for u in chosen}),
                        sharded, units, workers, directory, seconds,
                        normalizer=normalizer, as_known_at=as_known_at,
                        batch_size=batch_size, checkpoint=checkpoint, objects=objects,
                        object_parts=sidecar.part if sidecar is not None else None)
                    _foreign_keys(builder.connection, on=True)
                    rest = set(dirty) - (parallel["result"].done
                                         if parallel is not None and "result" in parallel
                                         else set())
                    if not rest:
                        serial = None
                    elif rest != set(dirty):
                        serial = Projector(
                            schema, pinned, normalizer=normalizer, as_known_at=as_known_at,
                            batch_size=batch_size, scratch_dir=directory,
                            scope=set().union(*(the_plan.units[u] for u in rest)),
                            emit=rest, shards={b: s["shards"] for b, s in sharded.items()},
                            objects=objects)
                        serial.object_parts = projector.object_parts
                        serial.checkpoint, serial.on_stage = checkpoint, stage
            else:
                parallel = _project_parallel(builder, schema, pinned, marks, the_plan,
                                             sharded, units, workers, directory, seconds,
                                             normalizer=normalizer, as_known_at=as_known_at,
                                             batch_size=batch_size, checkpoint=checkpoint)
                if parallel is not None and "result" in parallel:
                    serial = None
            if parallel is not None and "result" in parallel:
                work_peak[0] = parallel["result"].work_peak
            if serial is not None:
                laid = time.monotonic()
                if parallel is not None:
                    seconds.begin("project.serial")
                serial.project_into(builder)
                if parallel is not None:
                    seconds["project.serial"] = round(time.monotonic() - laid, 3)
            if incremental:
                laid = time.monotonic()
                seconds.begin("project.changed_keys")
                _identifier_rows(builder.connection, the_plan, dirty, "after", delta)
                builder.connection.execute("DROP TABLE IF EXISTS temp.plx_delta_objects")
                _changed_keys(builder.connection)
                seconds["project.changed_keys"] = round(time.monotonic() - laid, 3)
            # A token only this build holds names it to the steps' memos; the next build
            # reads it in units.json, so nothing a build that never committed derived
            # passes for derived against a committed generation.
            token = f"{gen_id}-{secrets.token_hex(8)}"
            # A cold build over a committed generation compares the two generations'
            # identifier rows instead, so the steps' memos hold for it as well.
            against = units.get("build_token") if incremental else None
            if not incremental and units is not None and units.get("build_token"):
                laid = time.monotonic()
                if _changed_keys_against(builder.connection, base.store):
                    against = units["build_token"]
                seconds["project.changed_keys"] = round(time.monotonic() - laid, 3)
            projector.build_context = BuildContext(state, against, token, builder.connection)
            if against is not None and not incremental:
                projector.build_context.base_store = base.store
            # The parts of the units each projector emitted (the one in this process also
            # reports on the units of what it staged: a worker's own part is theirs).
            projected = {**(parallel["result"].parts if parallel and "result" in parallel
                            else {}),
                         **({u: p.as_dict() for u, p in serial.report.parts.items()
                             if serial.emit is None or u in serial.emit}
                            if serial is not None else {})}
            laid = time.monotonic()
            seconds.begin("project.objects")
            # (an incremental build's workers hand on their objects' parts; a cold one's
            # keep none)
            projected = _object_parts(sidecar, routed, delta, projected, directory,
                                      parallel=bool(parallel and "result" in parallel
                                                    and not incremental))
            seconds["project.objects"] = round(time.monotonic() - laid, 3)
            # An entry reported outside every unit (a hook naming none) is in no part, so
            # no mode could report it alike: every mode leaves it out and counts it, and
            # the build stays incremental (nothing in a unit's rows depends on it).
            unattributed = (serial.report.unattributed if serial is not None else 0) + (
                parallel["result"].unattributed if parallel and "result" in parallel else 0)
            parts = {unit: projected.get(unit, _EMPTY)
                     if unit in dirty else units["fragments"].get(unit, _EMPTY)
                     for unit in sorted(keys)}
            # A cold build reports what it projected; an incremental one merges the parts
            # of its units (equal for a cold build: every entry is in exactly one part,
            # and samples are order-independent). A parallel build has only its units'
            # parts (every entry is in one), and a cold one reports them too (not an entry
            # outside them); a cyclic lineage has no units, so it reports what it projected.
            merged = incremental or the_plan.acyclic or \
                (parallel is not None and "result" in parallel)
            report = merge_reports(parts.values()) if merged else projector.report.as_dict()
            seconds["project"] = round(time.monotonic() - started, 3)
            laid = time.monotonic()
            seconds.begin("project.counts")
            counted = projected_counts(builder, report)
            seconds["project.counts"] = round(time.monotonic() - laid, 3)
            held = {
                "format": CHECKPOINT_FORMAT, "id": token, "context": context, "code": code,
                "keys": keys,
                "base": base.id if base is not None else None, "incremental": incremental,
                "store": builder.temp.name, "report": report,
                "counts": counted,
                "units": {"context": context,
                          # The keys' scheme and what a later build must share to
                          # rewrite unit by unit (``build_keys``): each binding's type,
                          # each event type; the code each source's held fingerprints and
                          # the shard prints were read with; report entries left out as
                          # outside every unit (``Generation.unattributed``).
                          "scheme": build_keys.KEY_SCHEME, "shape": shape,
                          "readers": {name: digest for name, digest in sorted(readers.items())
                                      if digest is not None},
                          "shard_code": shard_code, "unattributed": unattributed,
                          "fragments": {u: p for u, p in parts.items() if p != _EMPTY},
                          "watermarks": marks, "shards": sharded,
                          # How each object-local binding was built (``_object_plan``).
                          "objects": routed,
                          "parallel": _parallel_record(parallel, units, incremental),
                          "bytes": {"work_peak": work_peak[0],
                                    "mode": "incremental" if incremental else "cold",
                                    "cold_work_peak": _cold_work(units.get("bytes") or {})
                                    if incremental else work_peak[0]}},
                "generation": {"mode": ("incremental" if incremental else
                                        "cold" if the_plan.acyclic else "cold_cyclic_lineage"),
                               "units": len(keys), "units_rebuilt": len(dirty),
                               "bindings_read": len(scope) if scope is not None
                               else len(the_plan.bindings), "seed": seed, "need": need,
                               "unattributed": unattributed}}
            laid = time.monotonic()
            seconds.begin("project.checkpoint")
            if the_plan.acyclic and builder.keep_to_resume({"id": token, "steps": {}}):
                _write_json(directory / CHECKPOINT, held)
            seconds["project.checkpoint"] = round(time.monotonic() - laid, 3)
            generation = _finish(state, directory, builder, projector, held, token,
                                 post_project=post_project, clock=clock, seconds=seconds,
                                 started=time.monotonic())
    except BaseException:
        _discard(sidecar)
        if not _resumable(directory):
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return generation


def _discard(sidecar: _Objects | None) -> None:
    """Close and remove a build's objects file not moved into its generation."""
    if sidecar is None:
        return
    with contextlib.suppress(sqlite3.Error):
        sidecar.close()
    sidecar.path.unlink(missing_ok=True)


def _object_plan(sidecar: _Objects | None, the_plan: Plan, local: frozenset[str],
                 pinned: Mapping[str, Any],
                 slices: Mapping[str, Any], keys: Mapping[str, str],
                 units: Mapping[str, Any] | None, base: Generation | None,
                 dirty: set[str], incremental: bool, context: str,
                 reader_of: Mapping[str, str], *, serial: bool
                 ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """How each object-local binding is built (``units.json`` ``objects``): ``carried``
    (its unit unchanged: the base's objects file rows for it kept), ``delta`` (only its
    changed objects' rows rebuilt), ``whole`` (every object rebuilt, each one's part
    kept for the next build) or ``plain`` (rebuilt whole without parts: too small, or a
    cold build whose projection runs in worker processes). Returns that and binding ->
    its changed objects for the ``delta`` ones (``temp.changed`` of ``sidecar``).

    A binding is rebuilt object by object only when the base's objects file holds it
    for the base's own unit key and this build's basis (``_Objects``) and at most half
    its objects changed; anything else is rebuilt whole (fail closed)."""
    routed: dict[str, dict[str, Any]] = {}
    delta: dict[str, int] = {}
    if sidecar is None:
        return routed, delta
    code = build_keys.object_code_digest()
    if code is None:   # code that cannot be read decides nothing: every binding plain
        return {bid: {"mode": "plain"} for bid in sorted(local)
                if binding_unit(bid) in dirty}, delta
    sidecar.attach(base.store.parent / OBJECTS if base is not None else None)
    held_keys = (units or {}).get("keys") or {}
    bases: dict[str, str] = {}
    to_read: list[str] = []
    for bid in sorted(local):
        b = the_plan.bindings[bid][1]
        source = pinned.get(b["source"])
        if source is None:
            continue
        gap = getattr(source, "capture_gap", None)
        whole = getattr(source, "whole_scopes", None)
        bases[bid] = _sha(context + canonical({
            "schema": slices[binding_unit(bid)], "binding": bid,
            "reader": reader_of.get(b["source"]), "code": code,
            "state": [gap(b["relation"]) if callable(gap) else None,
                      sorted(whole(b["relation"])) if callable(whole) else None]}))
        unit = binding_unit(bid)
        if unit not in dirty:
            if sidecar.held(bid, held_keys.get(unit), bases[bid]):
                sidecar.carry(bid, keys[unit], bases[bid])
                routed[bid] = {"mode": "carried"}
            continue
        to_read.append(bid)
    if not to_read or not (incremental or serial):
        for bid in to_read:
            routed[bid] = {"mode": "plain"}
        return routed, delta
    counts = sidecar.read(pinned, the_plan, to_read)
    for bid in to_read:
        unit = binding_unit(bid)
        if counts[bid] < OBJECT_RECORDS:
            routed[bid] = {"mode": "plain", "records": counts[bid]}
            continue
        count = sidecar.prints(bid, bases[bid], keys[unit])
        routed[bid] = {"mode": "whole", "records": counts[bid], "objects": count}
        if incremental and sidecar.held(bid, held_keys.get(unit), bases[bid]):
            changed = sidecar.changed(bid)
            if 2 * changed <= count:
                routed[bid].update(mode="delta", changed=changed)
                delta[bid] = changed
    return routed, delta


def _delta_objects(conn: sqlite3.Connection, sidecar: _Objects | None,
                   delta: Mapping[str, int]) -> None:
    """``temp.plx_delta_objects``: the changed objects of every binding rebuilt object
    by object, in the generation being built (``_seed_delete``, ``_identifier_rows``)."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS plx_delta_objects "
                 "(object_id TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.execute("DELETE FROM temp.plx_delta_objects")
    for bid in sorted(delta):
        ids = sorted(sidecar.changed_ids(bid))
        for at in range(0, len(ids), DEFAULT_BATCH):
            conn.executemany("INSERT INTO temp.plx_delta_objects VALUES (?)",
                             [(oid,) for oid in ids[at:at + DEFAULT_BATCH]])


def _object_parts(sidecar: _Objects | None, routed: dict[str, dict[str, Any]],
                  delta: Mapping[str, int], projected: Mapping[str, Any], directory: Path,
                  *, parallel: bool) -> dict[str, Any]:
    """The projected parts with each object-local binding built object by object given
    its whole part (what it reported as a whole merged with its objects' parts), and the
    objects file moved into the generation's ``directory`` (none when it holds nothing).
    A projection by worker processes kept no object part: its bindings hold none."""
    if sidecar is None:
        return dict(projected)
    out = dict(projected)
    for bid, how in sorted(routed.items()):
        if how["mode"] not in ("delta", "whole"):
            continue
        if parallel:
            sidecar.drop(bid)
            how["mode"] = "plain"
            continue
        out[binding_unit(bid)] = sidecar.finish(bid, bid in delta,
                                                projected.get(binding_unit(bid), _EMPTY))
    held = sidecar.conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0]
    sidecar.close()
    if held:
        os.replace(sidecar.path, directory / OBJECTS)
    else:
        sidecar.path.unlink(missing_ok=True)
    return out


def _resumable(directory: Path) -> bool:
    """Whether a failed build's directory holds a checkpoint to resume (kept)."""
    try:
        held = json.loads((directory / CHECKPOINT).read_text(encoding="utf-8"))
        return (not (directory / "units.json").is_file()
                and (directory / held["store"]).is_file())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _finish(state: Path, directory: Path, builder: StoreBuilder, projector: Projector,
            held: Mapping[str, Any], token: str, *, post_project: Any,
            clock: Callable[[], datetime] | None, seconds: dict[str, float],
            started: float, stepped: Mapping[str, Any] | None = None) -> Generation:
    """Run the steps not done of a projected build (``held``: its checkpoint; ``builder``
    holds its rows; every step done is a checkpoint), commit its store and the generation:
    ``units.json``, then ``CURRENT``. ``stepped``: the report of a resumed build whose
    rows were checked and committed (only digested and swapped in here)."""
    done = dict(builder.checkpoint.get("steps") or {})
    if isinstance(seconds, BuildTimings):
        seconds.begin("finish")

    def after_step(name: str, step: Mapping[str, Any]) -> None:
        done[name] = dict(step)
        if builder.kept:
            laid = time.monotonic()
            builder.keep_to_resume({"id": builder.checkpoint["id"], "steps": done})
            seconds[f"finish.{name}.checkpoint"] = round(time.monotonic() - laid, 3)

    committed = False
    try:
        if stepped is not None:
            digest = builder.digest_and_swap()
            report = {**stepped, "content_digest": digest, "store": str(builder.target),
                      "format": store_ddl.FORMAT}
        else:
            report = finish_generation(
                builder, projector, held["report"], post_project=_timed(post_project, seconds),
                clock=clock, counts=held["counts"], steps_done=done, after_step=after_step)
        committed = True
    finally:
        projector.build_context.close(committed=committed)
    digested = {"mode": builder.digest_mode, "segments": builder.digest_segments}
    seconds.update({f"finish.{name}": value for name, value in builder.seconds.items()
                    if seconds.get(f"finish.{name}") != value})
    seconds["finish"] = round(time.monotonic() - started, 3)
    kept = dict(held["units"])
    _write_json(directory / "units.json", {
        "format": FORMAT, "content_digest": report["content_digest"],
        "context": kept["context"],
        # The keys' scheme, shape, readers' and shard code, entries outside every unit
        # (``_build``).
        **{name: kept.get(name) for name in ("scheme", "shape", "readers", "shard_code",
                                             "unattributed")},
        "keys": held["keys"], "fragments": kept["fragments"],
        # Each source's watermark and the fingerprints read under it (``_pin``).
        "watermarks": kept["watermarks"],
        # Each sharded binding's shard count and shard fingerprints (``_shard_prints``).
        "shards": kept["shards"],
        # How each object-local binding was built (``_object_plan``).
        "objects": kept.get("objects") or {},
        # The token this build's step memos are valid under (``BuildContext``).
        "build_token": token,
        # How the projection ran: workers and each task's seconds (the next cold
        # build starts the longest first), or why it ran serially.
        "parallel": kept["parallel"],
        # Measured, for the next build's space estimate (``space_need``).
        "bytes": {"store": (directory / "store.sqlite").stat().st_size, **kept["bytes"]}})
    (directory / CHECKPOINT).unlink(missing_ok=True)
    built = held["generation"]
    _write_json(state / CURRENT, {
        "format": FORMAT, "generation": directory.name,
        "content_digest": report["content_digest"], "mode": built["mode"],
        "committed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
    return Generation(directory.name, directory / "store.sqlite", report["content_digest"],
                      report, mode=built["mode"], units=built["units"],
                      units_rebuilt=built["units_rebuilt"],
                      bindings_read=built["bindings_read"], seconds=seconds,
                      seed=built["seed"], need=built["need"], digest=digested,
                      unattributed=int(built.get("unattributed") or 0))


def _resume(state: Path, held: tuple[Path, dict[str, Any]], schema: Any,
            pinned: Mapping[str, Any], context: str, code: str, keys: Mapping[str, str],
            base: Generation | None, sharded: Mapping[str, Any], seconds: dict[str, float], *,
            normalizer: Any, as_known_at: Any, batch_size: int, post_project: Any,
            clock: Callable[[], datetime] | None, workers: int | None) -> Generation | None:
    """Go on with the build kept in ``held`` (a checkpoint and its directory) when its unit
    keys, context, projection code (``_RESUME_CODE``) and base generation are this build's;
    else (or when its file holds no
    checkpoint) remove it and return None (the caller builds as before)."""
    directory, checkpoint = held
    base_id = base.id if base is not None else None
    if checkpoint.get("context") != context or checkpoint.get("code") != code \
            or checkpoint.get("keys") != dict(keys) \
            or checkpoint.get("base") != base_id:
        shutil.rmtree(directory, ignore_errors=True)
        return None
    try:
        builder, marked = ParallelBuilder.reopen(directory / checkpoint["store"],
                                                 directory / "store.sqlite",
                                                 seeded=bool(checkpoint.get("incremental")))
    except (StoreError, sqlite3.Error):
        shutil.rmtree(directory, ignore_errors=True)
        return None
    if marked.get("id") != checkpoint["id"]:
        builder.discard()
        shutil.rmtree(directory, ignore_errors=True)
        return None
    started = time.monotonic()
    if isinstance(seconds, BuildTimings):
        seconds.attach(directory)
    seconds["resumed"] = 1
    try:
        with builder:
            if isinstance(seconds, BuildTimings):
                builder.seconds = seconds.prefixed("finish.")
            builder.digest_workers = _digest_workers(workers)
            open_generation(builder)
            projector = Projector(schema, pinned, normalizer=normalizer,
                                  as_known_at=as_known_at, batch_size=batch_size,
                                  scratch_dir=directory,
                                  shards={b: s["shards"] for b, s in sharded.items()})
            token = f"{directory.name}-{secrets.token_hex(8)}"
            # The changed identifier keys of the first attempt are not kept: every key
            # may have changed (``BuildContext`` base None).
            projector.build_context = BuildContext(state, None, token, builder.connection)
            stepped = None
            if marked.get("stepped"):
                stepped = json.loads(builder.connection.execute(
                    "SELECT value FROM store_meta WHERE key = 'build_report'").fetchone()[0])
            return _finish(state, directory, builder, projector, checkpoint, token,
                           post_project=post_project, clock=clock, seconds=seconds,
                           started=started, stepped=stepped)
    except BaseException:
        if not _resumable(directory):
            shutil.rmtree(directory, ignore_errors=True)
        raise


def _gone_units(units: Mapping[str, Any], keys: Mapping[str, str], the_plan: Plan,
                shape: Mapping[str, Any]) -> dict[str, tuple] | None:
    """The base's units this schema no longer has, with where their rows are, when the
    base can be rewritten unit by unit; None when it cannot (the build is cold): a binding
    of the base gone or of another type now (its rows are under that type's ids), an event
    type of the base gone or changed (its events are found by its definition), or a unit
    gone whose rows cannot be found. A shard of a binding still here goes with its
    binding's unit (changed whenever the shards do); a link binding's unit holding only
    gaps has no rows."""
    held = units.get("shape")
    if not isinstance(held, Mapping) or not isinstance(units.get("keys"), Mapping):
        return None
    bindings, events = shape["bindings"], shape["events"]
    if any(bindings.get(bid) != tid for bid, tid in (held.get("bindings") or {}).items()):
        return None
    if any(events.get(eid) != digest for eid, digest in (held.get("events") or {}).items()):
        return None
    out: dict[str, tuple] = {}
    for unit in units["keys"]:
        if unit in keys:
            continue
        if unit.startswith("b:") and "#" in unit and \
                unit[2:].rpartition("#")[0] in the_plan.bindings:
            continue
        if unit.startswith("l:") and "|" not in unit:
            continue
        parts = unit[2:].split("|") if unit.startswith("l:") else []
        if len(parts) == 4 and parts[1] in the_plan.bindings and parts[2] in the_plan.bindings \
                and parts[3] in _PROJECTED_LINK_KINDS:
            out[unit] = ("links", *parts)
            continue
        return None
    return out


def _project_parallel(builder: StoreBuilder, schema: Any, pinned: Mapping[str, Any],
                      marks: Mapping[str, Any], the_plan: Plan,
                      sharded: Mapping[str, Any], units: Mapping[str, Any] | None,
                      workers: int | None, directory: Path, seconds: dict[str, float], *,
                      normalizer: Any, as_known_at: Any, batch_size: int,
                      checkpoint: Callable[[], None] | None,
                      objects: Mapping[str, frozenset[str] | None] | None = None,
                      object_parts: Callable[[str, str, Any], None] | None = None
                      ) -> dict[str, Any] | None:
    """A build's projection by worker processes (``ontology.project_parallel``) of the
    units of ``the_plan``: ``{"result": ...}`` when it projected them into ``builder``
    (an incremental build's: those of ``result.done``); ``{"unavailable": reason}`` when
    it could not (``builder`` then holds no row of them and the caller projects them
    serially); None when the build is serial by choice (one worker, or small captures
    with no count asked for). A seeded ``builder`` (an incremental build) has its foreign
    keys off (``_foreign_keys``) and every unit of ``the_plan`` deleted from its seed."""
    from ontology import project_parallel as par

    count = _parallel_count(workers, marks)
    if count < 2:
        return None
    hints = ((units or {}).get("parallel") or {}).get("seconds") or {}
    started = time.monotonic()
    if isinstance(seconds, BuildTimings):
        seconds.begin("project.parallel")
    try:
        result = par.project(builder, schema=schema, sources=pinned, marks=marks,
                             the_plan=the_plan, normalizer=normalizer,
                             as_known_at=as_known_at, batch_size=batch_size,
                             shards={b: s["shards"] for b, s in sharded.items()},
                             scratch_dir=directory, count=count, hints=hints,
                             checkpoint=checkpoint, objects=objects,
                             object_parts=object_parts)
    except par.ParallelUnavailable as why:
        if builder.seed_mode is None:
            builder.restart()
        return {"unavailable": str(why)}
    seconds["project.parallel"] = round(time.monotonic() - started, 3)
    seconds["project.parallel_merge"] = result.merge_seconds
    return {"result": result}


def _parallel_units(the_plan: Plan, dirty: Iterable[str],
                    objects: Mapping[str, frozenset[str] | None],
                    workers: int | None, marks: Mapping[str, Any]) -> set[str]:
    """The changed units an incremental build projects by worker processes, into parts
    merged into its seed (``ontology.project_parallel``): every one but a ``same_record``
    link unit whose ends' rows are not all projected again (it compares the values stored
    for both ends, ``Projector._conflicts``, and a worker's part holds only its task's
    rows; the build's own process projects it after the merge, from the seed). Empty
    (every unit projected in the build's process) when the build is serial
    (``_parallel_count``) or its units make fewer than two tasks."""
    from ontology import project_parallel as par

    dirty = set(dirty)
    count = _parallel_count(workers, marks)
    if count < 2 or not the_plan.acyclic:
        return set()
    held: dict[str, set[str]] = {}   # binding -> its units holding rows
    for unit, row in the_plan.rows.items():
        if row[0] in ("binding", "shard"):
            held.setdefault(row[1], set()).add(unit)
    chosen = set()
    for unit in sorted(dirty):
        row = the_plan.rows[unit]
        if row[0] == "links" and row[4] == "same_record" and not all(
                objects.get(end) is None and held.get(end, set()) | {binding_unit(end)}
                <= dirty for end in (row[2], row[3])):
            continue
        # A shard is cut with its binding's own unit (``project_parallel.tasks``; a changed
        # shard changes its binding's print, so that unit is changed too)
        if row[0] == "shard" and binding_unit(row[1]) not in dirty:
            continue
        chosen.add(unit)
    cut = par.tasks(replace(the_plan, units={u: the_plan.units[u] for u in chosen},
                            rows={u: the_plan.rows[u] for u in chosen}), par.pieces(count))
    return chosen if len(cut) > 1 else set()


def _foreign_keys(conn: sqlite3.Connection, *, on: bool) -> None:
    """Commit a seeded generation's open transaction and open the next with its foreign
    keys enforced (``on``: deferred to its commit, as ``_seed_delete`` defers them) or
    not (the parallel merge of an incremental build: ``StoreBuilder.check`` checks every
    key before the generation is committed). The build's file is its own until then: a
    build that fails before its first checkpoint removes it. So while the keys are off
    (the seed's deletes and the merge, which run before any checkpoint) the file keeps no
    rollback journal: a failure there discards it, and on the live 126 GB store the
    journal of the deletes alone passed 60 GB (every page they change written twice). The
    journal is back (``DELETE``) before the keys are on again."""
    if conn.in_transaction:
        conn.execute("COMMIT")
    mode = "DELETE" if on else "OFF"
    if conn.execute(f"PRAGMA journal_mode = {mode}").fetchone()[0].upper() != mode:
        raise StoreError("journal_mode_unchanged")
    conn.execute(f"PRAGMA foreign_keys = {'ON' if on else 'OFF'}")
    conn.execute("BEGIN IMMEDIATE")
    if on:
        conn.execute("PRAGMA defer_foreign_keys = ON")


def _parallel_count(workers: int | None, marks: Mapping[str, Any]) -> int:
    """The worker processes a build projects and parses with
    (``ontology.project_parallel.workers``); 1 (serial) when no count is asked for
    (``workers`` or ``PLX_BUILD_WORKERS``) and its captures are small
    (``MIN_CAPTURE_BYTES``): starting the workers costs more than they save."""
    from ontology import project_parallel as par

    count = par.workers(workers)
    if workers is None and os.environ.get(par.WORKERS_ENV) in (None, "") \
            and par.capture_bytes(marks) < par.MIN_CAPTURE_BYTES:
        return 1
    return count


@contextlib.contextmanager
def _document_pool(workers: int | None) -> Iterator[Any]:
    """``project_parallel.document_pool`` with the build's worker count; None when that
    count is refused (the projection refuses it first) or one."""
    from ontology import project_parallel as par

    count = _digest_workers(workers)
    if count < 2:
        yield None
        return
    with par.document_pool(count) as pool:
        yield pool


def _digest_workers(workers: int | None) -> int:
    """The processes a full content digest at the commit may use: the build's worker
    count (``ontology.project_parallel.workers``); one when that count is refused (a cold
    build's projection refuses it first)."""
    from ontology import project_parallel as par

    try:
        return par.workers(workers)
    except ProjectionError:
        return 1


def _parallel_record(parallel: dict[str, Any] | None, units: Mapping[str, Any] | None,
                     incremental: bool = False) -> dict[str, Any]:
    """``units.json`` ``parallel``: how the projection ran and each task's seconds (the
    next build's order). An incremental build keeps the last build's seconds and adds
    those of tasks it is the first to measure."""
    held = ((units or {}).get("parallel") or {}).get("seconds") or {}
    if parallel is None:   # serial by choice: hints kept
        return {"seconds": held}
    if "unavailable" in parallel:
        return {"unavailable": parallel["unavailable"], **({"seconds": held}
                                                          if incremental else {})}
    result = parallel["result"]
    seconds = {**result.seconds, **held} if incremental else result.seconds
    out: dict[str, Any] = {"workers": result.workers, "seconds": dict(sorted(seconds.items()))}
    if result.unavailable is not None:
        out["unavailable"] = result.unavailable
    return out


class BuildContext:
    """What a ``post_project`` step of a generation build may know of it (the projector's
    ``build_context``): the state directory and the build tokens (``units.json``
    ``build_token``) of the committed generation it is compared with (``base``: the one an
    incremental build started from, or the one a cold build compared its identifier rows
    with, ``_changed_keys_against``; None: none, as a build with no generation before or a
    resumed one) and of the one it builds. ``changed_key``: whether an identifier key's
    index rows (with their object's type and version digest and markings, what a mention
    resolves against) may differ from the base's; every key when there is no base."""

    def __init__(self, state: Path, base: str | None, generation: str,
                 conn: sqlite3.Connection) -> None:
        self.state, self.base, self.generation = state, base, generation
        # A cold build's base generation's store (read-only), whose document-step rows a
        # step may take (``ontology.text_index``); None otherwise.
        self.base_store: Path | None = None
        self._conn = conn
        self._memo: Any = None

    def changed_key(self, kind: str, normalized: str) -> bool:
        if self.base is None:
            return True
        return self._conn.execute(
            "SELECT 1 FROM temp.plx_changed_keys WHERE identifier_kind = ? AND "
            "normalized = ?", (kind, normalized)).fetchone() is not None

    def document_memo(self) -> Any:
        """The document steps' memo of this state directory (``ontology.mention_memo``),
        one for the build."""
        if self._memo is None:
            from ontology.mention_memo import FILE, DocumentMemo
            self._memo = DocumentMemo(self.state / FILE, self.base, self.generation,
                                      int(self.generation.split("-")[0]))
        return self._memo

    def close(self, *, committed: bool) -> None:
        """Close the memo; a committed build drops the entries no recent build used."""
        if self._memo is not None:
            self._memo.close(prune=committed)
            self._memo = None


# The identifier rows a mention resolves against (``ontology.mentions._store_candidates``).
_CANDIDATE_ROWS = (
    "SELECT i.identifier_kind, i.normalized, i.object_id, o.object_type, i.property, i.role, "
    "i.l1_handle, i.field_path, i.lineage, i.recorded_at, i.superseded_at, v.digest, "
    "v.markings FROM identifiers i JOIN objects o ON o.object_id = i.object_id "
    "JOIN object_versions v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle "
    "WHERE i.object_id >= ? AND i.object_id < ?")


def _identifier_rows(conn: sqlite3.Connection, the_plan: Plan, dirty: Iterable[str],
                     when: str, delta: Iterable[str] = ()) -> None:
    """Keep the identifier rows of the objects of the ``dirty`` units (``when``:
    ``before`` their rows are deleted from the seed, ``after`` they are projected
    again) in ``temp.plx_ident_<when>``: only those can differ between the base and
    this generation (no other unit's rows are written)."""
    conn.create_function("plx_shard", 2, shard_of, deterministic=True)
    table = f"temp.plx_ident_{when}"
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} AS {_CANDIDATE_ROWS} AND 0", ("", ""))
    shards: dict[str, list[int]] = {}
    delta = set(delta)
    for unit in sorted(dirty):
        where = the_plan.rows[unit]
        if where[0] == "binding" and where[1] in delta:   # its changed objects' only
            conn.execute(f"INSERT INTO {table} {_CANDIDATE_ROWS} AND i.object_id IN "
                         "(SELECT object_id FROM temp.plx_delta_objects)",
                         _prefix(the_plan, where[1]))
        elif where[0] == "binding":
            conn.execute(f"INSERT INTO {table} {_CANDIDATE_ROWS}",
                         _prefix(the_plan, where[1]))
        elif where[0] == "shard":
            shards.setdefault(where[1], []).append(where[3])
    for binding, chosen in sorted(shards.items()):
        count = the_plan.rows[shard_unit(binding, chosen[0])][2]
        conn.execute(f"INSERT INTO {table} {_CANDIDATE_ROWS} AND plx_shard(i.object_id, ?) "
                     "IN (SELECT value FROM json_each(?))",
                     (*_prefix(the_plan, binding), count, json.dumps(sorted(chosen))))


def _changed_keys(conn: sqlite3.Connection) -> None:
    """``temp.plx_changed_keys``: the identifier keys of the rows kept before and after
    that differ (``_identifier_rows``)."""
    conn.execute("DROP TABLE IF EXISTS temp.plx_changed_keys")
    conn.execute("CREATE TABLE temp.plx_changed_keys (identifier_kind TEXT, normalized TEXT, "
                 "PRIMARY KEY (identifier_kind, normalized)) WITHOUT ROWID")
    conn.execute(
        "INSERT OR IGNORE INTO temp.plx_changed_keys SELECT identifier_kind, normalized FROM "
        "(SELECT * FROM temp.plx_ident_before EXCEPT SELECT * FROM temp.plx_ident_after) "
        "UNION SELECT identifier_kind, normalized FROM "
        "(SELECT * FROM temp.plx_ident_after EXCEPT SELECT * FROM temp.plx_ident_before)")
    conn.execute("DROP TABLE temp.plx_ident_before")
    conn.execute("DROP TABLE temp.plx_ident_after")


# The identifier rows a mention resolves against (``_CANDIDATE_ROWS``) of a whole store,
# in key order (the identifiers' primary key).
_ALL_CANDIDATE_ROWS = (
    "SELECT i.identifier_kind, i.normalized, i.object_id, o.object_type, i.property, i.role, "
    "i.l1_handle, i.field_path, i.lineage, i.recorded_at, i.superseded_at, v.digest, "
    "v.markings FROM identifiers i JOIN objects o ON o.object_id = i.object_id "
    "JOIN object_versions v ON v.object_id = i.object_id AND v.l1_handle = i.l1_handle "
    "ORDER BY i.identifier_kind, i.normalized, i.object_id, i.l1_handle, i.field_path")


def _key_groups(conn: sqlite3.Connection) -> Iterator[tuple[tuple[str, str], list[tuple]]]:
    """``((kind, normalized), rows)`` of every identifier key of the store, in key order."""
    cursor = conn.execute(_ALL_CANDIDATE_ROWS)
    key: tuple[str, str] | None = None
    rows: list[tuple] = []
    while True:
        batch = cursor.fetchmany(DEFAULT_BATCH)
        if not batch:
            break
        for row in batch:
            if (row[0], row[1]) != key:
                if key is not None:
                    yield key, rows
                key, rows = (row[0], row[1]), []
            rows.append(tuple(row))
    if key is not None:
        yield key, rows


def _changed_keys_against(conn: sqlite3.Connection, base_store: Path) -> bool:
    """``temp.plx_changed_keys`` of a build that did not start from ``base_store`` (a cold
    one): every identifier key whose rows (``_CANDIDATE_ROWS``) differ between that
    committed generation and the one in ``conn``, in one ordered pass over both. False,
    with no table, when the base cannot be read (every key may then have changed)."""
    conn.execute("DROP TABLE IF EXISTS temp.plx_changed_keys")
    conn.execute("CREATE TABLE temp.plx_changed_keys (identifier_kind TEXT, normalized TEXT, "
                 "PRIMARY KEY (identifier_kind, normalized)) WITHOUT ROWID")
    changed: list[tuple[str, str]] = []

    def flush(force: bool = False) -> None:
        if changed and (force or len(changed) >= DEFAULT_BATCH):
            conn.executemany("INSERT OR IGNORE INTO temp.plx_changed_keys VALUES (?, ?)",
                             changed)
            changed.clear()
    try:
        with contextlib.closing(sqlite3.connect(f"file:{base_store}?mode=ro", uri=True)) as old:
            before, after = _key_groups(old), _key_groups(conn)
            a, b = next(before, None), next(after, None)
            while a is not None or b is not None:
                if b is None or (a is not None and a[0] < b[0]):
                    changed.append(a[0])
                    a = next(before, None)
                elif a is None or b[0] < a[0]:
                    changed.append(b[0])
                    b = next(after, None)
                else:
                    if a[1] != b[1]:
                        changed.append(a[0])
                    a, b = next(before, None), next(after, None)
                flush()
        flush(force=True)
    except sqlite3.Error:
        conn.execute("DROP TABLE IF EXISTS temp.plx_changed_keys")
        return False
    return True


def _timed(steps: Mapping[str, Callable[..., Any]] | None, seconds: dict[str, float]
           ) -> dict[str, Callable[..., Any]] | None:
    """``steps``, each timing itself into ``seconds`` (``finish.<step>``; measurement only,
    never in the report)."""
    if steps is None:
        return None

    def timed(name: str, step: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any) -> Any:
            started = time.monotonic()
            if isinstance(seconds, BuildTimings):
                seconds.begin(f"finish.{name}")
            try:
                return step(*args)
            finally:
                seconds[f"finish.{name}"] = round(time.monotonic() - started, 3)
        return run
    return {name: timed(name, step) for name, step in steps.items()}


def _seed_delete(conn: sqlite3.Connection, the_plan: Plan, schema: Any,
                 dirty: Iterable[str], delta: Iterable[str] = ()) -> None:
    """Remove from the seeded copy every row a partial build writes again: the rows of
    the changed units and every link and value no unit owns (the ``post_project`` steps
    write those again). An object-local binding of ``delta`` loses only the rows of its
    changed objects (``temp.plx_delta_objects``)."""
    delta = set(delta)
    conn.execute("PRAGMA defer_foreign_keys = ON")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS plx_unit_events "
                 "(event_id TEXT PRIMARY KEY)")
    events: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for eid, ev in sorted((_get(schema, "event_types") or {}).items()):
        events.setdefault(ev["object_type"], []).append((eid, ev))
    kinds = ", ".join(f"'{kind}'" for kind in _PROJECTED_LINK_KINDS)
    conn.execute(f"DELETE FROM link_rows WHERE binding_kind NOT IN ({kinds})")
    # The post_project values, and then their sources left without a value (a source is
    # only ever written with one, so no other source is without one).
    posted = ("FROM value_cells WHERE field IS NOT NULL AND layout IN "
              "(SELECT lf.layout FROM value_layout_fields lf JOIN value_kinds k "
              "ON k.kind = lf.kind WHERE k.parser = ?)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS plx_posted (src INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp.plx_posted")
    conn.execute(f"INSERT OR IGNORE INTO temp.plx_posted SELECT src {posted}", (POST_PROJECT,))
    conn.execute(f"DELETE {posted}", (POST_PROJECT,))
    conn.execute("DELETE FROM value_sources WHERE src IN (SELECT src FROM temp.plx_posted) "
                 "AND NOT EXISTS (SELECT 1 FROM value_cells c WHERE c.src = value_sources.src)")
    conn.execute("DROP TABLE temp.plx_posted")
    shards: dict[str, set[int]] = {}
    links: list[tuple[Any, ...]] = []
    with _binding_segments(conn) as mark:
        for unit in sorted(dirty):
            if the_plan.rows[unit][0] == "shard":
                _, bid, count, k = the_plan.rows[unit]
                shards.setdefault(bid, set()).add(k)
                continue
            if the_plan.rows[unit][0] == "binding" and the_plan.rows[unit][1] in delta:
                low, high = _prefix(the_plan, the_plan.rows[unit][1])
                if conn.execute("SELECT 1 FROM temp.plx_delta_objects WHERE object_id >= ? "
                                "AND object_id < ? LIMIT 1", (low, high)).fetchone():
                    mark(low)
                _delete_objects(conn, the_plan, the_plan.rows[unit][1], events)
                continue
            if the_plan.rows[unit][0] == "binding":
                mark(_prefix(the_plan, the_plan.rows[unit][1])[0])
            if the_plan.rows[unit][0] == "links":
                links.append(the_plan.rows[unit])
                continue
            _delete_unit(conn, the_plan, unit, events)
        conn.execute("DROP TABLE temp.plx_unit_events")
        _delete_links(conn, the_plan, links)
        for bid, chosen in sorted(shards.items()):
            mark(_prefix(the_plan, bid)[0])
            count = the_plan.rows[shard_unit(bid, min(chosen))][2]
            _delete_shards(conn, the_plan, bid, count, chosen)


def _delete_links(conn: sqlite3.Connection, the_plan: Plan,
                  units: Iterable[tuple[Any, ...]]) -> None:
    """Delete the links of the link ``units`` (their ``Plan.rows``: link type, from and to
    binding, binding kind) from a seeded generation: the rows ``_delete_unit`` deletes for
    each, in one read of the links from each from binding's objects (``links_from``)
    instead of one per unit."""
    by_from: dict[str, list[tuple[str, str, str, str]]] = {}
    for _, lid, frm, to, kind in units:
        by_from.setdefault(frm, []).append((lid, kind, *_prefix(the_plan, to)))
    if not by_from:
        return
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS plx_link_units (link_type TEXT NOT NULL, "
                 "binding_kind TEXT NOT NULL, to_low TEXT NOT NULL, to_high TEXT NOT NULL)")
    for frm, wanted in sorted(by_from.items()):
        conn.execute("DELETE FROM temp.plx_link_units")
        conn.executemany("INSERT INTO temp.plx_link_units VALUES (?, ?, ?, ?)", sorted(wanted))
        conn.execute("DELETE FROM link_rows WHERE from_object >= ? AND from_object < ? AND "
                     "EXISTS (SELECT 1 FROM temp.plx_link_units u WHERE "
                     "u.link_type = link_rows.link_type AND "
                     "u.binding_kind = link_rows.binding_kind AND "
                     "link_rows.to_object >= u.to_low AND link_rows.to_object < u.to_high)",
                     _prefix(the_plan, frm))
    conn.execute("DROP TABLE temp.plx_link_units")


# The tables whose rows of a binding the seed's deletes remove by the binding's id range
# (``_delete_unit``, ``_delete_objects``, ``_delete_shards``): every such row is in the
# binding's digest segment.
_BINDING_TABLES = ("value_cells", "value_sources", "identifiers", "object_versions",
                   "object_removals", "objects")


@contextlib.contextmanager
def _binding_segments(conn: sqlite3.Connection) -> Iterator[Callable[[str], None]]:
    """``mark(low)``: record in ``temp.plx_dirty`` the digest segment of the binding whose
    object ids start with ``low`` (``_prefix``), whose rows are about to be deleted. A
    seeded builder's delete triggers on ``_BINDING_TABLES`` (``store._DIRTY_TRIGGERS``)
    record that segment row by row, a value's by looking up its source and version (most
    of the deletes' time); they are dropped meanwhile and created again after. A mark
    without a deleted row only digests that segment again (the same parts). An id prefix
    that is not its own segment (a ':' in the type or binding) marks every segment. A
    builder without the triggers (an unseeded one, or a seed whose parts do not compose
    its digest) marks nothing."""
    names = [f"plx_dirty_{table}_delete" for table in _BINDING_TABLES]
    held = {name for (name,) in conn.execute(
        "SELECT name FROM sqlite_temp_master WHERE type = 'trigger' AND name IN "
        f"({', '.join('?' for _ in names)})", names)}
    if not held:
        yield lambda low: None
        return
    statements = [sql for sql in _DIRTY_TRIGGERS
                  if any(f" {name} " in sql for name in held)]
    if len(held) != len(names) or len(statements) != len(names):
        raise StoreError("dirty_triggers_unexpected")

    def mark(low: str) -> None:
        segment = object_segment(low)
        conn.execute("INSERT OR IGNORE INTO temp.plx_dirty VALUES (?, ?)",
                     ("object_id", segment) if segment == low else ("all", ""))
    for name in sorted(held):
        conn.execute(f"DROP TRIGGER temp.{name}")
    try:
        yield mark
    finally:
        for sql in statements:
            conn.execute(sql)


def _delete_objects(conn: sqlite3.Connection, the_plan: Plan, binding: str,
                    events: Mapping[str, list[tuple[str, Mapping[str, Any]]]]) -> None:
    """Delete the rows of the changed objects of an object-local ``binding``
    (``temp.plx_delta_objects``) from a seeded generation: their objects, versions, values,
    identifiers, removals and the events their versions yield (each qualifying only its
    own object). No other unit's row names them but the links of the link units reading
    the binding, which are rebuilt whole (``object_local``)."""
    low, high = _prefix(the_plan, binding)
    chosen = "SELECT object_id FROM temp.plx_delta_objects WHERE object_id >= ? " \
        "AND object_id < ?"
    parts = [(eid, ev["observed_property"] if ev["time_basis"] == "capture_observed"
              else ev["time"]["path"])
             for eid, ev in events.get(the_plan.bindings[binding][0], [])
             if ev["time_basis"] == "capture_observed" or ev["time"]["binding"] == binding]
    if parts:
        conn.execute("DELETE FROM temp.plx_unit_events")
        cursor = conn.execute("SELECT object_id, l1_handle FROM object_versions "
                              f"WHERE object_id IN ({chosen})", (low, high))
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
    versions = f"SELECT rowid FROM object_versions WHERE object_id IN ({chosen})"
    conn.execute("DELETE FROM value_cells WHERE src IN (SELECT src FROM value_sources "
                 f"WHERE ver IN ({versions}))", (low, high))
    conn.execute(f"DELETE FROM value_sources WHERE ver IN ({versions})", (low, high))
    for table in ("identifiers", "object_versions", "object_removals", "objects"):
        conn.execute(f"DELETE FROM {table} WHERE object_id IN ({chosen})", (low, high))


def _delete_shards(conn: sqlite3.Connection, the_plan: Plan, binding: str, count: int,
                   chosen: set[int]) -> None:
    """Delete the rows of the objects of ``binding`` in the ``chosen`` of its ``count``
    shards from a seeded generation (an isolated binding: no event, link or other
    binding's row names them)."""
    low, high = _prefix(the_plan, binding)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS plx_shard_objects "
                 "(object_id TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.execute("DELETE FROM temp.plx_shard_objects")
    cursor = conn.execute("SELECT object_id FROM objects WHERE object_id >= ? AND "
                          "object_id < ?", (low, high))
    while True:
        rows = cursor.fetchmany(DEFAULT_BATCH)
        if not rows:
            break
        conn.executemany("INSERT INTO temp.plx_shard_objects VALUES (?)",
                         [row for row in rows if shard_of(row[0], count) in chosen])
    objects = "SELECT object_id FROM temp.plx_shard_objects"
    versions = f"SELECT rowid FROM object_versions WHERE object_id IN ({objects})"
    conn.execute("DELETE FROM value_cells WHERE src IN (SELECT src FROM value_sources "
                 f"WHERE ver IN ({versions}))")
    conn.execute(f"DELETE FROM value_sources WHERE ver IN ({versions})")
    for table in ("identifiers", "object_versions", "object_removals", "objects"):
        conn.execute(f"DELETE FROM {table} WHERE object_id IN ({objects})")
    conn.execute("DROP TABLE temp.plx_shard_objects")


__all__ = ["CURRENT", "FORMAT", "Generation", "KEEP", "Plan", "UNKNOWN", "build_generation",
           "build_context", "can_clone", "current_generation", "kept_bytes", "plan",
           "planned_need", "prune_to_current",
           "space_need"]
