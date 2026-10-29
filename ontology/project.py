"""Project cited L1 captures into the L2 ontology store, driven by the schema (L7).

The projection is disposable: it
reads only L1 captures (never a source system), copies nothing uncited and is rebuilt
whole into a new store generation that is swapped in atomically
(``ontology.store.StoreBuilder``).

Input
-----
* A compiled schema (``ontology.schema.CompiledSchema`` from the L4 loader, or any
  object with ``object_types``, ``link_types``, ``event_types`` and ``digest``).
* One :class:`CaptureSource` per binding source family. :class:`MirrorCaptures` reads
  the Portal source mirror (``integrations/source_mirror.py``): every retained version
  of every row (``row_current`` and ``row_version``), cited exactly as the mirror cites
  it (``make_citation``: ``api://<relation>/<key>@<generation>-<capture>``), with the
  completion time of the capture that first held that content (``recorded_at``) and of
  the capture that replaced it (``superseded_at``). :class:`MailCaptures` and
  :class:`SharePointCaptures` read the versioned mail and SharePoint captures and
  ``ontology.project_bc.BcCaptures`` the versioned BC capture store,
  ``ontology.project_mc.McCaptures`` the Mission Control capture and
  ``ontology.project_graph.TeamsCaptures`` the Teams capture. A source may tell,
  per relation, what it does not hold (``capture_gap``): reported at each binding of that
  relation, and ``source_not_captured`` bindings are not projected. A source whose
  records each belong to one capture scope (BC: a company) may also tell which scopes it
  holds a relation whole in (``whole_scopes``) and the scope of a record (``scope_of``).

What is built
-------------
* **Objects** (identity only): one per source record of a binding (``<Type>:<binding>:
  <key JSON>``). Records are never merged. A binding key that names more than one
  source record is not projected (reported): identity is never guessed. A binding that
  ``stands_for`` another one of its type (BC: an order the posted invoices and shipments
  still name after BC deleted it) projects one object per company and key that no record
  of that other binding (nor of one listed before it) holds, cited to the first-recorded
  record naming the key: one identity per key, and none while the source does not hold
  the stood-for relation whole in the record's scope (``whole_scopes``; without it, in
  every scope).
* **Versions**: one per captured version, with lineage from the binding's declared
  rules (first match; predicates may test the record one declared N:1/1:1 hop away),
  else ``inherit`` (the lineage of the record that hop reaches), else ``default``; BC
  bindings take ``bc_<company>`` from the capture. Version ranges never overlap.
* **Values**: per property source, parsed only by the declared deterministic parser,
  cited by the version's L1 handle and the field path ``row.<path>`` (``rows.<n>.<path>``
  for an equal-key run), so ``operating/claim_checks.py`` checks them unchanged. Units and
  currencies come from the source's declaration, else the property's (constant,
  ``*_from`` a path or a one-hop ``via_link`` path, or ``*_unknown`` = NULL).
  ``authority_rank`` is the position of the first matching precedence rule (lineage,
  optional binding and era); values are never merged and a disagreement is reported,
  never resolved.
* **Links**: from declared link bindings (``fk``, ``json_path``, ``same_record``,
  ``text_match``; ``via`` join records), compared by the pair's declared mode
  (``integer_id``, ``text_id``, ``text``, ``identifier``; an identifier held in one source
  family is compared in the form the other side's source receives it, by the grammar
  carrier's ``sent_to`` rule: a Portal product code meets a BC item No. cut to 20
  characters, and a value never sent there joins nothing). Every link records ``reason``
  (the agreeing features with their cited values) and ``why_not`` (failed, unknown or
  capping features, near misses, property conflicts). Tier = the binding's ceiling,
  lowered: a free-text compare never reaches above ``stored_by_text_join`` (and only
  ``lead`` between two anchor records, which are never joined on text); an unknown
  pair, or an N:1/1:1 link whose from-record matches several records, is a ``lead``;
  typed values that differ make it ``contradicted``. ``mention`` and ``reviewed`` links
  come from other modules (documents, review decisions) and are not projected here.
  **BC company scope**: BC document and master numbers are unique only within one
  company (book), so a hop whose two ends are both BC captures
  (``ontology.schema.link_company_scope``) pairs records only when their capture
  company (``bc_<book>``) is equal; a match across companies is no link (counted as
  ``links.cross_company_not_linked``). A BC capture without a company has no lineage
  the store can hold, so it is not projected at all (gap ``bc_company_unknown`` at its
  binding) and is never paired. A Portal record does not serve the BC company
  it was posted to, so a hop between a BC capture and another source that reaches the
  BC record on a number, not on its key (``ontology.schema.bc_company_not_known``), is
  capped at ``lead`` (``tier_cap`` ``bc_company_not_known``) in every company that
  answers. An ``ambiguous_target`` note counts candidates of one company only and
  never names another: a stored note holds nothing a reader of one company may not see.
* **Identifier index**: every identifier-typed value with its raw text (role
  ``primary`` when the property is in a unique identity key, else ``alias``).
* **Events**: declared date columns (one per version that holds a parseable date) and
  ``capture_observed`` changes of a property between consecutive captures (window =
  last sighting of the old content .. first capture of the new one).

Markings: a version carries its type's markings; a value its type's plus its
property's; a link its type's plus both endpoint types' plus those of the properties
read by its pairs; an event its type's, its object type's and its time property's.

``build_store(path, schema, sources)`` returns a report (counts, gaps, conflicts and
the store's content digest); the same report is in ``store_meta.build_report``. A gap
carries its count and its three smallest sample handles, so the report does not depend
on the order records are visited in; a ``source_condition_unmet`` gap also records every
handle it was reported on (``RECORDED_GAPS``), kept out of the report in
``store_meta.source_condition_unmet`` (``Type.property@binding`` -> sorted handles), so a
reader decides a record's unmet ``when`` from the build, never re-deciding it on stored
values. ``ontology.project_incremental.build_generation``
builds the same generation incrementally: every gap and count is attributed to a build
unit (``binding_unit``, ``link_unit``) and a partial :class:`Projector` (``scope``,
``emit``) projects only the units whose captures changed.

Memory: the build streams (decision projection-memory, option a; see
:class:`Projector`). Capture rows are read lazily per binding, every intermediate
(versions, compare keys, link candidates, links) lives in an on-disk working file next
to the store, and store rows are written in batches; the result is byte-identical for
any ``batch_size``. A synthetic 114,500-object build over the curated schema peaked at
167 MB RSS (2,769 MB for the earlier all-in-memory build), with the same digest.

Stdlib only. Imports nothing from the legacy ontology store.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping as _MappingBase
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from itertools import groupby
import math
import os
from pathlib import Path
import pickle
import re
import secrets
import sqlite3
import tempfile
import time
import unicodedata
import weakref
import zlib
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import unquote

from integrations.capture_store import bc_blank_date
from ontology import identifiers, store_ddl
from ontology.schema import (BC_COMPANY_NOT_KNOWN, SAME_BC_COMPANY, bc_company_not_known,
                             link_company_scope)
from ontology.store import (_TABLE_ORDER, StoreBuilder, StoreError, canonical, instant,
                            known_at, markings_text)
from ontology.units import (currency_code, currency_shape, is_unit_price, js_trim, money_basis,
                            same_denominator, unit_token)

PROJECTOR = "plx-ontology-projector/v1"
# The parser of a value a ``post_project`` step writes (``_format.schema.json``): the
# projector skips its sources; the step writes it, and an incremental build drops it from
# the seeded generation so the step writes it again.
POST_PROJECT = "post_project"
# Relations one freshness entry lists at most (contract $defs/freshness.relations).
MAX_FRESHNESS_RELATIONS = 32
Normalizer = Callable[[str, Any], "str | None"]

_TIERS = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead",
          "contradicted")
# Tiers at which a link may carry derived facts (event attachment, lineage, units).
_STORED_KEY_TIERS = ("verified", "reviewed", "corroborated")
_PROJECTED_LINK_KINDS = ("fk", "json_path", "same_record", "text_match")
# Portal gold relations the silver-to-gold ETL re-inserts on every run under a fresh
# ``cuid()`` id (``ON CONFLICT (id)`` never fires, no DELETE, no natural unique
# constraint): source -> relation -> the columns the ETL mints per insert. No served
# column holds the silver source key, so a record's natural key is every other column of
# its row (``_natural_key``): copies of one silver row are equal on all of them. At
# projection time each binding over one of these relations keeps ONE object per natural
# key, audience and source presence (a copy removed from the source keeps its removal):
# the copy inserted first (smallest ``created_at``, then smallest object id), which a
# later run never displaces (the ETL only adds copies, each newer). Every other copy is
# dropped from the projection, reported (gap ``etl_duplicate_collapsed`` with its handle,
# count ``objects.etl_duplicates_collapsed``) and stays readable at L1. Two genuine rows
# equal on every column but the minted ones collapse too: indistinguishable in the served
# columns, and these raw relations are never the authority (their FM/silver counterparts
# are). Deterministic: the choice depends only on the captured rows.
ETL_REINSERTED: Mapping[str, Mapping[str, frozenset[str]]] = {
    "portal_api": {relation: frozenset({"id", "created_at", "updated_at"}) for relation in (
        "public.app_assembly_order_damage", "public.app_assembly_order_status",
        "public.app_batch_schedule", "public.app_manufacturer",
        "public.app_manufacturing_order_line", "public.app_production_task")}}
# The working file's layout. Bumped when what one build writes beside its store changes
# (2: the record kept once in ``stage``, compressed with a per-binding dictionary;
# ``v`` points at it), so a disk peak
# measured under an older layout is not taken as this one's need
# (scripts/ontology_projection_probe.py ``previous_peak``).
WORK_LAYOUT = 2
# The phases of ``Projector.project_into``, in order: ``on_stage`` names each when it ends.
STAGES = ("load", "links", "lineage", "versions", "values", "conflicts", "events",
          "links_written")
# Sorts after every ``instant`` text: the keep order of a row without a usable created_at.
_LAST = "\uffff"
_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")
_INTEGRAL_TEXT = re.compile(r"^\+?\d+(?:\.0*)?$")
_NUMERIC_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:$|[T ])")
_TIMESTAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})"
                        r"(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}(?::?\d{2})?)?$")
_YYYYMMDD = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_BC_LINEAGE = re.compile(r"^bc_[a-z0-9][a-z0-9_]{0,47}$")
_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_FALSE = frozenset({"false", "f", "no", "n", "0"})
_SAMPLES = 3


# Characters an object id's native key never holds as is ($defs/object_id: no whitespace
# and no invisible character): whitespace (a space-padded Sage code, a composite
# _sage_pk), control and format characters (zero-width, bidi marks and overrides, BOM:
# Unicode categories Cc and Cf) and '%' itself, so the key reads back exactly with
# urllib.parse.unquote (native_key_of).
_NATIVE_ESCAPE = re.compile(r"[\s%\x00-\x1f\x7f]|[^\x00-\x7f]")


def _native_escape(match: re.Match[str]) -> str:
    char = match.group()
    if char.isascii() or char.isspace() or unicodedata.category(char) in ("Cc", "Cf"):
        return "".join(f"%{b:02X}" for b in char.encode())
    return char


def object_id_of(type_id: str, binding_id: str, key: Mapping[str, Any]) -> str:
    """``<Type>:<binding id>:<native key>``: the canonical JSON of the binding key values,
    whitespace, control and format characters and '%' percent-encoded (UTF-8), so a real
    key with a space still projects an id that is whitespace-free and reversible, and no
    zero-width or bidi character rides an id."""
    native = _NATIVE_ESCAPE.sub(_native_escape, canonical(key))
    return f"{type_id}:{binding_id}:{native}"


def _natural_key(row: Any, minted: frozenset[str], own: str) -> tuple[str, str]:
    """(natural key, keep order) of a row of an ETL re-inserted relation
    (``ETL_REINSERTED``): the SHA-256 of the canonical JSON of every column but the
    ``minted`` ones, and its ``created_at`` text (a row without one sorts last). A row
    that is not a JSON object, or does not serialize, is its own natural key (``own``,
    its record key): it is never collapsed with another."""
    if isinstance(row, Mapping):
        try:
            text = canonical({name: value for name, value in row.items()
                              if name not in minted})
        except (TypeError, ValueError):
            text = None
        if text is not None:
            return ("sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    _keep_order(row.get("created_at")))
    return "record:" + own, _LAST


def _keep_order(created: Any) -> str:
    """A ``created_at`` as fixed-width UTC text (``instant``: the Portal serves its
    timestamps in UTC without a zone, with 0-3 fraction digits), so text order is time
    order; anything else sorts after every time (``_LAST``)."""
    if isinstance(created, str) and created.strip():
        text = created.strip()
        for candidate in (text, text + "Z"):
            try:
                return instant(candidate)
            except StoreError:
                continue
    return _LAST


def native_key_of(object_id: str) -> str:
    """The canonical JSON of the binding key values an object id names (inverse of the
    native part of ``object_id_of``)."""
    return unquote(object_id.split(":", 2)[2])


def _successor_id(type_id: str, binding: Mapping[str, Any], key: Any) -> str | None:
    """The object id a successor's source key names under ``binding``, or None when
    the key is not exactly the binding key."""
    if not isinstance(key, Mapping) or set(key) != set(binding["key"]):
        return None
    return object_id_of(type_id, binding["id"], {path: key[path] for path in binding["key"]})


def _replacement(replacement: Sequence[Any], successor_id: Callable[[Any], str | None]
                 ) -> tuple[str, str] | None:
    """(replaced_basis, replaced_by JSON object ids) of a capture's removal stretch tail
    (``(replaced_basis, [[key, capture id], ...] JSON)``), or None without a label or
    when a successor key names no object of the binding."""
    if not replacement or replacement[0] is None:
        return None
    basis, pairs = replacement
    try:
        keys = [key for key, _ in json.loads(pairs)]
    except (TypeError, ValueError):
        keys = []
    if basis not in store_ddl.REPLACEMENT_BASES or not keys or (
            basis == "source_replacement" and len(keys) != 1):
        raise ProjectionError("capture_removal_invalid", f"{basis}: {pairs!r:.200}")
    ids = [successor_id(key) for key in keys]
    return None if None in ids else (basis, canonical(sorted(ids)))


class ProjectionError(Exception):
    """The projection cannot be built faithfully; ``code`` is stable."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------- L1 input

@dataclass(frozen=True)
class CapturedRecord:
    """One captured version of one source record, as L1 cites it."""

    source: str                       # binding source family (portal_api, bc, ...)
    relation: str                     # binding relation it belongs to
    record_key: str                   # identity of the source record inside the relation
    row: Mapping[str, Any]
    l1_handle: str
    field_prefix: str                 # "row", or "rows.<n>" inside an equal-key run
    digest: str
    recorded_at: str                  # first capture that held this content
    superseded_at: str | None         # capture that replaced it (None = current)
    last_seen_at: str                 # last capture that still saw this content
    audience_key: str                 # api:<relation>, bc:<company>/<set>, ...
    missing_from_source_since: str | None = None
    capture_lineage: str | None = None   # bc_<company> for BC captures
    # (since, until, basis) stretches the source did not hold the record besides the
    # open absence above: absences it returned from, its soft deletes (open or closed).
    # Read from the current version, like missing_from_source_since. A mirror absence
    # a full capture found replaced adds (replaced_basis, replaced_by): the
    # source_mirror label and its successor keys as JSON [[key, capture id], ...].
    removals: tuple[tuple[Any, ...], ...] = ()
    # When the capture pass that first held this content began (a source whose capture
    # store records passes; None: the version's own recorded_at). A record read later in
    # one pass is still known from the pass's start (``Projector._local_currency``).
    pass_started_at: str | None = None


class CaptureSource(Protocol):
    source: str

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        """Every retained version of every record of ``relations``, read as one
        consistent snapshot. Each value may be lazy but must be re-iterable (a relation
        that feeds several bindings is read once per binding); it stays valid until the
        source is closed or ``records`` is called again."""

    def describe(self) -> dict[str, Any]:
        """Generation and freshness facts for ``store_meta`` (no business data)."""


class _Relations(_MappingBase):
    """A lazy ``relation -> re-iterable records`` map (a CaptureSource result)."""

    def __init__(self, relations: Sequence[str],
                 opener: Callable[[str], Iterator[CapturedRecord]]) -> None:
        self._relations = list(dict.fromkeys(relations))
        self._opener = opener

    def __getitem__(self, relation: str) -> "_Replay":
        if relation not in self._relations:
            raise KeyError(relation)
        return _Replay(lambda: self._opener(relation))

    def __iter__(self) -> Iterator[str]:
        return iter(self._relations)

    def __len__(self) -> int:
        return len(self._relations)


class _Replay:
    def __init__(self, factory: Callable[[], Iterator[CapturedRecord]]) -> None:
        self._factory = factory

    def __iter__(self) -> Iterator[CapturedRecord]:
        return iter(self._factory())



class RowSample:
    """A 1-in-``stride`` sample of the rows the record queries of a capture source step
    over (its ``sample``; the projection reserve of ``scripts/eval_document_fields_live``
    times a projection of it): only a kept row is decoded into a record. ``rows`` counts
    every row stepped over, ``kept`` those kept; the position runs on across queries, so
    many small ones keep 1 in ``stride`` too."""

    def __init__(self, stride: int) -> None:
        self.stride, self.rows, self.kept = stride, 0, 0

    def __call__(self, rows: Iterable[Any]) -> Iterator[Any]:
        for row in rows:
            self.rows += 1
            if (self.rows - 1) % self.stride == 0:
                self.kept += 1
                yield row


def _sampled(sample: RowSample | None, rows: Iterable[Any]) -> Iterable[Any]:
    """``rows``, or the ones ``sample`` keeps."""
    return rows if sample is None else sample(rows)

# Earlier mirror replacement rules whose labels the rule in force (the key) infers too,
# so ``MirrorCaptures`` reads them without waiting for a writer. v1 dropped the unique
# constraint on the cascade parent alone that v2 pairs again; v1 inferred nothing else,
# but a relation a v1 mirror filled from a persisted copy can carry an older rule's
# labels, so on such a file the reader keeps only the relations the rule in force gives
# a replacement key. A rule bump accepts no earlier marker until its entry here says so.
_SUBSUMED_RULES: Mapping[str, frozenset[str]] = {
    "beyond-cascade-parent/v2": frozenset({"beyond-cascade-parent/v1"})}


class MirrorCaptures:
    """Every retained version of the Portal source mirror, read-only.

    Reads the mirror file directly (``mode=ro``, one read transaction) because the
    mirror has no public "all versions" export yet; the tables and columns read are
    checked first and a mirror without them is refused (``mirror_layout_unsupported``).
    Handles are minted by the mirror's own ``make_citation``, so ``resolve_citation``
    resolves every one. Only completed captures are read; staged pages never are.
    Rows stream from a cursor (nothing is held per relation); the read transaction
    opened by :meth:`records` is the one snapshot every relation is read from, and it
    lasts until :meth:`close` (or the next :meth:`records`). The mirror runs in WAL
    mode, so the open snapshot never blocks its writer. The snapshot is one on purpose:
    the incremental build compares :meth:`fingerprint` with what :meth:`records` yields,
    and both must read the same commit (short per-relation transactions would let a
    sync land between them and record a fingerprint for rows never projected). While it
    is open no checkpoint can reset the WAL, so the WAL grows by the writes of one build
    only: the writer checkpoints at the end of each sync and the host after each build
    (``capture_store.checkpoint``, ``Host.checkpoint_stores``). A key's ``row_removal``
    stretches (missing from the source, or soft-deleted by it) ride on its current
    version as ``CapturedRecord.removals``. Their replacement labels are read only from
    a mirror labelled under the rule in force (``mirror_meta`` ``replacement_rule`` is
    ``source_mirror.REPLACEMENT_RULE``; a writer opening the file brings it there) or
    under an earlier rule it subsumes (:data:`_SUBSUMED_RULES`: every label that rule
    gave the rule in force gives too), so a read-only build need not wait for a sync; on
    such a file a relation ``source_mirror.replacement_key`` gives no key under the rule
    in force reads with no replacement label (one a copy imported under an older rule
    brought in), as a writer opening the file would leave it. A
    mirror marked with any other rule, or labelled with no marker, is refused
    (``mirror_replacement_rule_stale``), never projected with a successor the rule in
    force does not infer. ``prisma_facts`` overrides the committed generated Prisma facts
    those keys come from, as it does for ``SourceMirror``.
    """

    source = "portal_api"
    sample: RowSample | None = None   # a sample of its record queries (``RowSample``)
    _NEEDED = {
        "mirror_meta": {"name", "value"},
        "capture": {"capture_id", "finished_at"},
        "capture_state": {"capture_id", "state"},
        "relation_state": {"relation", "last_refresh_at", "last_capture_id",
                           "last_full_traversal_at", "columns_json"},
        "row_current": {"relation", "key_json", "run_index", "run_length", "row_json",
                        "row_digest", "capture_id", "last_seen_at", "absent_since_capture",
                        "missing_from_source_since"},
        "row_version": {"relation", "key_json", "capture_id", "run_index", "run_length",
                        "row_json", "row_digest", "last_seen_at", "superseded_by_capture"},
        "row_removal": {"relation", "key_json", "basis", "since", "until", "replaced_basis",
                        "replaced_by"},
    }
    # A current row's removal stretches (since, until, basis, replaced_basis,
    # replaced_by), as CapturedRecord.removals.
    _REMOVALS = ("(SELECT json_group_array(json_array(r.since, r.until, r.basis, "
                 "r.replaced_basis, r.replaced_by)) "
                 "FROM row_removal r WHERE r.relation = row_current.relation "
                 "AND r.key_json = row_current.key_json)")

    def __init__(self, path: str | Path, *,
                 prisma_facts: Mapping[str, Any] | None = None) -> None:
        from integrations.source_mirror import replacement_models

        self.path = Path(path)
        if not self.path.is_file():
            raise ProjectionError("mirror_missing", str(self.path))
        # Natural keys for replaced rows, as ``SourceMirror(prisma_facts=...)`` takes them
        # (None: the committed generated facts, loaded only when a label needs them).
        self._replacement_models = (None if prisma_facts is None
                                    else replacement_models(prisma_facts))
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # A scoped fallback (integrations/mirror_capture.py) reads the shared mirror it
        # names through scope views; any other file reads as itself.
        from integrations.source_mirror import attach_scoped
        attach_scoped(self._conn)
        self._snapshot = False
        layout = {}
        for table, columns in self._NEEDED.items():
            have = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            layout[table] = have
            if not columns <= have:
                self._conn.close()
                raise ProjectionError("mirror_layout_unsupported",
                                      f"{table} lacks {sorted(columns - have)}")
        # A current row's last sighting: the mirror's own rule (a full traversal writes
        # only changed rows and records its sighting of the rest once); a file written
        # before that rule stores it on every row.
        from integrations.source_mirror import _sighting_sql
        self._seen_at = (_sighting_sql("row_current")[1]
                         if "seen_through_at" in layout["relation_state"]
                         and "sighting_floor" in layout["row_current"] else "last_seen_at")
        row = self._conn.execute(
            "SELECT value FROM mirror_meta WHERE name = 'generation'").fetchone()
        if row is None:
            self._conn.close()
            raise ProjectionError("mirror_generation_missing")
        self.generation = row["value"]

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._end_snapshot()
        finally:
            self._conn.close()

    def __enter__(self) -> "MirrorCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        relations = {row["relation"]: {"last_refresh_at": row["last_refresh_at"],
                                       "last_capture_id": row["last_capture_id"],
                                       "last_full_traversal_at": row["last_full_traversal_at"]}
                     for row in self._conn.execute(
                         "SELECT * FROM relation_state ORDER BY relation")}
        described: dict[str, Any] = {"generation": self.generation, "relations": relations}
        persisted = self._persisted(relations)
        if persisted:
            described["persisted"] = persisted
        return described

    def _persisted(self, relations: Iterable[str]) -> dict[str, dict[str, Any]]:
        """relation -> how it is served, for the relations served from a persisted
        capture while the live source was unavailable (``integrations/mirror_capture.py``):
        every relation of a copy restored whole, or the ones a running mirror filled
        from a copy (``persisted_relation``); or a relation it holds whose last refresh
        failed (``refresh_failure``: ``cached_mirror``, as of its last refresh, with the
        failure's error code). Stale whatever their age."""
        def served(relation: str, freshness: Mapping[str, Any],
                   source: str = "persisted_capture") -> dict[str, Any]:
            out = {"relation": relation, "served_from": source,
                   "captured_at": freshness.get("captures_as_of"),
                   "http_status": freshness.get("http_status"),
                   "error_code": freshness.get("error_code")}
            if freshness.get("label"):
                out["label"] = freshness["label"]
            return out

        row = self._conn.execute(
            "SELECT value FROM mirror_meta WHERE name = 'persisted_fallback'").fetchone()
        if row is not None:
            whole = json.loads(row["value"])
            return {relation: served(relation, whole) for relation in relations}
        tables = {row["name"] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('persisted_relation', "
            "'refresh_failure')")}
        out: dict[str, dict[str, Any]] = {}
        if "refresh_failure" in tables:
            from integrations.mirror_capture import stale_label
            for item in self._conn.execute(
                    "SELECT f.relation, f.error, s.last_refresh_at FROM refresh_failure f "
                    "JOIN relation_state s ON s.relation = f.relation ORDER BY f.relation"):
                code = item["error"].partition(":")[0].strip()
                code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else "refresh_failed"
                as_of = item["last_refresh_at"]
                out[item["relation"]] = served(item["relation"], {
                    "captures_as_of": as_of, "http_status": None, "error_code": code,
                    "label": stale_label(as_of, None, code) if as_of else None},
                    "cached_mirror")
        if "persisted_relation" in tables:  # a copy filled in is the farther from live
            out.update({item["relation"]: served(item["relation"],
                                                 json.loads(item["freshness_json"]))
                        for item in self._conn.execute(
                            "SELECT relation, freshness_json FROM persisted_relation")})
        return dict(sorted(out.items()))

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        # The mirror's own minting and replacement rule.
        from integrations.source_mirror import (REPLACEMENT_RULE, generated_replacement_models,
                                                make_citation, stale_replacement_relations)

        self._end_snapshot()
        self._conn.execute("BEGIN")  # one snapshot for every relation
        self._snapshot = True
        finished = {row[0]: row[1] for row in self._conn.execute(
            """SELECT c.capture_id, c.finished_at FROM capture c JOIN capture_state s
               ON s.capture_id = c.capture_id WHERE s.state = 'complete'""")}
        rule = self._conn.execute(
            "SELECT value FROM mirror_meta WHERE name = 'replacement_rule'").fetchone()
        accepted = {REPLACEMENT_RULE, *_SUBSUMED_RULES.get(REPLACEMENT_RULE, ())}
        # An unknown marker is refused even unlabelled; no marker only when labelled.
        if (rule is not None and rule["value"] not in accepted) or (
                rule is None and self._conn.execute(
                    "SELECT 1 FROM row_removal WHERE replaced_basis IS NOT NULL LIMIT 1"
                    ).fetchone() is not None):
            self._end_snapshot()
            raise ProjectionError("mirror_replacement_rule_stale",
                                  "replacement labels under another rule; a mirror writer "
                                  "opening the file drops the stale ones")
        # Under a subsumed rule, the relations whose labels the rule in force drops (the
        # writer's own cleanup, applied on read); a relation not in relation_state has
        # no columns, so no key, and keeps no label.
        unlabelled: set[str] = set()
        if rule is not None and rule["value"] != REPLACEMENT_RULE:
            models = (generated_replacement_models() if self._replacement_models is None
                      else self._replacement_models)
            unlabelled = set(stale_replacement_relations(self._conn, models))

        def stream(relation: str) -> Iterator[CapturedRecord]:
            if not self._snapshot:
                raise ProjectionError("capture_snapshot_closed", relation)
            for row in _sampled(self.sample, self._conn.execute(
                    f"""SELECT key_json, run_index, run_length, row_json, row_digest,
                       capture_id, {self._seen_at} AS last_seen_at, missing_from_source_since,
                       NULL AS superseded_by_capture, 1 AS current,
                       {self._REMOVALS} AS removals
                       FROM row_current WHERE relation = ?
                       UNION ALL
                       SELECT key_json, run_index, run_length, row_json, row_digest,
                       capture_id, last_seen_at, NULL, superseded_by_capture, 0, NULL
                       FROM row_version WHERE relation = ?
                       ORDER BY 1, 6, 2""", (relation, relation))):
                recorded = finished.get(row["capture_id"])
                superseded = (None if row["current"] else
                              finished.get(row["superseded_by_capture"], ""))
                if recorded is None or superseded == "":
                    continue  # a capture that never completed is not L1 truth
                key = json.loads(row["key_json"])
                run = row["run_length"] > 1
                removals = json.loads(row["removals"] or "[]")
                if relation in unlabelled:
                    removals = [item[:3] + [None, None] for item in removals]
                yield CapturedRecord(
                    source=self.source, relation=relation,
                    record_key=row["key_json"] + (f"#{row['run_index']}" if run else ""),
                    row=json.loads(row["row_json"]),
                    l1_handle=make_citation(relation, key, row["capture_id"],
                                            self.generation),
                    field_prefix=f"rows.{row['run_index']}" if run else "row",
                    digest=row["row_digest"], recorded_at=recorded,
                    superseded_at=superseded, last_seen_at=row["last_seen_at"],
                    audience_key=f"api:{relation}",
                    missing_from_source_since=row["missing_from_source_since"],
                    removals=tuple(tuple(item) for item in removals))

        return _Relations(relations, stream)

    def fingerprint(self, relations: Sequence[str]) -> dict[str, str]:
        """relation -> SHA-256 of everything :meth:`records` yields for it that the
        projection reads (raw columns plus the completion times it resolves, and the
        generation its handles carry), read inside the snapshot the last :meth:`records`
        opened, without parsing a row (``ontology.project_incremental``). A current
        version's ``last_seen_at`` is left out: every sync that sees the row again moves
        it, and the projection reads only a superseded version's last sighting (the
        window of a capture event)."""
        if not self._snapshot:
            raise ProjectionError("capture_snapshot_closed", "fingerprint")
        finished = {row[0]: row[1] for row in self._conn.execute(
            """SELECT c.capture_id, c.finished_at FROM capture c JOIN capture_state s
               ON s.capture_id = c.capture_id WHERE s.state = 'complete'""")}
        out = {}
        for relation in dict.fromkeys(relations):
            digest = hashlib.sha256(f"{self.generation}\x00{relation}\n".encode())
            for row in self._conn.execute(
                    f"""SELECT key_json, run_index, run_length, row_json, row_digest,
                       capture_id, last_seen_at, missing_from_source_since,
                       NULL AS superseded_by_capture, 1 AS current,
                       {self._REMOVALS} AS removals
                       FROM row_current WHERE relation = ?
                       UNION ALL
                       SELECT key_json, run_index, run_length, row_json, row_digest,
                       capture_id, last_seen_at, NULL, superseded_by_capture, 0, NULL
                       FROM row_version WHERE relation = ?
                       ORDER BY 1, 6, 2""", (relation, relation)):
                cells = tuple(row)
                if cells[9]:
                    cells = cells[:6] + (None,) + cells[7:]
                digest.update(repr((cells, finished.get(row[5]),
                                    finished.get(row[8]))).encode("utf-8"))
                digest.update(b"\n")
            out[relation] = digest.hexdigest()
        return out


class MailCaptures:
    """Every retained message version of the versioned mail capture, read-only
    (``integrations/capture_mail.MailCaptureStore``; source family ``graph_mail``).

    Relations: ``messages`` (one record per message version interval, cited by its
    ``graph-mail://<mailbox>/<message>@<changeKey>`` handle; the row is the captured
    record without the body, as ``MailCaptureStore.resolve`` returns it, so values
    cite ``row.<field>`` unchanged) and ``attachments`` (one record per captured file
    attachment of a version, cited by its byte-pinned handle; uncaptured bytes have
    no handle and are not records). Audience ``mail:<mailbox object id>``: readers are
    re-gated per mailbox through ``ReadPolicy`` (``MailCaptureStore.audience_check``).

    A version listed again after a gap (removal, or A -> B -> A changeKeys) has
    several interval rows under one handle; the projection keeps one version per
    handle, so only the latest interval of such a handle is read (an older interval
    reads as not captured then: under-claimed, never overlapping).

    Absence: a message whose latest captured version was removed from the source (a
    delta ``@removed``, or ``absent_after_resync`` after a full round) was not replaced
    by other content, so that version stays the current one and the object (and its
    attachments) is marked ``missing_from_source_since`` the removal: missing from the
    source, never deleted, and not yet known for an ``as_known_at`` before it. Mail is
    gated per mailbox, not per folder, so this never shows a reader a message it could
    not see before.

    :meth:`documents` gives the same versions as ``ontology.mentions.Document``
    values (subject, body text or preview, captured attachment names), and each
    captured attachment as its own document (its name and extracted text, a PDF's pages
    cited ``&page=<n>``: ``ontology.project_graph``), for the mention links written into
    the same store generation. Relation ``documentFields``: the typed fields of captured
    attachments (``ontology.document_fields.captured``), members of containers included;
    the document reader is also told whether the captured mailbox sent the message itself
    (an own-party send: direction, never projected onto the attachment).
    Relation ``containerMembers``: the members of zip / ``.msg`` / ``.eml`` attachments
    and their parts not read (``ontology.project_graph.member_records``), each also a
    mention document.
    """

    source = "graph_mail"
    _NEEDED = {
        "meta": {"key", "value"},
        "folders": {"mailbox_id", "status", "last_run_at", "last_complete_at"},
        "messages": {"version_id", "l1_handle", "mailbox_id", "message_id", "record", "digest",
                     "body_sha256", "attachments_status", "recorded_at", "superseded_at",
                     "removed_reason", "last_seen_at"},
        "attachments": {"message_handle", "attachment_id", "l1_handle", "mailbox_id",
                        "metadata", "content_sha256", "content_status"},
    }
    # ``gone``: the interval ended by a removal and no later version of the message exists.
    _LATEST = ("SELECT m.*, (m.removed_reason IS NOT NULL AND m.version_id = "
               "(SELECT MAX(x.version_id) FROM messages x WHERE x.mailbox_id = m.mailbox_id "
               "AND x.message_id = m.message_id)) AS gone FROM messages m WHERE m.version_id "
               "IN (SELECT MAX(version_id) FROM messages GROUP BY l1_handle)")
    # The attachments the projection reads, each with its message's latest interval row.
    _ATTACHED = ("FROM attachments a JOIN (" + _LATEST + ") m ON m.l1_handle = "
                 "a.message_handle WHERE a.l1_handle IS NOT NULL AND a.content_sha256 "
                 "IS NOT NULL")

    def __init__(self, state_dir: str | Path, *, internal_domains: Iterable[str] = ()) -> None:
        root = Path(state_dir).expanduser() / "mail-capture"
        self.path = root / "capture.db"
        self.blobs = root / "blobs" / "sha256"
        self.internal_domains = tuple(d.strip().lower() for d in internal_domains
                                      if isinstance(d, str) and d.strip())
        if not self.path.is_file():
            raise ProjectionError("mail_capture_missing", str(self.path))
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # A scoped fallback (integrations/mirror_capture.py) reads the shared mirror it
        # names through scope views; any other file reads as itself.
        from integrations.source_mirror import attach_scoped
        attach_scoped(self._conn)
        self._snapshot = False
        for table, columns in self._NEEDED.items():
            have = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= have:
                self._conn.close()
                raise ProjectionError("mail_capture_layout_unsupported",
                                      f"{table} lacks {sorted(columns - have)}")
            if table == "attachments":   # a store captured before attachment text
                self._text = ("text_status, text_sha256" if {"text_status", "text_sha256"}
                              <= have else "NULL AS text_status, NULL AS text_sha256")

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._end_snapshot()
        finally:
            self._conn.close()

    def __enter__(self) -> "MailCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no business data): the stalest folder run, the oldest
        complete round, and whether every folder is complete with no truncated
        attachment listing (else ``last_full_traversal_at`` is null)."""
        folders = self._conn.execute(
            "SELECT status, last_run_at, last_complete_at FROM folders").fetchall()
        truncated = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE superseded_at IS NULL AND "
            "attachments_status IN ('truncated', 'truncated_capped')").fetchone()[0]
        last = self._conn.execute("SELECT MAX(version_id) FROM messages").fetchone()[0]
        runs = [f["last_run_at"] for f in folders if f["last_run_at"]]
        whole = bool(folders) and truncated == 0 and all(
            f["status"] == "complete" and f["last_complete_at"] for f in folders)
        state = {"last_refresh_at": min(runs) if runs else None, "last_capture_id": last,
                 "last_full_traversal_at": (min(f["last_complete_at"] for f in folders)
                                            if whole else None)}
        version = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return {"generation": None if version is None else f"mail-capture/{version[0]}",
                "relations": {"messages": dict(state), "attachments": dict(state)}}

    @staticmethod
    def _message_row(record_json: str) -> dict[str, Any]:
        record = json.loads(record_json)
        return {k: v for k, v in record.items() if k not in ("citation", "source_acl")}

    @staticmethod
    def _interval(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """(superseded_at, missing_from_source_since) of a latest-interval row."""
        return (None, row["superseded_at"]) if row["gone"] else (row["superseded_at"], None)

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        self._end_snapshot()
        self._conn.execute("BEGIN")  # one snapshot for every relation
        self._snapshot = True

        def stream(relation: str, ocr: bool = False, sender: bool = False
                   ) -> Iterator[CapturedRecord]:
            if not self._snapshot:
                raise ProjectionError("capture_snapshot_closed", relation)
            if relation == "messages":
                for row in self._conn.execute(self._LATEST + " ORDER BY mailbox_id, "
                                              "message_id, recorded_at, version_id"):
                    superseded, missing = self._interval(row)
                    yield CapturedRecord(
                        source=self.source, relation=relation,
                        record_key=canonical([row["mailbox_id"], row["message_id"]]),
                        row=self._message_row(row["record"]), l1_handle=row["l1_handle"],
                        field_prefix="row", digest=row["digest"],
                        recorded_at=row["recorded_at"], superseded_at=superseded,
                        last_seen_at=row["last_seen_at"],
                        audience_key=f"mail:{row['mailbox_id']}",
                        missing_from_source_since=missing)
            elif relation == "attachments":
                for row in self._conn.execute(
                        f"SELECT a.*, {self._text}, m.message_id, m.recorded_at, "
                        "m.superseded_at, m.last_seen_at, m.gone, m.record AS message "
                        + self._ATTACHED + " ORDER BY a.mailbox_id, m.message_id, "
                        "a.attachment_id, m.recorded_at"):
                    metadata = json.loads(row["metadata"])
                    superseded, missing = self._interval(row)
                    text = ({"text_status": row["text_status"],
                             "text_sha256": row["text_sha256"]} if ocr else {})
                    # Who sent the message (``sender``): only the document reader asks,
                    # to tell an attachment this mailbox sent itself from one it received
                    # (``ontology.document_fields``). Never projected onto the attachment.
                    whose = _sender(row["message"]) if sender else {}
                    yield CapturedRecord(
                        source=self.source, relation=relation,
                        record_key=canonical([row["mailbox_id"], row["message_id"],
                                              row["attachment_id"]]),
                        row={**metadata, "mailbox": row["mailbox_id"],
                             "message_id": row["message_id"],
                             "attachment_id": row["attachment_id"],
                             "content_sha256": row["content_sha256"],
                             "content_status": row["content_status"], **text, **whose},
                        l1_handle=row["l1_handle"], field_prefix="row",
                        digest=row["content_sha256"], recorded_at=row["recorded_at"],
                        superseded_at=superseded, last_seen_at=row["last_seen_at"],
                        audience_key=f"mail:{row['mailbox_id']}",
                        missing_from_source_since=missing)
            elif relation == "documentFields":   # typed fields of attachments
                from ontology import document_fields
                # A scan is read from its pinned OCR text, as for SharePoint files.
                yield from document_fields.captured(
                    stream("attachments", ocr=True, sender=True), self.blobs)
            elif relation == MEMBER_RELATION:   # members of zip / .msg / .eml attachments
                from ontology.project_graph import member_records
                yield from member_records(stream("attachments"), self.blobs, {
                    "mailbox": "mailbox", "message_id": "message_id",
                    "attachment_id": "attachment_id"})

        return _Relations(relations, stream)

    def _blob_text(self, digest: Any) -> str | None:
        """A body's text from its blob (stored as it is or compressed), re-hashed."""
        from integrations.captured_text import read_blob
        content = read_blob(self.blobs, digest)
        return None if content is None else content.decode("utf-8", errors="replace")

    def documents(self) -> Iterator[Any]:
        """Every projected message version as an ``ontology.mentions.Document``
        (fields: subject, the body text or else the preview, and the name of each
        captured attachment under its own handle), each followed by its captured
        attachments as documents of their own (``ontology.project_graph``). Read in its
        own snapshot."""
        from ontology.identifiers import copy_key
        from ontology.mentions import Document, TextField, sender_class
        from ontology.project_graph import attachment_document, member_documents

        # Streamed from a cursor (one message row at a time); the attachment names of
        # each row come from a second cursor on the same read-only connection.
        for row in self._conn.execute(self._LATEST + " ORDER BY mailbox_id, message_id, "
                                      "recorded_at, version_id"):
            record = self._message_row(row["record"])
            handle = row["l1_handle"]
            fields = []
            if isinstance(record.get("subject"), str):
                fields.append(TextField(handle, "row.subject", record["subject"]))
            body = self._blob_text(row["body_sha256"])
            if body is not None:
                fields.append(TextField(handle, "row.body_text", body))
            elif isinstance(record.get("body_preview"), str):
                fields.append(TextField(handle, "row.body_preview", record["body_preview"]))
            attached = self._conn.execute(
                f"SELECT l1_handle, metadata, content_sha256, {self._text} FROM attachments "
                "WHERE message_handle = ? AND l1_handle IS NOT NULL AND content_sha256 IS NOT "
                "NULL ORDER BY attachment_id", (handle,)).fetchall()
            for item in attached:
                name = json.loads(item["metadata"]).get("name")
                if isinstance(name, str):
                    fields.append(TextField(item["l1_handle"], "row.name", name))
            superseded, _missing = self._interval(row)
            date = record.get("received_at") or record.get("sent_at")
            yield Document(handle, "email", tuple(fields),
                           sender_class=sender_class(record.get("from"), self.internal_domains,
                                                     record.get("authentication_results")),
                           digest=row["digest"], recorded_at=row["recorded_at"],
                           superseded_at=superseded, date=date,
                           copy_key=copy_key("mail", "messages", record))
            for item in attached:
                captured = {**json.loads(item["metadata"]),
                            "content_sha256": item["content_sha256"],
                            "text_status": item["text_status"],
                            "text_sha256": item["text_sha256"]}
                yield attachment_document(item["l1_handle"], captured, self.blobs,
                                          recorded_at=row["recorded_at"],
                                          superseded_at=superseded, date=date)
                yield from member_documents(item["l1_handle"], captured, self.blobs,
                                            recorded_at=row["recorded_at"],
                                            superseded_at=superseded, date=date)


def _sender(record: str) -> dict[str, Any]:
    """The sender provenance of a captured message (its stored ``record``)."""
    message = json.loads(record)
    return {"message_from": message.get("from"), "mailbox_upn": message.get("mailbox_upn")}


def mail_attachment_sender(db: sqlite3.Connection, handle: str) -> dict[str, Any] | None:
    """The sender provenance the mail projection reads the direction of the captured
    attachment ``handle`` from (``MailCaptures`` ``sender=True``), on the capture
    connection ``db``, for ``ontology.document_fields.pinned_document_row`` to read it
    the same way: its message's ``message_from`` and ``mailbox_upn``, and as
    ``mailbox_copies`` the attachments of the same bytes in the same captured mailbox
    (``document_fields.copy_mailbox``: a forward is told from its received copy) with
    theirs, never another mailbox's, and every captured mailbox holding an attachment
    (the own party's domains). The rows ``documentFields`` reads: each message's latest
    interval only. None when the capture holds no such attachment. Never shown to a
    reader."""
    from ontology.document_fields import copy_mailbox

    attached = MailCaptures._ATTACHED
    found = db.execute(f"SELECT a.content_sha256, m.record {attached} AND a.l1_handle = ?",
                       (handle,)).fetchone()
    if found is None:
        return None
    digest, whose = found[0], _sender(found[1])
    copies = [copy for copy in ({**_sender(row[0]), "content_sha256": digest}
                                for row in db.execute(f"SELECT m.record {attached} AND "
                                                      "a.content_sha256 = ?", (digest,)))
              if copy_mailbox(copy) == copy_mailbox(whose)]
    mailboxes = [{"mailbox_upn": row[0]} for row in db.execute(
        f"SELECT DISTINCT json_extract(m.record, '$.mailbox_upn') {attached}")]
    return {**whose, "mailbox_copies": copies + mailboxes}


class SharePointCaptures:
    """Every retained drive-item version of the versioned SharePoint capture, read-only
    (``integrations/capture_sharepoint.SharePointCaptureStore``; source family
    ``sharepoint``).

    Relation ``driveItems``: one record per version interval, keyed by ``(drive_id, id)``
    and cited by its ``sharepoint://<drive>/<item>@<eTag>`` handle; the row is the
    captured record with ``content_sha256``/``content_status``/``text_status``, as
    ``SharePointCaptureStore.resolve`` returns it (the extracted text is cited, not
    projected). Audience ``sharepoint:<drive id>``; every reader is re-gated per item
    through the tools' item gate (``SharePointCaptureStore.item_gate``: path keys and
    item permissions recorded at capture). As for mail, only the latest interval of a
    handle listed again after a gap is read (under-claimed, never overlapping).

    :meth:`documents` gives the same versions as ``ontology.mentions.Document`` values
    (file name and extracted text, author unknown) for the mention links written into
    the same store generation. Relation ``documentFields``: the typed fields of captured
    files (``ontology.document_fields.captured``), a scan's read from its pinned OCR text,
    members of containers included. Relation ``containerMembers``: the members of zip /
    ``.msg`` / ``.eml`` files and their parts not read
    (``ontology.project_graph.member_records``), each also a mention document. Relation
    ``listItems``: the items of the SharePoint lists the list-item capture holds
    (``<state>/sharepoint-list-capture``, ``ontology.project_graph.SharePointListCaptures``),
    ``source_not_captured`` without that store.

    The drive capture and the list-item capture are two stores (:meth:`stores`), each
    opened when held and read independently of the other: without the drive store its
    relations are ``source_not_captured`` and the generation is the list store's; with
    neither the source is ``sharepoint_capture_missing``.
    """

    source = "sharepoint"
    sample: RowSample | None = None   # a sample of its record and document queries
    _NEEDED = {
        "meta": {"key", "value"},
        "roots": {"drive_id", "root_path", "status", "last_run_at", "last_complete_at"},
        "items": {"version_id", "l1_handle", "drive_id", "item_id", "record", "digest",
                  "content_status", "content_sha256", "text_status", "text_sha256",
                  "recorded_at", "superseded_at", "last_seen_at"},
    }
    _LATEST = ("SELECT * FROM items WHERE version_id IN "
               "(SELECT MAX(version_id) FROM items GROUP BY l1_handle)")

    @staticmethod
    def stores(state_dir: str | Path) -> tuple[Path, Path]:
        """The drive capture and the list-item capture store files of ``state_dir``,
        held or not."""
        from integrations import capture_sharepoint_lists as lists

        state = Path(state_dir).expanduser()
        return (state / "sharepoint-capture" / "capture.db",
                state / lists.STORE_DIR / lists.DB_FILE)

    def __init__(self, state_dir: str | Path) -> None:
        from ontology.project_graph import SharePointListCaptures

        self.path, held = self.stores(state_dir)
        self.blobs = self.path.parent / "blobs" / "sha256"
        if not self.path.is_file() and not held.is_file():
            raise ProjectionError("sharepoint_capture_missing", str(self.path))
        self._conn: sqlite3.Connection | None = None
        self._snapshot = False
        self._reads = 0   # the records() call whose relations may still be read
        self.lists = SharePointListCaptures(held) if held.is_file() else None
        if not self.path.is_file():
            return
        try:
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                         isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            for table, columns in self._NEEDED.items():
                have = {row["name"] for row in
                        self._conn.execute(f"PRAGMA table_info({table})")}
                if not columns <= have:
                    raise ProjectionError("sharepoint_capture_layout_unsupported",
                                          f"{table} lacks {sorted(columns - have)}")
        except BaseException:
            self.close()
            raise

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._reads += 1
        try:
            self._end_snapshot()
        finally:
            if self._conn is not None:
                self._conn.close()
            if self.lists is not None:
                self.lists.close()

    def __enter__(self) -> "SharePointCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def capture_gap(self, relation: str) -> str | None:
        """A relation this capture does not read (``CAPTURED_RELATIONS``), a drive
        relation without the drive store, or list items while the list-item capture holds
        none or not all."""
        if relation == LIST_ITEMS:
            return "source_not_captured" if self.lists is None else self.lists.capture_gap()
        return None if relation in CAPTURED_RELATIONS[self.source] \
            and self._conn is not None else "source_not_captured"

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no business data): the stalest root walk, the oldest complete
        walk, and whether every root is complete (else ``last_full_traversal_at`` is
        null; a root with a file whose bytes were not captured is not complete), and the
        list items'. Without the drive store, only the list items' and the list store's
        generation."""
        if self._conn is None:
            return {"generation": f"sharepoint-list-capture/{self.lists.generation}",
                    "relations": {LIST_ITEMS: self.lists.describe()}}
        roots = self._conn.execute(
            "SELECT status, last_run_at, last_complete_at FROM roots").fetchall()
        last = self._conn.execute("SELECT MAX(version_id) FROM items").fetchone()[0]
        runs = [r["last_run_at"] for r in roots if r["last_run_at"]]
        whole = bool(roots) and all(r["status"] == "complete" and r["last_complete_at"]
                                    for r in roots)
        version = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        relations = {"driveItems": {
            "last_refresh_at": min(runs) if runs else None, "last_capture_id": last,
            "last_full_traversal_at": (min(r["last_complete_at"] for r in roots)
                                       if whole else None)}}
        if self.lists is not None:
            relations[LIST_ITEMS] = self.lists.describe()
        return {"generation": None if version is None else f"sharepoint-capture/{version[0]}",
                "relations": relations}

    @staticmethod
    def _row(row: Mapping[str, Any]) -> dict[str, Any]:
        from operating.doc_metadata_taxonomy import stated_metadata

        record = json.loads(row["record"])
        # What the Portal states in the version's own eTag-bound listItem columns (QA-06).
        return {**record, "portal_metadata": stated_metadata(record),
                "content_sha256": row["content_sha256"],
                "content_status": row["content_status"], "text_status": row["text_status"]}

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        self._end_snapshot()
        self._reads += 1
        read = self._reads
        if self._conn is not None:
            self._conn.execute("BEGIN")  # one snapshot for every drive relation
            self._snapshot = True

        def stream(relation: str, ocr: bool = False) -> Iterator[CapturedRecord]:
            if read != self._reads:   # records called again, or closed
                raise ProjectionError("capture_snapshot_closed", relation)
            if relation == LIST_ITEMS and self.lists is not None:
                yield from self.lists.records(relation, self.sample)
                return
            if self._conn is None:   # no drive store: its relations hold nothing
                return
            if relation == "documentFields":   # typed fields of captured files
                from ontology import document_fields
                # A scan is read from its pinned OCR text (P8 quality packets).
                yield from document_fields.captured(stream("driveItems", ocr=True),
                                                    self.blobs)
                return
            if relation == MEMBER_RELATION:   # members of zip / .msg / .eml files
                from ontology.project_graph import member_records
                yield from member_records(stream("driveItems"), self.blobs,
                                          {"drive_id": "drive_id", "item_id": "id"})
                return
            if relation != "driveItems":
                return
            for row in _sampled(self.sample, self._conn.execute(
                    self._LATEST + " ORDER BY drive_id, item_id, recorded_at, version_id")):
                fields = self._row(row)
                if ocr:
                    fields["text_sha256"] = row["text_sha256"]
                yield CapturedRecord(
                    source=self.source, relation=relation,
                    record_key=canonical([row["drive_id"], row["item_id"]]),
                    row=fields, l1_handle=row["l1_handle"], field_prefix="row",
                    digest=row["digest"], recorded_at=row["recorded_at"],
                    superseded_at=row["superseded_at"], last_seen_at=row["last_seen_at"],
                    audience_key=f"sharepoint:{row['drive_id']}")

        return _Relations(relations, stream)

    def documents(self) -> Iterator[Any]:
        """Every projected version as an ``ontology.mentions.Document`` (fields: the file
        name under the version's handle and its text, a PDF's page by page cited
        ``<handle>#page=<n>``: the one reading of the captured file,
        ``integrations.captured_text``, shared with ``documentFields`` and resolve). The
        author of a file is unknown, so the sender class is ``unknown`` (external
        assertions)."""
        from integrations.captured_text import captured_text
        from ontology.identifiers import copy_key
        from ontology.mentions import Document, TextField, file_text_fields
        from ontology.project_graph import member_documents
        from operating.doc_metadata_taxonomy import REF_COLUMNS, stated_metadata

        if self._conn is None:
            return
        for row in _sampled(self.sample, self._conn.execute(
                self._LATEST + " ORDER BY drive_id, item_id, recorded_at, version_id")):
            record = json.loads(row["record"])
            handle = row["l1_handle"]
            fields = []
            if isinstance(record.get("name"), str):
                fields.append(TextField(handle, "row.name", record["name"]))
            # A record number the Portal states in a Ref column is a mention of this file,
            # cited to the captured column value (taxonomy.stated_metadata).
            stated = stated_metadata(record)
            for name in REF_COLUMNS if stated is not None else ():
                if name in stated["columns"]:
                    fields.append(TextField(handle, f"row.list_item_fields.fields.{name}",
                                            record["list_item_fields"]["fields"][name]))
            found = captured_text(self.blobs, {
                **record, "content_sha256": row["content_sha256"],
                "text_status": row["text_status"], "text_sha256": row["text_sha256"]})
            # A file read on its first pages only: its name only, and why (no gap
            # channel here).
            withheld = None if found is None else found.text_withheld
            if found is not None and found.text is not None and withheld is None:
                fields.extend(file_text_fields(handle, "row.content_text", found.text,
                                               paged=found.paged))
            yield Document(handle, "sharepoint_file", tuple(fields), sender_class="unknown",
                           digest=row["digest"], recorded_at=row["recorded_at"],
                           superseded_at=row["superseded_at"],
                           date=record.get("last_modified"),
                           copy_key=copy_key("sharepoint", "driveItems",
                                             {**record, "content_sha256": row["content_sha256"]}),
                           text_withheld=withheld)
            yield from member_documents(handle, {**record,
                                                 "content_sha256": row["content_sha256"]},
                                        self.blobs, recorded_at=row["recorded_at"],
                                        superseded_at=row["superseded_at"],
                                        date=record.get("last_modified"))


def _bc_captures(path: str | Path, **options: Any) -> Any:
    """The BC capture store source (``ontology/project_bc.py``, imported on use: it builds
    on this module's ``CapturedRecord``)."""
    from ontology.project_bc import BcCaptures
    return BcCaptures(path, **options)


def _mc_captures(path: str | Path, **options: Any) -> Any:
    """The Mission Control capture store source (``ontology/project_mc.py``, imported on
    use for the same reason)."""
    from ontology.project_mc import McCaptures
    return McCaptures(path, **options)


def _teams_captures(path: str | Path, **options: Any) -> Any:
    """The Teams capture store source (``ontology/project_graph.py``, imported on use)."""
    from ontology.project_graph import TeamsCaptures
    return TeamsCaptures(path, **options)


def _knowledge_captures(path: str | Path, **options: Any) -> Any:
    """The brain knowledge capture store source (``ontology/project_knowledge.py``,
    imported on use for the same reason)."""
    from ontology.project_knowledge import KnowledgeCaptures
    return KnowledgeCaptures(path, **options)


# The members of captured zip / .msg / .eml files and their parts not read
# (``ontology.project_graph.member_records``), served by the mail and SharePoint captures.
MEMBER_RELATION = "containerMembers"
# Capture sources by binding source family (format ``$defs/source``): the projector's
# registry of L1 readers (a binding whose source has none is reported
# ``source_not_captured``).
CAPTURE_SOURCES: dict[str, Callable[..., Any]] = {
    "portal_api": MirrorCaptures, "graph_mail": MailCaptures,
    "sharepoint": SharePointCaptures, "bc": _bc_captures, "mission_control": _mc_captures,
    "brain": _knowledge_captures, "teams": _teams_captures}
# The relations a family's capture reads, where it reads fewer than its bindings name (a
# family not listed reads every relation of its bindings). The others are never captured
# (``source_not_captured``; ``scripts/source_registry.py`` stops them at ``captured``):
# Teams lists its two message relations, so a relation a later Teams binding names is not
# captured until its capture reads it. SharePoint list items are
# those of the lists ``integrations/sharepoint_list_capture_scope.json`` names
# (``integrations.capture_sharepoint_lists``).
LIST_ITEMS = "listItems"
CAPTURED_RELATIONS: dict[str, frozenset[str]] = {
    "sharepoint": frozenset({"driveItems", "documentFields", MEMBER_RELATION, LIST_ITEMS}),
    "teams": frozenset({"channelMessages", "chatMessages", "chats", "channels", "teams"})}


def capture_source(source: str, path: str | Path, **options: Any) -> Any:
    """Open the registered capture source of one source family over ``path``."""
    try:
        factory = CAPTURE_SOURCES[source]
    except KeyError:
        raise ProjectionError("capture_source_unknown", source) from None
    return factory(path, **options)


# --------------------------------------------------------------------------- helpers

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def binding_unit(binding_id: str) -> str:
    """The build unit of one binding: its objects, versions, values, identifiers and
    events (and every gap reported while projecting them)."""
    return f"b:{binding_id}"


def link_unit(link_id: str, index: int, lb: Mapping[str, Any],
              bindings: Mapping[str, Any]) -> str:
    """The build unit of one link binding: the links of ``link_id`` of its kind between
    its two bindings (several link bindings with the same ends and kind share one
    unit, so a stored link row names its unit), else the binding alone (a kind this
    projector does not project, or an unknown binding: gaps only)."""
    if lb.get("kind") in _PROJECTED_LINK_KINDS and lb.get("from_binding") in bindings \
            and lb.get("to_binding") in bindings:
        return f"l:{link_id}|{lb['from_binding']}|{lb['to_binding']}|{lb['kind']}"
    return f"l:{link_id}#{index}"


def link_ends(lb: Mapping[str, Any], bindings: Mapping[str, Any]) -> set[str]:
    """The known bindings a link binding reads (from, to and its join binding)."""
    ends = {lb.get("from_binding"), lb.get("to_binding"), (lb.get("via") or {}).get("binding")}
    return {end for end in ends if end in bindings}


def event_id_of(event_type: str, l1_handle: str, object_id: str, part: str) -> str:
    """Id of the event a version yields (``part``: the time path of a date event, the
    observed property of a capture event)."""
    return "E:" + _sha("|".join([event_type, l1_handle, object_id, part]))[:40]


def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _cell(row: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """(present, value) of a column or a key inside a JSON-object column."""
    parts = path.split(".")
    if parts[0] not in row:
        return False, None
    value = row[parts[0]]
    for part in parts[1:]:
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _text(value: Any) -> str | None:
    """Source value as text, as the mirror's predicates read it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return canonical(value)


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value.strip()):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def _plain(number: Decimal) -> str:
    text = format(number, "f")
    return "0" if text in ("-0", "+0") else text


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not js_trim(value))


def _conflict_key(row: Mapping[str, Any]) -> tuple:
    """What two values must share to agree: value, unit after the one Portal spelling
    fold (``ontology/units.py``: ``KGS`` and ``kg`` agree) and currency."""
    return (row["value"], unit_token(row["unit"]), row["currency"])


def _basis_key(prop: Mapping[str, Any], rows: Iterable[Mapping[str, Any]],
               version: "_Version") -> frozenset:
    """The tax bases of a money property's values on ``version``'s record (stored with
    each value, else declared, as ``ontology.units.value_basis``); ``unknown`` equals only
    itself from the same binding (``ontology/units.py``)."""
    bid = version.binding["id"]
    bases = {r["tax_basis"] or money_basis(prop, bid, version.lineage) for r in rows}
    return frozenset((b, bid if b == "unknown" else None) for b in bases)


def _interval(*spans: tuple[str, str | None]) -> tuple[str, str | None] | None:
    start = max(span[0] for span in spans)
    ends = [span[1] for span in spans if span[1] is not None]
    end = min(ends) if ends else None
    return (start, end) if end is None or start < end else None


def _weaker(a: str, b: str) -> str:
    return a if _TIERS.index(a) >= _TIERS.index(b) else b


# --------------------------------------------------------------------------- parsers

def _blank_date(version: "_Version", raw: Any) -> bool:
    """A date cell of a BC record holding BC's empty date (``bc_blank_date``)."""
    return version.binding.get("source") == "bc" and bc_blank_date(raw)


def _parse(parser: str, value: Any, *, kind: str | None,
           normalizer: Normalizer | None) -> tuple[str | None, str | None]:
    """(value text, problem). None value text = source null."""
    if value is None:
        return None, None
    if parser == "as_is":
        return _text(value), None
    if parser == "trim":
        if isinstance(value, (dict, list)):
            return None, "not_text"
        return js_trim(_text(value)), None
    if parser == "decimal_text":
        number = _number(value)
        if number is None or not number.is_finite():
            return None, "not_decimal"
        return _plain(number), None   # exact: the text the source sent, never rounded
    if parser == "integer_text":
        if isinstance(value, bool):
            return None, "not_integer"
        if isinstance(value, int):
            return str(value), None
        if isinstance(value, float) and value.is_integer():
            return str(int(value)), None
        if isinstance(value, str) and _INTEGRAL_TEXT.fullmatch(value.strip()):
            return str(int(Decimal(value.strip()))), None
        if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
            return str(int(value.strip())), None
        return None, "not_integer"
    if parser == "iso_date":
        match = _DATE.match(value.strip()) if isinstance(value, str) else None
        if match is None:
            return None, "not_iso_date"
        try:
            return date(int(match[1]), int(match[2]), int(match[3])).isoformat(), None
        except ValueError:
            return None, "not_iso_date"
    if parser == "yyyymmdd_date":
        text = str(value).strip() if isinstance(value, (int, str)) and \
            not isinstance(value, bool) else None
        if text in ("0", "00000000"):
            return None, "zero_date"   # Sage/FM's empty date: not a source null, a gap
        match = _YYYYMMDD.fullmatch(text or "")
        if match is None:
            return None, "not_yyyymmdd_date"
        try:
            return date(int(match[1]), int(match[2]), int(match[3])).isoformat(), None
        except ValueError:
            return None, "not_yyyymmdd_date"
    if parser == "iso_timestamp":
        return _timestamp(value)
    if parser == "boolean_text":
        if isinstance(value, bool):
            return ("true" if value else "false"), None
        text = _text(value)
        text = text.strip().lower() if text is not None else ""
        if text in _TRUE:
            return "true", None
        if text in _FALSE:
            return "false", None
        return None, "not_boolean"
    if parser == "identifier":
        if normalizer is None or kind is None:
            return None, "identifier_normalizer_unavailable"
        if isinstance(value, (bool, dict, list, float)):
            return None, "identifier_not_text"
        if _blank(value) or identifiers.placeholder(kind, value):
            return None, None   # empty, whitespace-only or a stand-in ("N/A"): no value sent
        try:
            normalized = normalizer(kind, value)
        except Exception:  # noqa: BLE001 - a normalizer failure is a typed gap
            normalized = None
        if normalized is None:
            return None, "identifier_unparsed"
        return normalized, None
    raise ProjectionError("parser_unknown", parser)


def _timestamp(value: Any) -> tuple[str | None, str | None]:
    match = _TIMESTAMP.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None, "not_iso_timestamp"
    if match[8] is None:
        # No zone: the source's wall time, kept as written (never assumed UTC).
        fraction = (match[7] or "0")[:6].ljust(6, "0")
        try:
            moment = datetime(int(match[1]), int(match[2]), int(match[3]), int(match[4]),
                              int(match[5]), int(match[6] or 0), int(fraction))
        except ValueError:
            return None, "not_iso_timestamp"
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f"), None
    try:
        return instant(value), None
    except Exception:  # noqa: BLE001 - store.StoreError: invalid
        return None, "not_iso_timestamp"


_TYPE_CHECKS: dict[str, Callable[[str], bool]] = {
    "integer": lambda text: bool(_INTEGER_TEXT.fullmatch(text)),
    "decimal": lambda text: _number(text) is not None,
    "money": lambda text: _number(text) is not None,
    "quantity": lambda text: _number(text) is not None,
    "boolean": lambda text: text in ("true", "false"),
    "date": lambda text: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)),
    "timestamp": lambda text: bool(_TIMESTAMP.fullmatch(text)),
}


def _compare_norm(value: Any, compare: str, kind: str | None,
                  normalizer: Normalizer | None) -> tuple[str | None, str | None]:
    """(normalized key, problem) under a declared compare mode, never coerced across
    types (the semantics of operating/case_links.py ``_normalize``)."""
    if _blank(value):
        return None, "value_absent"
    if isinstance(value, bool):
        return None, "value_not_" + compare
    if compare == "integer_id":
        if isinstance(value, int):
            return (str(value), None) if value >= 0 else (None, "value_not_integer_id")
        if isinstance(value, float):
            if value.is_integer() and 0 <= value < 2 ** 53:
                return str(int(value)), None
            return None, "value_not_integer_id"
        if isinstance(value, str) and _INTEGRAL_TEXT.fullmatch(value.strip()):
            return str(int(Decimal(value.strip()))), None
        return None, "value_not_integer_id"
    if compare == "identifier":
        parsed, problem = _parse("identifier", value, kind=kind, normalizer=normalizer)
        return (parsed, None) if parsed is not None else (None, problem or "value_absent")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None, "value_not_" + compare
    if compare == "text":
        value = value.strip(" ")  # Postgres TRIM: spaces only
        if not value:
            return None, "value_absent"
    return value, None


# --------------------------------------------------------------------------- model

@dataclass
class _Version:
    object_id: str
    type_id: str
    binding: Mapping[str, Any]
    rec: CapturedRecord
    start: str
    end: str | None
    lineage: str | None = None
    values: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    vid: int = 0                      # row of the working file (projection order)

    @property
    def span(self) -> tuple[str, str | None]:
        return self.start, self.end

    def field(self, path: str) -> str:
        return f"{self.rec.field_prefix}.{path}"


# ``_side`` problems that mean a record holds no key (nothing to read), not an unread one.
_NO_KEY = frozenset({"value_absent", "never_sent_to_counterpart_system"})

# Gap codes whose every handle a build records (``handles``), not only samples.
RECORDED_GAPS = frozenset({"source_condition_unmet"})


@dataclass
class _Report:
    """Counts and gaps of a build. A gap keeps its count and its ``_SAMPLES``
    smallest distinct handles, so the report does not depend on the order versions
    are visited in; a ``RECORDED_GAPS`` gap keeps every handle too (``handles``).
    Every entry is also kept under the build ``unit`` it was reported in (``parts``;
    ``binding_unit`` / ``link_unit``), which lets an incremental build reuse the report
    of an unchanged unit (:func:`merge_reports`)."""

    counts: dict[str, int] = field(default_factory=dict)
    gaps: dict[str, dict[str, Any]] = field(default_factory=dict)
    unit: str | None = None
    parts: dict[str, "_Report"] = field(default_factory=dict)
    unattributed: int = 0

    def gap(self, code: str, where: str, handle: str | None = None, count: int = 1) -> None:
        self._gap(code, where, handle, count)
        self._part()._gap(code, where, handle, count)

    def _gap(self, code: str, where: str, handle: str | None, count: int = 1) -> None:
        entry = self.gaps.setdefault(f"{code}:{where}", {"code": code, "where": where,
                                                         "count": 0, "samples": []})
        entry["count"] += count
        samples = entry["samples"]
        if handle is not None and handle not in samples and (
                len(samples) < _SAMPLES or handle < samples[-1]):
            entry["samples"] = _smallest(samples, [handle])
        if code in RECORDED_GAPS and handle is not None:
            entry.setdefault("handles", set()).add(handle)

    def add(self, name: str, amount: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + amount
        part = self._part()
        part.counts[name] = part.counts.get(name, 0) + amount

    def _part(self) -> "_Report":
        if self.unit is None:
            self.unattributed += 1
            return _Report()
        part = self.parts.get(self.unit)
        if part is None:
            part = self.parts[self.unit] = _Report()
        return part

    def as_dict(self) -> dict[str, Any]:
        return {"counts": dict(sorted(self.counts.items())),
                "gaps": [_listed(self.gaps[key]) for key in sorted(self.gaps)]}


def _listed(gap: Mapping[str, Any]) -> dict[str, Any]:
    return {**gap, "handles": sorted(gap["handles"])} if "handles" in gap else dict(gap)


def _smallest(samples: Iterable[str], more: Iterable[str]) -> list[str]:
    return sorted(set(samples) | set(more))[:_SAMPLES]


def merge_reports(parts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """One report (``_Report.as_dict`` shape) of several unit reports: counts summed,
    gap counts summed and samples the smallest handles of all, so merging the parts
    of a build gives its whole report."""
    counts: dict[str, int] = {}
    gaps: dict[str, dict[str, Any]] = {}
    for part in parts:
        for name, amount in part.get("counts", {}).items():
            counts[name] = counts.get(name, 0) + amount
        for gap in part.get("gaps", []):
            entry = gaps.setdefault(f"{gap['code']}:{gap['where']}", {
                "code": gap["code"], "where": gap["where"], "count": 0, "samples": []})
            entry["count"] += gap["count"]
            entry["samples"] = _smallest(entry["samples"], gap["samples"])
            if "handles" in gap:
                entry.setdefault("handles", set()).update(gap["handles"])
    return {"counts": dict(sorted(counts.items())),
            "gaps": [_listed(gaps[key]) for key in sorted(gaps)]}


@lru_cache(maxsize=1024)
def _markings_text(markings: frozenset[str]) -> str:
    """``markings_text`` of one markings set (a build writes millions of rows with a
    few dozen distinct sets)."""
    return markings_text(markings)


def _default_normalizer() -> Normalizer | None:
    try:
        from ontology import identifiers  # L6; absent until it lands
    except ImportError:
        return None
    normalize = getattr(identifiers, "normalize", None)
    return normalize if callable(normalize) else None


# --------------------------------------------------------------------------- working file

DEFAULT_BATCH = 2000
_VERSION_CACHE = 4096
_SCRATCH_CACHE_KIB = 65536
_REC_FIELDS = tuple(f.name for f in fields(CapturedRecord))
_SCRATCH_DDL = (
    # Every captured record a binding keeps, before its object passes the identity
    # checks (key unique, version ranges sound); merge = its ``stands_for`` key;
    # natural/rank = its ETL natural key and keep order (``ETL_REINSERTED``). rec is
    # the record, compressed with its binding's dictionary (``_RecCodec``); the one
    # copy of it the working file holds.
    """CREATE TABLE stage (seq INTEGER PRIMARY KEY, binding TEXT NOT NULL,
        type_id TEXT NOT NULL, object_id TEXT NOT NULL, record_key TEXT NOT NULL,
        handle TEXT NOT NULL,
        prefix TEXT NOT NULL, digest TEXT NOT NULL, start TEXT NOT NULL, end_ TEXT,
        audience TEXT NOT NULL, missing TEXT, merge TEXT, rec BLOB NOT NULL,
        removals TEXT, natural TEXT, rank TEXT)""",
    "CREATE INDEX stage_by_object ON stage(binding, object_id, start, handle, seq)",
    "CREATE INDEX stage_by_merge ON stage(binding, merge)",
    "CREATE TABLE accepted (seq INTEGER PRIMARY KEY)",
    # The objects of one binding dropped as ETL re-inserted copies (``ETL_REINSERTED``).
    "CREATE TABLE collapsed (object_id TEXT PRIMARY KEY, handle TEXT NOT NULL)",
    # Projected versions; vid is the projection order (object id, then time). sid is
    # the version's stage row, whose rec it reads: the record is never copied again
    # (decision projection-disk-need: one copy of the captures in the working file).
    """CREATE TABLE v (vid INTEGER PRIMARY KEY, binding TEXT NOT NULL, type_id TEXT NOT NULL,
        object_id TEXT NOT NULL, handle TEXT NOT NULL, prefix TEXT NOT NULL,
        digest TEXT NOT NULL, start TEXT NOT NULL, end_ TEXT, lineage TEXT,
        sid INTEGER NOT NULL)""",
    "CREATE INDEX v_by_binding ON v(binding, vid)",
    "CREATE INDEX v_by_object ON v(object_id, start)",
    # Normalized compare keys: sig = (binding, path, compare mode, identifier kind).
    "CREATE TABLE k (sig INTEGER NOT NULL, norm TEXT NOT NULL, vid INTEGER NOT NULL)",
    "CREATE INDEX k_by_norm ON k(sig, norm, vid)",
    "CREATE INDEX k_by_vid ON k(sig, vid)",
    # Candidate pairs of one link binding: (from vid, pair index, other vid).
    "CREATE TABLE cand (a INTEGER NOT NULL, i INTEGER NOT NULL, b INTEGER NOT NULL)",
    "CREATE INDEX cand_by_a ON cand(a, i, b)",
    # Projected links (seq = emit order); row = the link row with vid citations.
    """CREATE TABLE links (seq INTEGER PRIMARY KEY, link_type TEXT NOT NULL,
        binding_kind TEXT, from_object TEXT NOT NULL, to_object TEXT NOT NULL,
        from_vid INTEGER NOT NULL, to_vid INTEGER NOT NULL, from_type TEXT NOT NULL,
        to_type TEXT NOT NULL, tier TEXT NOT NULL, recorded_at TEXT NOT NULL,
        superseded_at TEXT, row BLOB NOT NULL)""",
    "CREATE INDEX links_by_from_vid ON links(link_type, from_vid, seq)",
    "CREATE INDEX links_by_from ON links(link_type, from_object)",
    "CREATE INDEX links_by_to ON links(link_type, to_object)",
)


STALE_BUILD_HOURS = 24.0


def sweep_stale_build_files(directory: str | Path, store_name: str | None = None, *,
                            max_age_hours: float = STALE_BUILD_HOURS,
                            now: float | None = None) -> list[str]:
    """Remove ``.plx-projection-*.work`` and ``.<store_name>.build-*`` files left in
    ``directory`` by a crashed build. A file goes only when it is older than
    ``max_age_hours`` AND no live build holds it: a build keeps its SQLite write
    lock for its whole transaction, so a file that refuses ``BEGIN IMMEDIATE`` is in
    use and stays. Returns the names removed."""
    directory = Path(directory)
    patterns = [".plx-projection-*.work"]
    if store_name:
        patterns.append(f".{store_name}.build-*")
    cutoff = (time.time() if now is None else now) - max_age_hours * 3600
    removed: list[str] = []
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            if path.name.endswith("-journal"):
                continue
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_mtime > cutoff:
                    continue
                if _held_by_a_live_build(path):
                    continue
                for suffix in ("", "-journal"):
                    try:
                        Path(str(path) + suffix).unlink()
                    except FileNotFoundError:
                        pass
                removed.append(path.name)
            except OSError:
                continue
    return removed


def _held_by_a_live_build(path: Path) -> bool:
    try:
        probe = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError as error:
        return "locked" in str(error).lower() or "busy" in str(error).lower()
    except sqlite3.Error:
        return False
    finally:
        probe.close()


def _file_bytes(path: Path | None) -> int:
    """The size of ``path``; 0 when there is none."""
    try:
        return os.path.getsize(path) if path is not None else 0
    except OSError:
        return 0


def _db_bytes(conn: sqlite3.Connection, path: Path | None) -> int:
    """What a database open in one long transaction occupies, or will once its
    cache spills: its pages (written or still dirty in the cache), at least its
    file. The file alone understates a build mid-transaction."""
    pages, size = (conn.execute(f"PRAGMA {name}").fetchone()[0]
                   for name in ("page_count", "page_size"))
    return max(pages * size, _file_bytes(path))


def _drop_scratch(conn: sqlite3.Connection, path: Path) -> None:
    try:
        conn.close()
    finally:
        for suffix in ("", "-journal"):
            try:
                Path(str(path) + suffix).unlink()
            except FileNotFoundError:
                pass


def _pack(value: Any) -> bytes:
    # The working file is private to this build (created and removed by it), so the
    # exact Python values of a link row (Decimal, float, int, nested JSON) survive.
    # Compressed (zlib, fastest level): link rows are repetitive JSON, and the
    # working file's size is most of a build's disk need beyond its store. Stage
    # records use ``_RecCodec`` (a dictionary per binding) instead.
    return zlib.compress(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL), 1)


def _unpack(blob: bytes) -> Any:
    return pickle.loads(zlib.decompress(blob))  # noqa: S301 - this process's own file


_ZDICT_RECORDS = 8
_ZDICT_BYTES = 32768
_REC_LEVEL = 6


class _RecCodec:
    """Stage-record compression with one preset dictionary per binding.

    A capture row compressed alone repeats every column name and the shared values
    its binding's rows all carry; per-record zlib leaves ~46% of the pickle. Primed
    with the tail of the binding's first ``_ZDICT_RECORDS`` pickles, each record
    compresses to ~10% (measured on the mirror: 83.5 MB of pickles -> 38.7 MB plain
    level 1, 8.7 MB primed level 6, and faster, since ``copy()`` of a primed
    compressor skips re-reading the dictionary). The dictionaries live in memory
    only (a few dozen KiB per binding) and serve this build's own working file; a
    binding without one (no records yet) compresses plain, which a primed
    decompressor reads unchanged (a zlib stream names whether it needs the
    dictionary)."""

    def __init__(self) -> None:
        self._dicts: dict[str, bytes] = {}
        self._primed: dict[str, Any] = {}

    def prime(self, binding: str, pickles: Sequence[bytes]) -> None:
        """Fix ``binding``'s dictionary from its first pickles; once, never changed,
        so every blob of the binding decodes with the one dictionary."""
        if binding in self._dicts or not pickles:
            return
        zdict = b"".join(pickles)[-_ZDICT_BYTES:]
        self._dicts[binding] = zdict
        self._primed[binding] = zlib.compressobj(_REC_LEVEL, zdict=zdict)

    def primed(self, binding: str) -> bool:
        return binding in self._dicts

    def pack(self, binding: str, raw: bytes) -> bytes:
        primed = self._primed.get(binding)
        if primed is None:
            return zlib.compress(raw, _REC_LEVEL)
        out = primed.copy()
        return out.compress(raw) + out.flush()

    def unpack(self, binding: str, blob: bytes) -> CapturedRecord:
        reader = zlib.decompressobj(zdict=self._dicts.get(binding, b""))
        raw = reader.decompress(blob) + reader.flush()
        return CapturedRecord(**dict(zip(_REC_FIELDS, pickle.loads(raw))))  # noqa: S301


def _rec_pickle(rec: CapturedRecord) -> bytes:
    return pickle.dumps(tuple(getattr(rec, name) for name in _REC_FIELDS),
                        protocol=pickle.HIGHEST_PROTOCOL)


def _portable(features: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Link features with their ``_cite`` versions replaced by working-file vids."""
    out = []
    for feature in features:
        item = dict(feature)
        if "_cite" in item:
            item["_cite"] = [(version.vid, path) for version, path in item["_cite"]]
        out.append(item)
    return out


# --------------------------------------------------------------------------- projector

class Projector:
    """One deterministic projection of L1 captures under one compiled schema.

    Bounded memory (decision projection-memory, option a): a binding's capture rows
    stream into a private on-disk working file (SQLite, next to the store) and the
    store's rows are written into the open :class:`StoreBuilder` transaction batch by
    batch. Cross-binding work reads back from disk, never from an in-memory object
    map: link candidates are SQL joins over normalized compare keys, hops and event
    qualifiers are indexed link lookups, and same-record conflicts and capture events
    read values from the builder's own (uncommitted) tables. Python holds one batch
    (``batch_size`` versions), a bounded version cache and the report. The phases and
    their iteration order are those of the in-memory build, so objects, links, tiers,
    gaps (with their samples), the build report and the content digest do not depend
    on ``batch_size``.
    """

    def __init__(self, schema: Any, sources: Mapping[str, CaptureSource], *,
                 normalizer: Normalizer | None = None,
                 as_known_at: Any = None, batch_size: int = DEFAULT_BATCH,
                 scratch_dir: str | Path | None = None,
                 scope: Iterable[str] | None = None,
                 emit: Iterable[str] | None = None) -> None:
        self.schema = schema
        self.types: dict[str, Mapping[str, Any]] = dict(_get(schema, "object_types") or {})
        self.links: dict[str, Mapping[str, Any]] = dict(_get(schema, "link_types") or {})
        self.events: dict[str, Mapping[str, Any]] = dict(_get(schema, "event_types") or {})
        self.digest = _get(schema, "digest") or ""
        if not self.digest:
            raise ProjectionError("schema_digest_missing")
        self.sources = dict(sources)
        self.normalizer = normalizer if normalizer is not None else _default_normalizer()
        self.cut = known_at(as_known_at) if as_known_at is not None else None
        self.report = _Report()
        # (object, record handle, property) of each value whose currency a rule read, so
        # a conflict it takes part in is counted (money_rule.currency.conflicts.*).
        self._currency_ruled: set[tuple[str, str, str]] = set()
        self.bindings: dict[str, tuple[str, Mapping[str, Any]]] = {}
        # ``stands_for``: a binding that stands in for another (and that other one) ->
        # the pairs and side whose values make a record's merge key (``_merge_key``).
        self._merge: dict[str, tuple[Sequence[Mapping[str, Any]], str]] = {}
        self._stands_in: list[str] = []
        for tid, t in self.types.items():
            for b in t.get("bindings", []):
                self.bindings[b["id"]] = (tid, b)
                stands = b.get("stands_for")
                if stands:
                    self._stands_in.append(b["id"])
                    self._merge[b["id"]] = (stands["pairs"], "from")
                    self._merge.setdefault(stands["binding"], (stands["pairs"], "to"))
        self._check_schema()
        if batch_size < 1:
            raise ProjectionError("batch_size_invalid", str(batch_size))
        self.batch = int(batch_size)
        self.checkpoint: Callable[[], None] | None = None
        # Called when each phase (``STAGES``) ends with the bytes of the working file and
        # of the generation being built (with its journal): what that stage holds on disk.
        # Measurement only: never part of the build report or the digest.
        self.on_stage: Callable[[str, int, int], None] | None = None
        self._work_path: Path | None = None
        self.scratch_dir = Path(scratch_dir) if scratch_dir is not None else None
        # Partial build (ontology.project_incremental): only the bindings in ``scope``
        # are read, and only the rows of the units in ``emit`` are written. Every unit
        # in ``emit`` must have its dependencies in ``scope``. None = everything.
        self.scope = None if scope is None else frozenset(scope)
        self.emit = None if emit is None else frozenset(emit)
        self._path_marks: dict[tuple[str, str], frozenset[str]] = {}
        self._company_unknown: dict[tuple[str, int], bool] = {}
        self._sigs: dict[tuple[str, str, str, str | None, str | None], int] = {}
        self._unread_keys: dict[int, _Report] = {}   # per sig: the key values it could not read
        self._towards: dict[tuple[str, str, str, str | None, str], str | None] = {}
        self._source_order: dict[tuple[str, str, str], dict[str, int]] = {}
        self._cache: OrderedDict[int, _Version] = OrderedDict()
        self._cites: dict[int, tuple[str, str, str, str]] = {}
        self._recs = _RecCodec()  # the dictionaries of this working file's stage.rec
        self._locals: dict[tuple[str, str | None], list[int]] = {}
        self._conn: sqlite3.Connection | None = None
        self._finalizer: Any = None

    def _emits(self, unit: str) -> bool:
        return self.emit is None or unit in self.emit

    def _unit_of_link(self, link_type: str, kind: str | None, from_binding: str,
                      to_binding: str) -> str:
        return link_unit(link_type, -1, {"kind": kind, "from_binding": from_binding,
                                         "to_binding": to_binding}, self.bindings)

    # -- working file ----------------------------------------------------------

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            directory = self.scratch_dir or Path(tempfile.gettempdir())
            path = directory / f".plx-projection-{secrets.token_hex(8)}.work"
            # O_EXCL: never adopt a file another process (or a planted link) made;
            # 0600: the working file holds captured business rows.
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
            conn = sqlite3.connect(str(path), isolation_level=None)
            self._work_path = path
            self._finalizer = weakref.finalize(self, _drop_scratch, conn, path)
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA temp_store = FILE")
            conn.execute(f"PRAGMA cache_size = -{_SCRATCH_CACHE_KIB}")
            for statement in _SCRATCH_DDL:
                conn.execute(statement)
            conn.execute("BEGIN")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        """Remove the working file (the store being built is not touched)."""
        if self._finalizer is not None:
            self._finalizer()
        self._conn = None
        self._cache.clear()
        self._cites.clear()
        self._recs = _RecCodec()

    def _version(self, vid: int) -> _Version:
        """The version at ``vid``; one object per vid while it is cached, so a lineage
        computed through a hop is the lineage the version itself carries."""
        version = self._cache.get(vid)
        if version is not None:
            self._cache.move_to_end(vid)
            return version
        row = self._db.execute("SELECT v.binding, v.type_id, v.object_id, v.start, v.end_, "
                               "v.lineage, s.rec FROM v JOIN stage s ON s.seq = v.sid "
                               "WHERE v.vid = ?", (vid,)).fetchone()
        if row is None:
            raise ProjectionError("working_version_missing", str(vid))
        rec = self._recs.unpack(row[0], row[6])
        version = _Version(row[2], row[1], self.bindings[row[0]][1], rec, row[3], row[4],
                           lineage=row[5], vid=vid)
        self._cache[vid] = version
        if len(self._cache) > _VERSION_CACHE:
            self._cache.popitem(last=False)
        return version

    def _put_version(self, version: _Version) -> _Version:
        """Add one version to the working file at the next vid (unit tests)."""
        rec = version.rec
        vid = (self._db.execute("SELECT COALESCE(MAX(vid), 0) FROM v").fetchone()[0]) + 1
        sid = self._db.execute(
            "INSERT INTO stage (binding, type_id, object_id, record_key, handle, prefix, "
            "digest, start, end_, audience, rec) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version.binding["id"], version.type_id, version.object_id, rec.record_key,
             rec.l1_handle, rec.field_prefix, rec.digest, version.start, version.end,
             rec.audience_key, self._recs.pack(version.binding["id"], _rec_pickle(rec)))
        ).lastrowid
        self._db.execute(
            "INSERT INTO v VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, version.binding["id"], version.type_id, version.object_id, rec.l1_handle,
             rec.field_prefix, rec.digest, version.start, version.end, version.lineage, sid))
        version.vid = vid
        self._cache[vid] = version
        return version

    def _vids(self, binding: str | None = None) -> Iterator[int]:
        """Every vid (of one binding) in projection order, read a batch at a time."""
        last = 0
        while True:
            self._tick()
            if binding is None:
                rows = self._db.execute("SELECT vid FROM v WHERE vid > ? ORDER BY vid LIMIT ?",
                                        (last, self.batch)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT vid FROM v WHERE binding = ? AND vid > ? ORDER BY vid LIMIT ?",
                    (binding, last, self.batch)).fetchall()
            if not rows:
                return
            for (vid,) in rows:
                yield vid
            last = rows[-1][0]

    def _versions(self, binding: str) -> Iterator[_Version]:
        """Every version of one binding in projection order, read a batch at a time in
        one query each. Not cached and never mutated: only for the phases that read
        versions after their lineage is final (or never read it)."""
        last, spec = 0, self.bindings[binding][1]
        while True:
            self._tick()
            rows = self._db.execute(
                "SELECT v.vid, v.type_id, v.object_id, v.start, v.end_, v.lineage, s.rec "
                "FROM v JOIN stage s ON s.seq = v.sid "
                "WHERE v.binding = ? AND v.vid > ? ORDER BY v.vid LIMIT ?",
                (binding, last, self.batch)).fetchall()
            if not rows:
                return
            for vid, type_id, object_id, start, end, lineage, blob in rows:
                rec = self._recs.unpack(binding, blob)
                yield _Version(object_id, type_id, spec, rec, start, end, lineage=lineage,
                               vid=vid)
            last = rows[-1][0]

    def _cite(self, vid: int) -> tuple[str, str, str, str]:
        """(l1 handle, field prefix, digest, lineage) of a version, read without its
        row (link citations are written after every lineage is final)."""
        cited = self._cites.get(vid)
        if cited is None:
            cited = self._db.execute("SELECT handle, prefix, digest, lineage FROM v "
                                     "WHERE vid = ?", (vid,)).fetchone()
            if cited is None:
                raise ProjectionError("working_version_missing", str(vid))
            if len(self._cites) >= _VERSION_CACHE:
                self._cites.pop(next(iter(self._cites)))
            self._cites[vid] = cited
        return cited

    def _bound(self) -> list[str]:
        """Bindings that projected at least one version, sorted."""
        return [row[0] for row in self._db.execute("SELECT DISTINCT binding FROM v ORDER BY 1")]

    # -- schema --------------------------------------------------------------

    def _check_schema(self) -> None:
        uses_identifiers = False
        for tid, t in self.types.items():
            for b in t.get("bindings", []):
                if any("via_link" in w for w in b.get("where", [])):
                    raise ProjectionError("where_via_link_unsupported",
                                          f"{tid} binding {b['id']}")
            for p in t.get("properties", []):
                if any(s.get("parser") == "identifier" for s in p["sources"]):
                    uses_identifiers = True
        for link in self.links.values():
            for lb in link["bindings"]:
                pairs = list(lb.get("pairs", []))
                via = lb.get("via") or {}
                pairs += list(via.get("from_pairs", [])) + list(via.get("to_pairs", []))
                if any(p["compare"] == "identifier" for p in pairs):
                    uses_identifiers = True
        if uses_identifiers and self.normalizer is None:
            raise ProjectionError("identifier_normalizer_unavailable",
                                  "the schema parses or compares identifiers; "
                                  "ontology.identifiers.normalize is not importable")

    def _type_marks(self, tid: str) -> frozenset[str]:
        return frozenset(self.types[tid].get("markings", []))

    def _prop_marks(self, tid: str, prop: Mapping[str, Any]) -> frozenset[str]:
        return self._type_marks(tid) | frozenset(prop.get("markings", []))

    def _marks_for_path(self, binding_id: str, path: str) -> frozenset[str]:
        """Markings of the properties a binding's path feeds (what a link reveals)."""
        key = (binding_id, path)
        if key not in self._path_marks:
            tid = self.bindings[binding_id][0]
            marks = set(self._type_marks(tid))
            for p in self.types[tid].get("properties", []):
                if any(s["binding"] == binding_id and s["path"] == path for s in p["sources"]):
                    marks |= set(p.get("markings", []))
            self._path_marks[key] = frozenset(marks)
        return self._path_marks[key]

    # -- phase 1: objects and versions ----------------------------------------

    def _load(self, builder: StoreBuilder) -> None:
        wanted: dict[str, set[str]] = {}
        unheld: dict[str, str] = {}   # binding -> what its source does not hold of it
        for bid, (tid, b) in sorted(self.bindings.items()):
            if self.scope is not None and bid not in self.scope:
                continue
            self.report.unit = binding_unit(bid)
            if self.types[tid].get("availability", {}).get("state") == "not_served":
                continue
            if b["source"] not in self.sources:
                self.report.gap("source_not_captured", bid)
                unheld[bid] = "source_not_captured"
                continue
            held = getattr(self.sources[b["source"]], "capture_gap", None)
            missing = held(b["relation"]) if held is not None else None
            if missing:  # what the source does not hold of this relation, explicitly
                self.report.gap(missing, bid)
                unheld[bid] = missing
                if missing == "source_not_captured":
                    continue
            wanted.setdefault(b["source"], set()).add(b["relation"])
        captured = {source: self.sources[source].records(sorted(relations))
                    for source, relations in sorted(wanted.items())}
        # A binding that stands in for another is projected after every other one, in
        # declared order: what it may hold depends on what they hold (``_stand_in``).
        stands_in = set(self._stands_in)
        for bid in sorted(set(self.bindings) - stands_in) + self._stands_in:
            tid, b = self.bindings[bid]
            if self.scope is not None and bid not in self.scope:
                continue
            records = captured.get(b["source"], {}).get(b["relation"])
            if records is None:
                continue
            self.report.unit = binding_unit(bid)
            target = (b.get("stands_for") or {}).get("binding")
            whole = None
            if target is not None and (target in unheld or self.scope is not None
                                       and target not in self.scope):
                # The stood-for records are not held whole, so an absent one is not
                # known to be gone: nothing stands in for it (fail closed), except in
                # the scopes the source holds them whole in (``_whole_scopes``).
                whole = self._whole_scopes(b, target, unheld)
                if whole is None:
                    self.report.gap(unheld.get(target, "source_not_captured"), bid)
                    continue
            self._project_binding(tid, b, records, builder, whole)
        # Number the kept versions in projection order (object id, then time). Only the
        # ordering columns and the stage row are sorted and written, never the record:
        # each version reads its rec from its stage row (``v.sid``). The rows no object
        # kept and the staging indexes go; their pages are reused by the later tables.
        self._db.execute(
            """INSERT INTO v (vid, binding, type_id, object_id, handle, prefix, digest, start,
                              end_, lineage, sid)
               SELECT ROW_NUMBER() OVER (ORDER BY s.object_id, s.start, s.handle, s.seq),
                      s.binding, s.type_id, s.object_id, s.handle, s.prefix, s.digest,
                      s.start, s.end_, NULL, s.seq
               FROM stage s JOIN accepted a ON a.seq = s.seq""")
        self._db.execute("DELETE FROM stage WHERE seq NOT IN (SELECT seq FROM accepted)")
        self._db.execute("DROP INDEX IF EXISTS stage_by_object")
        self._db.execute("DROP INDEX IF EXISTS stage_by_merge")
        self._db.execute("DELETE FROM accepted")

    def _whole_scopes(self, b: Mapping[str, Any], target: str, unheld: Mapping[str, str]
                      ) -> tuple[str, Callable[[CapturedRecord], str], frozenset[str]] | None:
        """For a binding standing in for ``target`` while ``target`` is not held whole:
        (the gap, the record's scope, the scopes its source holds ``target`` whole in), or
        None when no scope is (``target`` not captured or not in this partial build, or a
        source that does not tell scopes)."""
        source = self.sources.get(b["source"])
        stood = self.bindings[target][1]
        whole = getattr(source, "whole_scopes", None)
        scope_of = getattr(source, "scope_of", None)
        if (target not in unheld or unheld[target] == "source_not_captured"
                or self.scope is not None and target not in self.scope
                or stood["source"] != b["source"] or whole is None or scope_of is None):
            return None
        return unheld[target], scope_of, frozenset(whole(stood["relation"]))

    def _project_binding(self, tid: str, b: Mapping[str, Any],
                         records: Iterable[CapturedRecord], builder: StoreBuilder,
                         whole: tuple[str, Callable[[CapturedRecord], str],
                                      frozenset[str]] | None = None) -> None:
        staged: list[tuple] = []
        minted = ETL_REINSERTED.get(b["source"], {}).get(b["relation"])

        rid = b["id"]
        held = 0  # staged rows whose rec is still the raw pickle (before prime)

        def flush() -> None:
            nonlocal held
            if held:
                # The dictionary comes from the binding's first records whatever the
                # batch size, so the working file is the same for every batch.
                self._recs.prime(rid, [row[12] for row in staged[:held]])
                staged[:held] = [row[:12] + (self._recs.pack(rid, row[12]),) + row[13:]
                                 for row in staged[:held]]
                held = 0
            self._db.executemany(
                "INSERT INTO stage (binding, type_id, object_id, record_key, handle, prefix, "
                "digest, start, end_, audience, missing, merge, rec, removals, natural, "
                "rank) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                staged)
            staged.clear()

        for rec in records:
            start = instant(rec.recorded_at)
            end = instant(rec.superseded_at) if rec.superseded_at is not None else None
            if self.cut is not None:
                if start > self.cut:
                    continue
                if end is not None and end > self.cut:
                    end = None
            if not all(_predicate(rec.row, w) for w in b.get("where", [])):
                continue
            if whole is not None and whole[1](rec) not in whole[2]:
                # Its scope's stood-for records are not held whole (``_whole_scopes``).
                self.report.gap(whole[0], b["id"], rec.l1_handle)
                continue
            key = {}
            for path in b["key"]:
                present, value = _cell(rec.row, path)
                if not present or _blank(value) or isinstance(value, (dict, list, float)):
                    key = None
                    break
                key[path] = value
            if key is None:
                self.report.gap("binding_key_absent", b["id"], rec.l1_handle)
                continue
            if "from_capture" in b["lineage"] and not (
                    rec.capture_lineage and _BC_LINEAGE.fullmatch(rec.capture_lineage)):
                # The capture's company is its lineage; without one the version has no
                # lineage the store can hold and must never pair within "a" company.
                self.report.gap("bc_company_unknown", b["id"], rec.l1_handle)
                continue
            merge = self._merge_key(rec, b["id"]) if b["id"] in self._merge else None
            if merge is None and "stands_for" in b:
                self.report.gap("binding_key_absent", b["id"], rec.l1_handle)
                continue
            object_id = object_id_of(tid, b["id"], key)
            raw = _rec_pickle(rec)
            hold = not self._recs.primed(rid)
            held += hold
            staged.append((b["id"], tid, object_id, rec.record_key, rec.l1_handle, rec.field_prefix,
                           rec.digest, start, end, rec.audience_key,
                           rec.missing_from_source_since, merge,
                           raw if hold else self._recs.pack(rid, raw),
                           json.dumps(rec.removals) if rec.removals else None,
                           *(_natural_key(rec.row, minted, rec.record_key)
                             if minted is not None else (None, None))))
            if held >= _ZDICT_RECORDS or (len(staged) >= self.batch and not held):
                self._tick()
                flush()
        flush()
        if minted is not None:
            self._collapse_reinserted(b)
        if "stands_for" in b:
            self._stand_in(b)
        objects: list[dict[str, Any]] = []
        removals: list[dict[str, Any]] = []
        accepted: list[tuple[int]] = []
        write = self._emits(binding_unit(b["id"]))
        cursor = self._db.execute(
            "SELECT object_id, seq, record_key, handle, start, end_, audience, missing, "
            "removals FROM stage WHERE binding = ? ORDER BY object_id, start, handle, seq",
            (b["id"],))
        for object_id, group in groupby(cursor, key=lambda row: row[0]):
            versions = list(group)
            if len({row[2] for row in versions}) > 1:
                # Several source records answer to one binding key: never guess which.
                self.report.gap("binding_key_not_unique", b["id"], versions[0][3])
                continue
            # Overlapping, zero-length or inverted ranges (a clock step, two captures
            # finishing at one instant): that object is skipped and reported; one bad
            # record never blocks the store.
            if any(row[5] is not None and row[5] <= row[4] for row in versions) or any(
                    prev[5] is None or prev[5] > nxt[4] or prev[4] == nxt[4]
                    for prev, nxt in zip(versions, versions[1:])):
                self.report.gap("capture_versions_overlap", b["id"], versions[0][3])
                continue
            accepted += [(row[1],) for row in versions]
            first, last = versions[0], versions[-1]
            missing = last[7] if last[5] is None else None
            if missing is not None:
                missing = instant(missing)
                if self.cut is not None and missing > self.cut:
                    missing = None
            removals += self._removals(object_id, missing,
                                       json.loads(last[8]) if last[5] is None and last[8]
                                       else (), lambda key: _successor_id(tid, b, key))
            objects.append({
                "object_id": object_id, "object_type": tid, "binding_id": b["id"],
                "native_key": native_key_of(object_id),
                # The CURRENT (latest) version's audience governs every read, including
                # as_known_at reads of older versions: a narrowed ACL applies at once.
                "audience_key": last[6], "first_recorded_at": first[4],
                "missing_from_source_since": missing})
            if len(objects) >= self.batch:
                self._tick()
                if write:
                    builder.add("objects", objects)
                    builder.add("object_removals", removals)
                objects.clear()
                removals.clear()
            if len(accepted) >= self.batch:
                self._tick()
                self._db.executemany("INSERT INTO accepted (seq) VALUES (?)", accepted)
                accepted.clear()
        if write:
            builder.add("objects", objects)
            builder.add("object_removals", removals)
        self._db.executemany("INSERT INTO accepted (seq) VALUES (?)", accepted)

    def _removals(self, object_id: str, missing: str | None,
                  stretches: Iterable[Sequence[Any]],
                  successor_id: Callable[[Any], str | None]) -> list[dict[str, Any]]:
        """The ``object_removals`` rows of one object: its open absence and the
        stretches its capture reports, as known at the cut (one later is not yet known;
        one ending after it is still open); empty stretches are dropped and one start
        keeps its longest end. A replacement label keeps its successors as object ids
        (``successor_id`` of each successor key); one a key cannot name is dropped,
        and the stretch stays a plain absence."""
        found: dict[tuple[str, str], str | None] = {}
        replaced: dict[tuple[str, str], tuple[str, str]] = {}
        if missing is not None:
            found[(missing, "missing_from_source")] = None
        for since, until, basis, *replacement in stretches:
            if basis not in store_ddl.REMOVAL_BASES or len(replacement) not in (0, 2):
                raise ProjectionError("capture_removal_invalid", f"{object_id}: {basis}")
            label = _replacement(replacement, successor_id)
            if label is not None:
                replaced[(instant(since), basis)] = label
            since = instant(since)
            until = None if until is None else instant(until)
            if self.cut is not None:
                if since > self.cut:
                    continue
                if until is not None and until > self.cut:
                    until = None
            if until is not None and until <= since:
                continue
            if (since, basis) in found:
                held = found[(since, basis)]
                until = None if held is None or until is None else max(held, until)
            found[(since, basis)] = until
        return [{"object_id": object_id, "since": since, "until": until, "basis": basis,
                 "replaced_basis": replaced.get((since, basis), (None, None))[0],
                 "replaced_by": replaced.get((since, basis), (None, None))[1]}
                for (since, basis), until in sorted(found.items())]

    def _merge_key(self, rec: CapturedRecord, binding_id: str) -> str | None:
        """What a record names under ``stands_for``: its company (a BC capture's
        ``bc_<book>``) and the normalized values of the pairs' paths on its side, or None
        when one is absent or does not normalize."""
        pairs, side = self._merge[binding_id]
        parts = [(rec.capture_lineage or "")
                 if self.bindings[binding_id][1]["source"] == "bc" else ""]
        for pair in pairs:
            present, value = _cell(rec.row, pair[side])
            norm = _compare_norm(value, pair["compare"], pair.get("identifier_kind"),
                                 self.normalizer)[0] if present else None
            if norm is None:
                return None
            parts.append(norm)
        return canonical(parts)

    def _collapse_reinserted(self, b: Mapping[str, Any]) -> None:
        """Keep one object per natural key of a binding over an ETL re-inserted relation
        (``ETL_REINSERTED``): an object's natural key, audience, source presence and
        keep order are its latest version's. Copies are one only under one audience (a
        collapse never moves a row between readers) and only when all are still in the
        source or all are removed from it (a removed copy keeps its own removal, which
        a present copy never absorbs). The object kept is the first in keep order, then
        the smallest object id; every version of each other object leaves the stage,
        reported."""
        bid = b["id"]
        cut = self.cut

        def removed(missing: Any, end: Any) -> int:
            # Missing as the object row will say: its current version's absence, as
            # known at the cut (an absence after the cut is not yet known).
            if end is not None or missing is None:
                return 0
            return 0 if cut is not None and instant(missing) > cut else 1

        self._db.create_function("plx_removed", 2, removed, deterministic=True)
        self._db.execute("DELETE FROM collapsed")
        self._db.execute(
            """INSERT INTO collapsed (object_id, handle)
               SELECT object_id, handle FROM (
                   SELECT object_id, handle, ROW_NUMBER() OVER (
                       PARTITION BY natural, audience, plx_removed(missing, end_)
                       ORDER BY rank, object_id) AS r
                   FROM (SELECT object_id, handle, natural, audience, missing, end_, rank,
                         ROW_NUMBER() OVER (
                             PARTITION BY object_id ORDER BY start DESC, seq DESC) AS n
                         FROM stage WHERE binding = ?)
                   WHERE n = 1)
               WHERE r > 1""", (bid,))
        self._db.execute("DELETE FROM stage WHERE binding = ? AND object_id IN "
                         "(SELECT object_id FROM collapsed)", (bid,))
        last, dropped = "", 0
        while True:   # reported a batch at a time (a gap keeps its count and samples)
            self._tick()
            rows = self._db.execute("SELECT object_id, handle FROM collapsed WHERE object_id > ? "
                                    "ORDER BY object_id LIMIT ?", (last, self.batch)).fetchall()
            if not rows:
                break
            for _, handle in rows:
                self.report.gap("etl_duplicate_collapsed", bid, handle)
            dropped += len(rows)
            last = rows[-1][0]
        if dropped:
            self.report.add("objects.etl_duplicates_collapsed", dropped)

    def _stand_in(self, b: Mapping[str, Any]) -> None:
        """Keep, of a binding that stands in for another, one record per merge key that no
        record of that binding, nor of a binding listed before this one standing in for
        it, holds: the first recorded (then the smallest record key), with every version
        of it. One identity per key: a key the stood-for binding holds is that record's."""
        bid, target = b["id"], b["stands_for"]["binding"]
        holders = [target] + [other for other in self._stands_in[:self._stands_in.index(bid)]
                              if self.bindings[other][1]["stands_for"]["binding"] == target]
        marks = ", ".join("?" * len(holders))
        held = self._db.execute(
            f"DELETE FROM stage WHERE binding = ? AND merge IN (SELECT merge FROM stage "
            f"WHERE binding IN ({marks}) AND merge IS NOT NULL)", (bid, *holders)).rowcount
        if held:
            self.report.add("objects.stood_for_held", held)
        self._db.execute(
            """DELETE FROM stage WHERE binding = ? AND seq NOT IN (
                   SELECT s.seq FROM stage s JOIN (
                       SELECT merge, record_key, ROW_NUMBER() OVER (
                           PARTITION BY merge ORDER BY MIN(start), record_key) AS r
                       FROM stage WHERE binding = ? GROUP BY merge, record_key) f
                   ON f.r = 1 AND s.merge = f.merge AND s.record_key = f.record_key
                   WHERE s.binding = ?)""", (bid, bid, bid))

    # -- phase 2: links ----------------------------------------------------------

    def _project_links(self) -> None:
        for lid in sorted(self.links):
            link = self.links[lid]
            for index, lb in enumerate(link["bindings"]):
                where = f"{lid}#{index}"
                if self.scope is not None and not link_ends(lb, self.bindings) <= self.scope:
                    continue
                self.report.unit = link_unit(lid, index, lb, self.bindings)
                if lb["kind"] not in _PROJECTED_LINK_KINDS:
                    self.report.gap("link_kind_not_projected_here", where)
                    continue
                if lb.get("from_binding") not in self.bindings or \
                        lb.get("to_binding") not in self.bindings:
                    self.report.gap("link_binding_unknown", where)
                    continue
                if "via" in lb:
                    self._link_via(lid, link, index, lb)
                else:
                    self._link_pairs(lid, link, index, lb)

    def _toward(self, binding: str, path: str, pair: Mapping[str, Any], other: str
                ) -> str | None:
        """The source of ``other`` when it receives the identifier ``binding`` holds at
        ``path`` in a declared sent form (``identifiers.sent_rule``: a Portal product code
        is a BC item No. only as cut to 20 characters), else None: compared as held."""
        key = (binding, path, pair["compare"], pair.get("identifier_kind"), other)
        if key not in self._towards:
            here, there = self.bindings[binding][1], self.bindings[other][1]
            sent = (pair["compare"] == "identifier" and here["source"] != there["source"]
                    and identifiers.sent_rule(pair.get("identifier_kind"), here["relation"],
                                              path, there["source"]) is not None)
            self._towards[key] = there["source"] if sent else None
        return self._towards[key]

    def _side(self, version: _Version, path: str, pair: Mapping[str, Any],
              toward: str | None = None) -> tuple[str | None, str | None, Any]:
        present, value = _cell(version.rec.row, path)
        if not present:
            return None, "value_absent", None
        if toward is not None and not _blank(value):
            value = identifiers.sent_form(pair.get("identifier_kind"),
                                          version.binding["relation"], path, value, toward)
            if value is None:
                return None, "never_sent_to_counterpart_system", None
        norm, problem = _compare_norm(value, pair["compare"], pair.get("identifier_kind"),
                                      self.normalizer)
        return norm, problem, value

    def _sig(self, binding: str, path: str, pair: Mapping[str, Any], other: str) -> int:
        """The compare-key set of ``binding`` at ``path`` under ``pair``'s compare mode,
        as compared with ``other`` (``_toward``), written to the working file once per
        (binding, path, mode, kind, receiving source)."""
        toward = self._toward(binding, path, pair, other)
        key = (binding, path, pair["compare"], pair.get("identifier_kind"), toward)
        sig = self._sigs.get(key)
        if sig is None:
            sig = self._sigs[key] = len(self._sigs) + 1
            unread = self._unread_keys[sig] = _Report()
            rows: list[tuple[int, str, int]] = []
            for version in self._versions(binding):
                norm, problem, _ = self._side(version, path, pair, toward)
                if norm is not None:
                    rows.append((sig, norm, version.vid))
                elif problem not in _NO_KEY:
                    unread._gap(f"link_key_{problem}", "", version.rec.l1_handle)
                if len(rows) >= self.batch:
                    self._tick()
                    self._db.executemany("INSERT INTO k VALUES (?, ?, ?)", rows)
                    rows.clear()
            self._db.executemany("INSERT INTO k VALUES (?, ?, ?)", rows)
        return sig

    def _report_unread_keys(self, where: str, keys: Iterable[tuple[str, str, int]]) -> None:
        """Report, under ``<where>@<binding>.<path>``, the key values of one link binding
        its ``_sig`` sets could not read: such a record joins nothing, so an absence
        prover must see it (once per key path, however many pairs compare it)."""
        for binding, path, sig in sorted(set(keys)):
            for gap in self._unread_keys[sig].gaps.values():
                at = f"{where}@{binding}.{path}"
                for handle in gap["samples"]:
                    self.report.gap(gap["code"], at, handle)
                if gap["count"] > len(gap["samples"]):
                    self.report.gap(gap["code"], at, None, gap["count"] - len(gap["samples"]))

    def _candidates(self, joins: Sequence[tuple[int, int]]
                    ) -> Iterator[tuple[int, list[tuple[int, int]]]]:
        """(from vid, [(pair index, other vid)]) in from-vid order, for the
        (from sig, other sig) key joins of one link binding: what the in-memory
        build's per-pair index lookups returned, read a batch of from-versions at a
        time."""
        self._db.execute("DELETE FROM cand")
        for index, (fsig, tsig) in enumerate(joins):
            self._db.execute(
                """INSERT INTO cand (a, i, b) SELECT DISTINCT f.vid, ?, t.vid FROM k f
                   JOIN k t ON t.sig = ? AND t.norm = f.norm WHERE f.sig = ?""",
                (index, tsig, fsig))
        last = 0
        while True:
            self._tick()
            ends = self._db.execute("SELECT DISTINCT a FROM cand WHERE a > ? ORDER BY a LIMIT ?",
                                    (last, self.batch)).fetchall()
            if not ends:
                return
            rows = self._db.execute("SELECT a, i, b FROM cand WHERE a > ? AND a <= ? "
                                    "ORDER BY a, i, b", (last, ends[-1][0])).fetchall()
            for vid, group in groupby(rows, key=lambda row: row[0]):
                yield vid, [(row[1], row[2]) for row in group]
            last = ends[-1][0]

    def _features(self, left: _Version, right: _Version,
                  pairs: Sequence[Mapping[str, Any]], hop: str
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """(agree, differ, unknown) features of comparing two versions on ``pairs``."""
        agree, differ, unknown = [], [], []
        here, there = left.binding["id"], right.binding["id"]
        for pair in pairs:
            lnorm, lprob, _ = self._side(left, pair["from"], pair,
                                         self._toward(here, pair["from"], pair, there))
            rnorm, rprob, _ = self._side(right, pair["to"], pair,
                                         self._toward(there, pair["to"], pair, here))
            feature = {"feature": "pair", "hop": hop, "compare": pair["compare"],
                       "from_path": pair["from"], "to_path": pair["to"],
                       "from_handle": left.rec.l1_handle, "from_field": left.field(pair["from"]),
                       "to_handle": right.rec.l1_handle, "to_field": right.field(pair["to"])}
            if pair.get("identifier_kind"):
                feature["identifier_kind"] = pair["identifier_kind"]
            feature["_cite"] = [(left, pair["from"]), (right, pair["to"])]
            if lnorm is None or rnorm is None:
                unknown.append({**feature, "agrees": None, "outcome": "unknown",
                                "problem": lprob if lnorm is None else rprob})
            elif lnorm == rnorm:
                agree.append({**feature, "agrees": True, "outcome": "agree", "value": lnorm})
            else:
                differ.append({**feature, "agrees": False, "outcome": "differ", "from_value": lnorm,
                               "to_value": rnorm})
        return agree, differ, unknown

    def _scoped(self, left_binding: str, right_binding: str) -> bool:
        """Whether a hop between two bindings compares within one BC company."""
        return link_company_scope({"from_binding": left_binding, "to_binding": right_binding},
                                  self.bindings) == SAME_BC_COMPANY

    def _same_company(self, scoped: bool, left: _Version, right: _Version,
                      where: str) -> bool:
        """A scoped hop pairs two BC records only within one capture company."""
        if not scoped:
            return True
        companies = (_bc_company(left), _bc_company(right))
        if None in companies:
            self.report.gap("bc_company_unknown", where,
                            (left if companies[0] is None else right).rec.l1_handle)
            return False
        if companies[0] != companies[1]:
            self.report.add("links.cross_company_not_linked")
            return False
        return True

    def _binding_applies(self, lb: Mapping[str, Any], fv: _Version) -> bool:
        """Whether a conditional link binding (``where``) is tried on this from-record: a
        fallback binding fires only where the primary one cannot (e.g. the system id the
        primary pairs on is blank). Tested on the bound row alone, so phase 2 needs no
        resolved hop."""
        return all(_predicate(fv.rec.row, w) for w in lb.get("where", ()))

    def _link_pairs(self, lid: str, link: Mapping[str, Any], index: int,
                    lb: Mapping[str, Any]) -> None:
        pairs = lb["pairs"]
        joins = [(self._sig(lb["from_binding"], pair["from"], pair, lb["to_binding"]),
                  self._sig(lb["to_binding"], pair["to"], pair, lb["from_binding"]))
                 for pair in pairs]
        scoped = self._scoped(lb["from_binding"], lb["to_binding"])
        where = f"{lid}#{index}"
        self._report_unread_keys(where, [
            key for pair, (fsig, tsig) in zip(pairs, joins)
            for key in ((lb["from_binding"], pair["from"], fsig),
                        (lb["to_binding"], pair["to"], tsig))])
        for fvid, matches in self._candidates(joins):
            fv = self._version(fvid)
            if not self._binding_applies(lb, fv):
                continue
            candidates: dict[str, _Version] = {}
            for _, tvid in matches:
                tv = self._version(tvid)
                candidates.setdefault(tv.rec.l1_handle + "|" + tv.object_id, tv)
            found = []
            for _, tv in sorted(candidates.items()):
                if tv.object_id == fv.object_id:
                    continue
                if not self._same_company(scoped, fv, tv, where):
                    continue
                span = _interval(fv.span, tv.span)
                if span is None:
                    continue
                agree, differ, unknown = self._features(fv, tv, pairs, "from->to")
                if agree:
                    found.append((tv, span, agree, differ, unknown))
            self._emit(lid, link, index, lb, fv, found)

    def _link_via(self, lid: str, link: Mapping[str, Any], index: int,
                  lb: Mapping[str, Any]) -> None:
        via = lb["via"]
        if via["binding"] not in self.bindings:
            self.report.gap("link_binding_unknown", f"{lid}#{index}")
            return
        joins = [(self._sig(lb["from_binding"], pair["from"], pair, via["binding"]),
                  self._sig(via["binding"], pair["to"], pair, lb["from_binding"]))
                 for pair in via["from_pairs"]]
        onward = [(self._sig(via["binding"], pair["from"], pair, lb["to_binding"]),
                   self._sig(lb["to_binding"], pair["to"], pair, via["binding"]))
                  for pair in via["to_pairs"]]
        where = f"{lid}#{index}"
        self._report_unread_keys(where, [
            key for pairs, ends, sigs in ((via["from_pairs"], (lb["from_binding"], via["binding"]),
                                           joins),
                                          (via["to_pairs"], (via["binding"], lb["to_binding"]),
                                           onward))
            for pair, (fsig, tsig) in zip(pairs, sigs)
            for key in ((ends[0], pair["from"], fsig), (ends[1], pair["to"], tsig))])
        scoped_in = self._scoped(lb["from_binding"], via["binding"])
        scoped_out = self._scoped(via["binding"], lb["to_binding"])
        scoped_ends = self._scoped(lb["from_binding"], lb["to_binding"])
        for fvid, matches in self._candidates(joins):
            fv = self._version(fvid)
            if not self._binding_applies(lb, fv):
                continue
            found = []
            seen: set[int] = set()
            for _, jvid in matches:
                if jvid in seen:
                    continue
                seen.add(jvid)
                jv = self._version(jvid)
                if not self._same_company(scoped_in, fv, jv, where):
                    continue
                span1 = _interval(fv.span, jv.span)
                if span1 is None:
                    continue
                a1, d1, u1 = self._features(fv, jv, via["from_pairs"], "from->via")
                if d1 or u1 or not a1:
                    continue   # a join record joins only when every from-pair agrees
                targets: dict[str, _Version] = {}
                for tvid in self._onward(jvid, onward):
                    tv = self._version(tvid)
                    targets.setdefault(tv.rec.l1_handle + "|" + tv.object_id, tv)
                for _, tv in sorted(targets.items()):
                    if tv.object_id == fv.object_id:
                        continue
                    if not (self._same_company(scoped_out, jv, tv, where) and
                            self._same_company(scoped_ends, fv, tv, where)):
                        continue
                    span = _interval(span1, tv.span)
                    if span is None:
                        continue
                    a2, d2, u2 = self._features(jv, tv, via["to_pairs"], "via->to")
                    if d2 or u2 or not a2:
                        continue
                    found.append((tv, span, a1 + a2, [], [], jv))
            self._emit(lid, link, index, lb, fv, found)

    def _onward(self, vid: int, joins: Sequence[tuple[int, int]]) -> list[int]:
        """Versions a join record reaches on any of its onward pairs."""
        out: list[int] = []
        for jsig, tsig in joins:
            out += [row[0] for row in self._db.execute(
                """SELECT t.vid FROM k j JOIN k t ON t.sig = ? AND t.norm = j.norm
                   WHERE j.sig = ? AND j.vid = ? ORDER BY t.vid""", (tsig, jsig, vid))]
        return out

    def _emit(self, lid: str, link: Mapping[str, Any], index: int, lb: Mapping[str, Any],
              fv: _Version, found: list[tuple]) -> None:
        if not found:
            return
        from_type, to_type = self.types[link["from"]], self.types[link["to"]]
        # Clean targets are counted per capture company: a BC number recurs across
        # companies, and a count or list spanning them would tell a reader of one
        # company what another holds. Across companies each link is capped below
        # (``bc_company_not_known``); the reader sees the ones it may read.
        clean_targets: dict[str | None, set[str]] = {}
        for item in found:
            if not item[3]:
                clean_targets.setdefault(_bc_company(item[0]), set()).add(item[0].object_id)
        single = link["cardinality"] in ("N:1", "1:1")
        if single and lb.get("unique_target"):
            # ``unique_target``: an ambiguous key is left unresolved rather than capped to a
            # lead, so a reader is never shown a guessed target for this binding.
            kept = [item for item in found
                    if len(clean_targets.get(_bc_company(item[0]), ())) < 2]
            if len(kept) != len(found):
                self.report.add("links.ambiguous_target_not_linked")
            found = kept
            if not found:
                return
        if (lid, index) not in self._company_unknown:
            self._company_unknown[lid, index] = bc_company_not_known(lb, self.bindings)
        company_unknown = self._company_unknown[lid, index]
        marks = (frozenset(link.get("markings", [])) | self._type_marks(link["from"]) |
                 self._type_marks(link["to"]))
        for item in found:
            tv, span, agree, differ, unknown = item[:5]
            join = item[5] if len(item) > 5 else None
            tier = lb["tier_ceiling"]
            why_not: list[dict[str, Any]] = list(differ) + list(unknown)
            if company_unknown:
                capped = _weaker(tier, "lead")
                if capped != tier:
                    why_not.append({"feature": "tier_cap", "agrees": None, "cap": capped,
                                    "because": BC_COMPANY_NOT_KNOWN})
                tier = capped
            if any(f["compare"] == "text" for f in agree) and lb["kind"] != "text_match":
                capped = _weaker(tier, "stored_by_text_join")
                if capped != tier:
                    why_not.append({"feature": "tier_cap", "agrees": None, "cap": capped,
                                    "because": "free_text_compare_is_not_a_stored_key"})
                tier = capped
            if (lb["kind"] == "same_record" and from_type.get("anchor") and
                    to_type.get("anchor") and any(f["compare"] == "text" for f in agree)):
                capped = _weaker(tier, "lead")
                if capped != tier:
                    why_not.append({"feature": "tier_cap", "agrees": None, "cap": capped,
                                    "because": "anchor_records_never_join_on_text"})
                tier = capped
            if unknown:
                capped = _weaker(tier, "lead")
                if capped != tier:
                    why_not.append({"feature": "tier_cap", "agrees": None, "cap": capped,
                                    "because": "a_declared_pair_is_unknown"})
                tier = capped
            rivals = clean_targets.get(_bc_company(tv), set())
            if single and len(rivals) > 1 and not differ:
                capped = _weaker(tier, "lead")
                why_not.append({"feature": "ambiguous_target", "agrees": None, "candidates":
                                len(rivals), "cardinality": link["cardinality"],
                                **({"cap": capped} if capped != tier else {})})
                tier = capped
            if differ:
                tier = "contradicted"
            if lb["kind"] == "text_match" and tier not in ("stored_by_text_join", "lead",
                                                           "contradicted"):
                tier = "lead"
            handles = [fv.rec.l1_handle] + ([join.rec.l1_handle] if join else []) + \
                [tv.rec.l1_handle]
            path_marks: set[str] = set(marks)
            for f in agree + differ:
                left = {"from->to": lb["from_binding"], "from->via": lb["from_binding"],
                        "via->to": lb.get("via", {}).get("binding")}[f["hop"]]
                right = {"from->to": lb["to_binding"], "from->via": lb.get("via", {}).get(
                    "binding"), "via->to": lb["to_binding"]}[f["hop"]]
                path_marks |= self._marks_for_path(left, f["from_path"])
                path_marks |= self._marks_for_path(right, f["to_path"])
            link_id = "L:" + _sha("|".join([lid, str(index), *handles, fv.object_id,
                                            tv.object_id]))[:40]
            row = {"link_id": link_id, "link_type": lid, "from_object": fv.object_id,
                   "to_object": tv.object_id, "tier": tier, "binding_kind": lb["kind"],
                   "link_contract": lb.get("link_contract"), "reason": agree,
                   "why_not": why_not, "evidence_handles": list(dict.fromkeys(handles)),
                   "markings": frozenset(path_marks), "recorded_at": span[0],
                   "superseded_at": span[1], "_from": fv, "_to": tv}
            self._store_link(row)
            self.report.add(f"links.{tier}")

    def _store_link(self, row: Mapping[str, Any]) -> None:
        """One projected link into the working file. Its non-contradicted rows are
        the hops lineage, units and currencies follow (``_hop``), in emit order."""
        fv, tv = row["_from"], row["_to"]
        data = {k: v for k, v in row.items() if not k.startswith("_")}
        data["reason"] = _portable(row.get("reason", []))
        data["why_not"] = _portable(row.get("why_not", []))
        self._db.execute(
            """INSERT INTO links (link_type, binding_kind, from_object, to_object, from_vid,
               to_vid, from_type, to_type, tier, recorded_at, superseded_at, row)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["link_type"], row.get("binding_kind"), row["from_object"], row["to_object"],
             fv.vid, tv.vid, fv.type_id, tv.type_id, row["tier"], row["recorded_at"],
             row["superseded_at"], _pack(data)))

    # -- phase 3: lineage ------------------------------------------------------

    def _hop(self, version: _Version, link_id: str, where: str) -> _Version | None:
        """The record one declared N:1/1:1 hop away, if exactly one object answers at a
        stored-key tier. A hop that only lead/stored_by_text_join links reach is not
        followed (lineage, unit and currency never come from a text join) and is
        reported as ``hop_below_tier``."""
        targets = [(self._version(tvid), tier) for tvid, tier in self._db.execute(
            """SELECT to_vid, tier FROM links WHERE link_type = ? AND from_vid = ?
               AND tier <> 'contradicted' ORDER BY seq""", (link_id, version.vid)).fetchall()]
        strong = [tv for tv, tier in targets if tier in _STORED_KEY_TIERS]
        if targets and not strong:
            self.report.gap("hop_below_tier", where, version.rec.l1_handle)
        objects = {tv.object_id for tv in strong}
        if len(objects) != 1:
            return None
        versions = sorted(strong, key=lambda v: v.start)
        for tv in versions:
            if tv.start <= version.start and (tv.end is None or tv.end > version.start):
                return tv
        return versions[0]

    def _lineage(self, version: _Version, stack: frozenset[str] = frozenset()) -> str | None:
        if version.lineage is not None:
            return version.lineage
        unit, self.report.unit = self.report.unit, binding_unit(version.binding["id"])
        try:
            return self._lineage_of(version, stack)
        finally:
            self.report.unit = unit

    def _lineage_of(self, version: _Version, stack: frozenset[str]) -> str:
        spec = version.binding["lineage"]
        if "from_capture" in spec:
            lineage = version.rec.capture_lineage
            lineage = lineage if lineage and _BC_LINEAGE.fullmatch(lineage) else None
        else:
            lineage = None
            for rule in spec.get("rules", []):
                if all(self._rule_predicate(version, w) for w in rule["when"]):
                    lineage = rule["lineage"]
                    break
            if lineage is None and "inherit" in spec and version.rec.l1_handle not in stack:
                target = self._hop(version, spec["inherit"],
                                   f"{version.binding['id']} lineage inherit")
                if target is not None:
                    lineage = self._lineage(target, stack | {version.rec.l1_handle})
            if lineage is None:
                lineage = spec["default"]
        version.lineage = lineage
        if version.vid:
            self._db.execute("UPDATE v SET lineage = ? WHERE vid = ?", (lineage, version.vid))
        return lineage

    def _rule_predicate(self, version: _Version, predicate: Mapping[str, Any]) -> bool:
        if "via_link" in predicate:
            target = self._hop(version, predicate["via_link"],
                               f"{version.binding['id']} lineage rule")
            return target is not None and _predicate(target.rec.row, predicate)
        return _predicate(version.rec.row, predicate)

    # -- phase 4: values -------------------------------------------------------

    def _ref_cell(self, version: _Version, ref: Any) -> tuple[_Version | None, bool, Any]:
        """(record holding the cell, present, value) of a ref_path; no record when its
        one hop does not resolve."""
        if isinstance(ref, Mapping):
            target = self._hop(version, ref["via_link"],
                               f"{version.binding['id']} {ref['via_link']}.{ref['path']}")
            if target is None:
                return None, False, None
            return (target,) + _cell(target.rec.row, ref["path"])
        return (version,) + _cell(version.rec.row, ref)

    def _local_currency(self, version: _Version, holder: _Version | None, path: str,
                        value: Any) -> tuple[str | None, dict[str, str] | None, str | None]:
        """(code, citation, gap) of the local currency a blank currency cell ``path`` of
        ``holder`` means: the holder binding's ``local_currency`` read on the one record of
        its ``binding`` in the holder's lineage (one capture scope, a BC company), on that
        record's version in force at ``version``'s time, which the value cites beside its
        own record. The record's first version is in force from the start of the capture
        pass that first read it, so a document read earlier in that same pass (a BC capture
        reads companyInformation after the document sets) resolves to it. No code for any
        other cell, none or several such records or no text there; a
        ``local_currency_unversioned`` gap when no version of the record was in force then
        (a document from an earlier pass: never another version's code)."""
        local = holder.binding.get("local_currency") if holder is not None else None
        if local is None or local["blank"] != path or not isinstance(value, str) or \
                js_trim(value):
            return None, None, None
        key = (local["binding"], holder.lineage)
        if key not in self._locals:
            self._locals[key] = [vid for vid, in self._db.execute(
                "SELECT vid FROM v WHERE binding = ? AND lineage = ? ORDER BY start, vid",
                key)]
        versions = [self._version(vid) for vid in self._locals[key]]
        if len({v.object_id for v in versions}) != 1:
            return None, None, None
        at = next((v for v in versions if v.start <= version.start and
                   (v.end is None or v.end > version.start)), None)
        first = versions[0]
        if at is None and version.start < first.start and instant(
                first.rec.pass_started_at or first.rec.recorded_at) <= version.start:
            at = first   # read later in the pass that also read this record
        if at is None:
            return None, None, "local_currency_unversioned"
        present, code = _cell(at.rec.row, local["path"])
        if not present or not isinstance(code, str):
            return None, None, None
        return js_trim(code), {"object_id": at.object_id, "l1_handle": at.rec.l1_handle,
                               "field_path": at.field(local["path"]),
                               "digest": at.rec.digest, "lineage": holder.lineage}, None

    def _tax_basis(self, version: _Version, prop: Mapping[str, Any],
                   source: Mapping[str, Any], where: str) -> str:
        """A money value's tax basis on its own record: the cell ``tax_basis_from`` names
        (true is incl_tax, false excl_tax, anything else unknown with a
        ``tax_basis_unresolved`` gap), else the declared basis (``ontology/units.py``)."""
        if "tax_basis_from" not in source:
            return money_basis(prop, source["binding"], version.lineage)
        _, present, value = self._ref_cell(version, source["tax_basis_from"])
        if present and isinstance(value, bool):
            return "incl_tax" if value else "excl_tax"
        self.report.gap("tax_basis_unresolved", where, version.rec.l1_handle)
        return "unknown"

    def _measure(self, version: _Version, prop: Mapping[str, Any], source: Mapping[str, Any],
                 what: str, where: str, cites: list | None = None) -> str | None:
        """The unit or currency of a value; ``cites`` receives the citation of the record
        a currency was read on when that is not the value's own (``_local_currency``)."""
        spec = source if (what in source or what + "_from" in source or
                          what + "_unknown" in source) else prop
        if what in spec:
            return spec[what]
        if what + "_from" in spec:
            ref = spec[what + "_from"]
            holder, present, value = self._ref_cell(version, ref)
            cite = gap = None
            if present and not _blank(value) and not isinstance(value, (dict, list)):
                text = js_trim(_text(value))
            elif what == "currency":   # a blank code may name the local currency
                text, cite, gap = self._local_currency(version, holder, ref["path"] if (
                    isinstance(ref, Mapping)) else ref, value if present else None)
            else:
                text = None
            if not text:
                self.report.gap(gap or f"{what}_unresolved", where, version.rec.l1_handle)
                return None
            if what == "currency":
                code, rule = currency_code(text)
                if code is None:
                    self.report.gap("currency_invalid", where, version.rec.l1_handle)
                    self.report.add(f"currency_shape.{currency_shape(text)}")
                    return None
                if rule is not None:
                    self.report.add(f"money_rule.currency.{source['binding']}.{rule}")
                    self._currency_ruled.add((version.object_id, version.rec.l1_handle,
                                              prop["name"]))
                text = code
                if cite is not None and cites is not None:
                    cites.append(cite)
            return text[:40]
        return None   # *_unknown or not declared: NULL, a typed gap for the tools

    def _per_unit(self, version: _Version, source: Mapping[str, Any], where: str) -> str | None:
        """A unit price's denominator: the unit its source says the price is per, stored
        as the money value's unit; None when the source, or the record's lineage, declares
        it unknown (a writer stores a placeholder or another record's unit there). An
        unknown denominator is a ``price_denominator_unknown`` gap, never a guess."""
        if "per_unit_unknown" in source or \
                version.lineage in source.get("per_unit_unknown_by_lineage", {}):
            unit, undecided = self._per_unit_rule(version, source)
            if unit is not None:
                return unit
            if undecided is not None:
                self.report.add(f"money_rule.price_denominator.{source['binding']}."
                                f"undecided.{undecided}")
            self.report.gap("price_denominator_unknown", where, version.rec.l1_handle)
            return None
        return self._measure(version, source, source, "per_unit", where)

    def _per_unit_rule(self, version: _Version, source: Mapping[str, Any]
                       ) -> tuple[str | None, str | None]:
        """(unit, None) of the first ``per_unit_rules`` rule that decides the record's
        denominator, counted per rule; else (None, why none decided: ``unit_blank`` or
        ``units_disagree`` from the last ``agreeing`` rule, else ``no_rule_holds``; None
        for a source with no rules). ``when``
        rules hold a literal unit; ``agreeing`` reads unit cells of the record that must
        all be filled and fold (``unit_token``) to one unit, stored as the first cell's
        trimmed text: whichever of them the price is per, it is that unit."""
        rules = source.get("per_unit_rules", ())
        undecided = "no_rule_holds" if rules else None
        for rule in rules:
            if "agreeing" in rule:
                texts = []
                for path in rule["agreeing"]:
                    present, cell = _cell(version.rec.row, path)
                    texts.append(js_trim(cell) if present and isinstance(cell, str)
                                 else "")
                folded = {unit_token(text) for text in texts}
                if None in folded:
                    undecided = "unit_blank"
                    continue
                if len(folded) != 1:
                    undecided = "units_disagree"
                    continue
                unit = texts[0][:40]
            elif all(_predicate(version.rec.row, w) for w in rule["when"]):
                unit = rule["per_unit"]
            else:
                continue
            self.report.add(f"money_rule.price_denominator.{source['binding']}.{rule['id']}")
            return unit, None
        return None, undecided

    def _project_values(self, builder: StoreBuilder) -> None:
        values: list[dict[str, Any]] = []
        identifiers: list[dict[str, Any]] = []
        for bid in self._bound():
            if not self._emits(binding_unit(bid)):
                continue
            self.report.unit = binding_unit(bid)
            tid, binding = self.bindings[bid]
            t = self.types[tid]
            unique_keys = {name for key in t.get("identity_keys", []) if key["unique"]
                           for name in key["properties"]}
            sourced = [(prop, source) for prop in t.get("properties", [])
                       if prop.get("availability") != "not_served"
                       for source in prop["sources"] if source["binding"] == bid
                       and source.get("parser") != POST_PROJECT]
            for version in self._versions(bid):
                for prop, source in sourced:
                    self._value(version, t, tid, prop, source, unique_keys)
                self._placeholders(version, t, tid)
                self._rank(version, t)
                for name in sorted(version.values):
                    for row in version.values[name]:
                        # builder.add reads only the store's columns, never the "_" keys
                        row["markings"] = _markings_text(row["markings"])
                        values.append(row)
                        if row["value_type"] == "identifier" and row["value"] is not None:
                            identifiers.append({
                                "identifier_kind": row["_kind"], "normalized": row["value"],
                                "raw": _text(row["_raw"]), "role": row["_role"],
                                "object_id": version.object_id, "property": name,
                                "l1_handle": row["l1_handle"], "field_path": row["field_path"],
                                "lineage": row["lineage"], "markings": row["markings"],
                                "recorded_at": row["recorded_at"],
                                "superseded_at": row["superseded_at"]})
                version.values = {}
                if len(values) >= self.batch:
                    self._tick()
                    builder.add("object_values", values)
                    builder.add("identifiers", identifiers)
                    values.clear()
                    identifiers.clear()
        builder.add("object_values", values)
        builder.add("identifiers", identifiers)

    def _stored_values(self, builder: StoreBuilder, version: _Version,
                       name: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """A version's projected values, read back from the open store generation,
        per property in the order its sources are declared (the in-memory build's
        order)."""
        sql = ("SELECT property, value, unit, currency, authority_rank, field_path, "
               "l1_handle, digest, lineage, markings, tax_basis FROM object_values "
               "WHERE object_id = ? AND l1_handle = ?")
        params: tuple = (version.object_id, version.rec.l1_handle)
        if name is not None:
            sql += " AND property = ?"
            params += (name,)
        out: dict[str, list[dict[str, Any]]] = {}
        for row in builder.connection.execute(sql, params):
            out.setdefault(row[0], []).append({
                "value": row[1], "unit": row[2], "currency": row[3], "authority_rank": row[4],
                "field_path": row[5], "l1_handle": row[6], "digest": row[7], "lineage": row[8],
                "markings": frozenset(json.loads(row[9])), "tax_basis": row[10]})
        skip = len(version.rec.field_prefix) + 1
        for prop, rows in out.items():
            order = self._source_order.get((version.type_id, version.binding["id"], prop))
            if order is None:
                order = {}
                for p in self.types[version.type_id].get("properties", []):
                    if p["name"] == prop:
                        for source in p["sources"]:
                            if source["binding"] == version.binding["id"]:
                                order.setdefault(source["path"], len(order))
                self._source_order[(version.type_id, version.binding["id"], prop)] = order
            rows.sort(key=lambda r: order.get(r["field_path"][skip:], len(order)))
        return out

    def _value(self, version: _Version, t: Mapping[str, Any], tid: str,
               prop: Mapping[str, Any], source: Mapping[str, Any],
               unique_keys: set[str]) -> None:
        where = f"{tid}.{prop['name']}@{source['binding']}"
        if not all(self._rule_predicate(version, w) for w in source.get("when", ())):
            # The field means the property only on records that pass the source's tests.
            self.report.gap("source_condition_unmet", where, version.rec.l1_handle)
            return
        present, raw = _cell(version.rec.row, source["path"])
        if not present:
            self.report.gap("path_absent", where, version.rec.l1_handle)
            return
        if prop["value_type"] in ("date", "timestamp") and _blank_date(version, raw):
            raw = None   # BC's empty date: a source null, never year 1
        parser = source.get("parser", "as_is")
        text, problem = _parse(parser, raw, kind=prop.get("identifier_kind"),
                               normalizer=self.normalizer)
        value_type = prop["value_type"]
        if problem is None and text is not None and value_type in _TYPE_CHECKS and \
                not _TYPE_CHECKS[value_type](text):
            problem = f"not_{value_type}"
        if problem is not None:
            self.report.gap(f"parse_{problem}", where, version.rec.l1_handle)
            return
        if value_type == "enum" and text is not None and text not in prop["enum_values"]:
            self.report.gap("enum_value_undeclared", where, version.rec.l1_handle)
        unit = currency = basis = None
        cites: list[dict[str, str]] = []
        if text is not None:
            unit = (self._per_unit(version, source, where) if is_unit_price(prop)
                    else self._measure(version, prop, source, "unit", where))
            currency = self._measure(version, prop, source, "currency", where, cites)
            if value_type == "money":
                basis = self._tax_basis(version, prop, source, where)
        marks = self._prop_marks(tid, prop)
        row = {"object_id": version.object_id, "property": prop["name"],
               "value_type": value_type, "value": text, "unit": unit,
               "quantity_role": prop.get("quantity_role") if value_type == "quantity" else None,
               "currency": currency, "tax_basis": basis,
               "currency_citation": canonical(cites[0]) if cites else None,
               "l1_handle": version.rec.l1_handle,
               "field_path": version.field(source["path"]), "digest": version.rec.digest,
               "lineage": version.lineage, "binding_id": source["binding"], "parser": parser,
               "authority_rank": None, "markings": marks, "recorded_at": version.start,
               "superseded_at": version.end,
               "_raw": raw, "_role": "primary" if prop["name"] in unique_keys else "alias",
               "_kind": prop.get("identifier_kind")}
        version.values.setdefault(prop["name"], []).append(row)
        self.report.add("values")

    def _placeholders(self, version: _Version, t: Mapping[str, Any], tid: str) -> None:
        """A value equal to one of the same record's own identifiers named by the
        property's ``placeholder_when_equal_to`` (a fallback copy of the record's own
        number) is a self-reference, never a value of the property: it is stored as a
        source null on its field and reported (``placeholder_self_reference``). The rule
        is ``capability_guards.value_state``'s, so the projection and the gap guard
        read such a field alike."""
        for prop in t.get("properties", []):
            others = prop.get("placeholder_when_equal_to")
            rows = version.values.get(prop["name"])
            if not others or not rows:
                continue
            own = {_text(row["_raw"]).strip() for other in others
                   for row in version.values.get(other, ())
                   if row["value"] is not None and isinstance(row["_raw"], (int, str))
                   and not isinstance(row["_raw"], bool)}
            for row in rows:
                raw = row["_raw"]
                if row["value"] is not None and isinstance(raw, (int, str)) and \
                        not isinstance(raw, bool) and str(raw).strip() in own - {""}:
                    row["value"] = None
                    self.report.gap("placeholder_self_reference",
                                    f"{tid}.{prop['name']}@{row['binding_id']}",
                                    version.rec.l1_handle)

    def _rank(self, version: _Version, t: Mapping[str, Any]) -> None:
        props = {p["name"]: p for p in t.get("properties", [])}
        for name, rows in version.values.items():
            precedence = props[name].get("authority", {}).get("precedence", [])
            for row in rows:
                for rank, rule in enumerate(precedence):
                    if rule.get("binding", row["binding_id"]) != row["binding_id"]:
                        continue
                    if row["lineage"] not in rule["lineage"]:
                        continue
                    if "era" in rule and not self._in_era(version, rule["era"]):
                        continue
                    row["authority_rank"] = rank
                    break
            texts = {_conflict_key(r) for r in rows if r["value"] is not None}
            if len(texts) > 1:
                self.report.gap("conflict_within_record", f"{version.type_id}.{name}",
                                version.rec.l1_handle)

    def _in_era(self, version: _Version, era: Mapping[str, Any]) -> bool:
        basis = [r["value"] for r in version.values.get(era["basis"], [])
                 if r["value"] is not None]
        if len(set(basis)) != 1:
            return False
        day = basis[0][:10]
        return ((era.get("from") is None or day >= era["from"]) and
                (era.get("until") is None or day < era["until"]))

    # -- phase 5: conflicts across records of one business object --------------

    def _conflicts(self, builder: StoreBuilder) -> None:
        last = 0
        while True:
            self._tick()
            chunk = self._db.execute(
                """SELECT seq, from_vid, to_vid, row FROM links WHERE seq > ?
                   AND binding_kind = 'same_record' AND tier <> 'contradicted'
                   ORDER BY seq LIMIT ?""", (last, self.batch)).fetchall()
            if not chunk:
                return
            for seq, from_vid, to_vid, blob in chunk:
                fv, tv = self._version(from_vid), self._version(to_vid)
                if fv.type_id != tv.type_id:
                    continue
                row = _unpack(blob)
                unit = self._unit_of_link(row["link_type"], row["binding_kind"],
                                          fv.binding["id"], tv.binding["id"])
                if not self._emits(unit):
                    continue
                self.report.unit = unit
                if self._conflict(builder, row, fv, tv):
                    self._db.execute("UPDATE links SET row = ? WHERE seq = ?", (_pack(row), seq))
            last = chunk[-1][0]

    def _compared_keys(self, version: _Version, other: _Version, prop: Mapping[str, Any],
                       rows: Sequence[Mapping[str, Any]]) -> set[tuple]:
        """``_conflict_key`` of each value, an identifier in the form ``other``'s source
        receives it, as its link compared it (``_toward``: a Portal product code agrees
        with its BC item No. on its first 20 characters)."""
        keys = set()
        skip = len(version.rec.field_prefix) + 1
        pair = {"compare": "identifier", "identifier_kind": prop.get("identifier_kind")}
        for row in rows:
            key = _conflict_key(row)
            path = row["field_path"][skip:]
            if prop["value_type"] == "identifier" and self._toward(
                    version.binding["id"], path, pair, other.binding["id"]) is not None:
                sent = identifiers.sent_form(pair["identifier_kind"],
                                             version.binding["relation"], path, row["value"],
                                             other.binding["source"])
                key = (sent and self.normalizer(pair["identifier_kind"], sent),) + key[1:]
            keys.add(key)
        return keys

    def _conflict(self, builder: StoreBuilder, row: dict[str, Any], fv: _Version,
                  tv: _Version) -> bool:
        """Add a ``property_conflict`` feature per differing property; True if any."""
        changed = False
        props = {p["name"]: p for p in self.types[fv.type_id].get("properties", [])}
        fvalues, tvalues = self._stored_values(builder, fv), self._stored_values(builder, tv)
        for name in sorted(set(fvalues) & set(tvalues)):
            left = [r for r in fvalues[name] if r["value"] is not None]
            right = [r for r in tvalues[name] if r["value"] is not None]
            if not left or not right:
                continue
            if self._compared_keys(fv, tv, props[name], left) == \
                    self._compared_keys(tv, fv, props[name], right):
                continue
            if props[name]["value_type"] == "money" and \
                    _basis_key(props[name], left, fv) != _basis_key(props[name], right, tv):
                # Amounts on two tax bases (or an unknown one from another column) are
                # never compared, so a difference between them is no conflict.
                self.report.gap("conflict_not_compared_tax_basis", f"{fv.type_id}.{name}",
                                fv.rec.l1_handle)
                continue
            if is_unit_price(props[name]) and \
                    not same_denominator(r["unit"] for r in left + right):
                # Prices per two units (ea vs kg), or per an unknown one, are never
                # compared, so a difference between them is no conflict.
                self.report.gap("conflict_not_compared_price_denominator",
                                f"{fv.type_id}.{name}", fv.rec.l1_handle)
                continue
            currencies = {r["currency"] for r in left + right}
            if props[name]["value_type"] == "money" and None in currencies and \
                    len(currencies) > 1:
                # An amount in an unknown currency (a code no rule decides) is never
                # compared with one in a known currency: an unknown is not a difference.
                self.report.gap("conflict_not_compared_currency", f"{fv.type_id}.{name}",
                                fv.rec.l1_handle)
                continue
            self.report.gap("conflict_across_records", f"{fv.type_id}.{name}",
                            fv.rec.l1_handle)
            if is_unit_price(props[name]) and any(
                    "per_unit_rules" in s and s["binding"] in (fv.binding["id"],
                                                                tv.binding["id"])
                    for s in props[name]["sources"]):
                # Never compared before a rule decided its denominator: a new conflict,
                # counted so the live check can say no rule made one.
                self.report.add(f"money_rule.price_denominator.conflicts."
                                f"{fv.type_id}.{name}")
            if any((v.object_id, v.rec.l1_handle, name) in self._currency_ruled
                   for v in (fv, tv)):
                # Its currency a rule read (an invalid code before): a new conflict too.
                self.report.add(f"money_rule.currency.conflicts.{fv.type_id}.{name}")
            if not self._prop_marks(fv.type_id, props[name]) <= row["markings"]:
                continue   # never reveal a marked property's conflict on a weaker link
            best = lambda rows: min(rows, key=lambda r: (  # noqa: E731
                r["authority_rank"] is None, r["authority_rank"] or 0, r["field_path"]))
            lbest, rbest = best(left), best(right)
            ranks = (lbest["authority_rank"], rbest["authority_rank"])
            authority = None
            if ranks[0] is not None and (ranks[1] is None or ranks[0] < ranks[1]):
                authority = "from"
            elif ranks[1] is not None and (ranks[0] is None or ranks[1] < ranks[0]):
                authority = "to"
            row["why_not"].append({
                "feature": "property_conflict", "agrees": False, "property": name,
                "authority": authority,
                "citations": [{k: side[k] for k in ("l1_handle", "field_path", "digest",
                                                     "lineage")} for side in (lbest, rbest)],
                "from": {k: lbest[k] for k in ("l1_handle", "field_path", "lineage",
                                                "authority_rank")},
                "to": {k: rbest[k] for k in ("l1_handle", "field_path", "lineage",
                                              "authority_rank")}})
            changed = True
        return changed

    # -- phase 6: events ---------------------------------------------------------

    def _related(self, version: _Version, link_id: str, object_type: str,
                 where: str) -> list[str]:
        """Objects an event qualifier attaches through ``link_id``: only links at a
        stored-key tier (verified/reviewed/corroborated; ``event_objects`` has no tier,
        so a lead must never look like an attachment), and never several objects
        through a single-valued (N:1/1:1) hop. Dropped attachments are reported."""
        cardinality = self.links.get(link_id, {}).get("cardinality")
        out, below, single = set(), set(), False
        ends = self._db.execute(
            """SELECT 1, to_object, to_type, tier, recorded_at, superseded_at FROM links
               WHERE link_type = ? AND from_object = ?
               UNION ALL
               SELECT 0, from_object, from_type, tier, recorded_at, superseded_at FROM links
               WHERE link_type = ? AND to_object = ?""",
            (link_id, version.object_id, link_id, version.object_id)).fetchall()
        for forward, other, other_type, tier, recorded, superseded in ends:
            if other_type != object_type or other == version.object_id or \
                    not _interval((recorded, superseded), version.span):
                continue
            single = single or cardinality == "1:1" or (
                cardinality == ("N:1" if forward else "1:N"))
            if tier in _STORED_KEY_TIERS:
                out.add(other)
            else:
                below.add(other)
        for _ in sorted(below - out):
            self.report.gap("event_qualifier_below_tier", where, version.rec.l1_handle)
        if single and len(out) > 1:
            self.report.gap("event_qualifier_ambiguous", where, version.rec.l1_handle)
            return []
        return sorted(out)

    def _project_events(self, builder: StoreBuilder) -> None:
        events: list[dict[str, Any]] = []
        relations: dict[tuple[str, str, str], dict[str, Any]] = {}

        def flush() -> None:
            builder.add("events", events)
            builder.add("event_objects", [relations[key] for key in sorted(relations)])
            events.clear()
            relations.clear()

        bound = set(self._bound())
        for eid in sorted(self.events):
            ev = self.events[eid]
            tid = ev["object_type"]
            if tid not in self.types:
                continue
            marks = frozenset(ev.get("markings", [])) | self._type_marks(tid)
            for bid in sorted(b["id"] for b in self.types[tid].get("bindings", [])):
                if bid not in bound or not self._emits(binding_unit(bid)):
                    continue
                self.report.unit = binding_unit(bid)
                for version in self._versions(bid):
                    made = (self._capture_event(builder, eid, ev, version, marks)
                            if ev["time_basis"] == "capture_observed"
                            else self._date_event(eid, ev, version, marks))
                    for event, span in made:
                        events.append({**event, "markings": _markings_text(event["markings"])})
                        for q in ev["qualifiers"]:
                            if q["via"] == "self":
                                targets = [version.object_id]
                            else:
                                probe = _Version(version.object_id, tid, version.binding,
                                                 version.rec, span[0], span[1])
                                targets = self._related(probe, q["via"], q["object_type"],
                                                        f"{eid}.{q['qualifier']}")
                            for target in targets:
                                relations[(event["event_id"], target, q["qualifier"])] = {
                                    "event_id": event["event_id"], "object_id": target,
                                    "qualifier": q["qualifier"]}
                        self.report.add("events")
                    if len(events) >= self.batch:
                        self._tick()
                        flush()
        flush()

    def _date_event(self, eid: str, ev: Mapping[str, Any], version: _Version,
                    marks: frozenset[str]) -> list[tuple[dict[str, Any], tuple]]:
        spec = ev["time"]
        if spec["binding"] != version.binding["id"]:
            return []
        present, raw = _cell(version.rec.row, spec["path"])
        if not present or raw is None or _blank_date(version, raw):
            return []   # no date, or BC's empty date: no event
        parser = spec.get("parser")
        if parser is None:
            text, problem = _timestamp(raw)
            if problem:
                text, problem = _parse("iso_date", raw, kind=None, normalizer=None)
        else:
            text, problem = _parse(parser, raw, kind=None, normalizer=None)
        if problem or text is None:
            if problem:
                self.report.gap(f"event_time_{problem}", eid, version.rec.l1_handle)
            return []
        marks = marks | self._marks_for_path(spec["binding"], spec["path"])
        event = {"event_id": event_id_of(eid, version.rec.l1_handle, version.object_id,
                                         spec["path"]),
                 "event_type": eid, "time_basis": ev["time_basis"], "time": text,
                 "window_start": None, "window_end": None,
                 "l1_handle": version.rec.l1_handle, "field_path": version.field(spec["path"]),
                 "digest": version.rec.digest, "lineage": version.lineage, "markings": marks,
                 "recorded_at": version.start, "superseded_at": version.end}
        return [(event, version.span)]

    def _capture_event(self, builder: StoreBuilder, eid: str, ev: Mapping[str, Any],
                       version: _Version, marks: frozenset[str]
                       ) -> list[tuple[dict[str, Any], tuple]]:
        before = self._db.execute(
            "SELECT vid FROM v WHERE object_id = ? AND start < ? ORDER BY start DESC LIMIT 1",
            (version.object_id, version.start)).fetchone()
        if before is None:
            return []
        previous = self._version(before[0])
        name = ev["observed_property"]
        old = self._stored_values(builder, previous, name).get(name)
        new = self._stored_values(builder, version, name).get(name)
        if not old or not new:
            return []
        if sorted(str(r["value"]) for r in old) == sorted(str(r["value"]) for r in new):
            return []
        start = instant(previous.rec.last_seen_at)
        if start > version.start:
            self.report.gap("event_window_inverted", eid, version.rec.l1_handle)
            return []
        cited = new[0]
        marks = marks | cited["markings"]
        event = {"event_id": event_id_of(eid, version.rec.l1_handle, version.object_id,
                                         name),
                 "event_type": eid, "time_basis": "capture_observed", "time": None,
                 "window_start": start, "window_end": version.start,
                 "l1_handle": version.rec.l1_handle, "field_path": cited["field_path"],
                 "digest": version.rec.digest, "lineage": version.lineage, "markings": marks,
                 "recorded_at": version.start, "superseded_at": None}
        return [(event, (version.start, None))]

    # -- build -------------------------------------------------------------------

    def _bound_relations(self) -> dict[str, set[str]]:
        """Source family -> the relations its bindings of served object types read."""
        relations: dict[str, set[str]] = {}
        for bid, (tid, b) in self.bindings.items():
            if self.types[tid].get("availability", {}).get("state") != "not_served":
                relations.setdefault(b["source"], set()).add(b["relation"])
        return relations

    def relation_completeness(self, described: Mapping[str, Mapping[str, Any]]
                              ) -> dict[str, dict[str, dict[str, Any]]]:
        """``store_meta.relation_completeness``: source family -> bound relation -> its
        own capture claim. ``completeness`` is ``complete`` only when the capture
        source records a completed full traversal of that relation
        (``full_traversal_at``, the time it finished), else ``partial``; never inferred
        from row counts. ``stale`` when the relation is served from a persisted capture
        or a held mirror copy (``described["persisted"]``). A family with no capture
        source (or one describing no relations) has no entry: its relations are
        unknown."""
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for source, bound in sorted(self._bound_relations().items()):
            state = described[source].get("relations") if source in described else None
            if state is None:
                continue
            persisted = described[source].get("persisted") or {}
            entries = {}
            for relation in sorted(bound):
                full = (state.get(relation) or {}).get("last_full_traversal_at")
                entries[relation] = {"completeness": "complete" if full else "partial",
                                     "full_traversal_at": full or None,
                                     "stale": relation in persisted}
            out[source] = entries
        return out

    def freshness(self, described: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``store_meta.freshness``: one contract ``$defs/freshness`` object per source
        family a binding names (operating/ontology_tool_contracts.json). ``captured_at``
        is the oldest last refresh among the bound relations (the answer is only as
        fresh as its stalest input); ``completeness`` is ``complete`` when every bound
        relation is (``relation_completeness``), ``partial`` when some are not and
        ``unknown`` for a family with no capture source. ``stale`` is decided by the
        reader's freshness bound, so the build writes false, unless a bound relation is
        served from a persisted capture while the live source is unavailable
        or held by a mirror whose refresh failed (``described["persisted"]``): then the
        entry is stale, ``served_from`` ``persisted_capture`` (or ``cached_mirror`` when
        every such relation is a held one), with the capture's ``label`` and those
        ``relations``."""
        relations = self._bound_relations()
        per_relation = self.relation_completeness(described)
        out = []
        for source in sorted(relations):
            entry: dict[str, Any] = {"source": source, "captured_at": None, "lag_seconds": None,
                                     "completeness": "unknown", "stale": False}
            state = described.get(source, {}).get("relations") if source in described else None
            if state is not None:
                times = [instant(state[rel]["last_refresh_at"]) for rel in sorted(
                    relations[source]) if rel in state and state[rel].get("last_refresh_at")]
                entry["captured_at"] = min(times) if times else None
                entry["completeness"] = ("complete" if all(
                    item["completeness"] == "complete"
                    for item in per_relation[source].values()) else "partial")
            copied = [item for relation, item in sorted(
                (described.get(source, {}).get("persisted") or {}).items())
                if relation in relations[source]]
            if copied:
                # A held relation whose refresh failed (``cached_mirror``) or one filled
                # from a persisted capture: stale either way, the farther from live named.
                served = ("persisted_capture" if any(
                    item.get("served_from", "persisted_capture") == "persisted_capture"
                    for item in copied) else "cached_mirror")
                entry.update(stale=True, served_from=served,
                             relations=copied[:MAX_FRESHNESS_RELATIONS])
                label = "; ".join(sorted({item["label"] for item in copied
                                          if item.get("label")}))
                if label:
                    entry["label"] = label[:599]
            out.append(entry)
        return out

    def _tick(self) -> None:
        """Between batches: the caller's ``checkpoint`` may raise to stop the build."""
        if self.checkpoint is not None:
            self.checkpoint()

    def project_into(self, builder: StoreBuilder) -> None:
        """Project every binding into ``builder``'s open generation, batch by batch.
        The working file is removed afterwards, whether or not the build succeeded."""
        def lineage() -> None:
            for vid in self._vids():
                self._lineage(self._version(vid))

        # Bindings declared ``value_storage: packed`` store their values as packed records.
        builder.packed_bindings = frozenset(
            bid for bid, (_, b) in self.bindings.items() if b.get("value_storage") == "packed")
        try:
            # Each phase names the unit of what it reports (``_Report.unit``); none
            # inherits the last unit of the phase before it.
            phases = (lambda: self._load(builder), self._project_links, lineage,
                      lambda: self._write_versions(builder),
                      lambda: self._project_values(builder),
                      lambda: self._conflicts(builder),
                      lambda: self._project_events(builder),
                      lambda: self._write_links(builder))
            for stage, phase in zip(STAGES, phases, strict=True):
                self.report.unit = None
                phase()
                if self.on_stage is not None:
                    self.on_stage(stage, _db_bytes(self._db, self._work_path),
                                  _db_bytes(builder.connection, builder.temp)
                                  + _file_bytes(Path(str(builder.temp) + "-journal")))
            self.report.unit = None
            conn = builder.connection
            self.report.counts.update({
                name: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for name, table in (("objects", "objects"), ("versions", "object_versions"),
                                    ("values", "object_values"), ("links", "links"),
                                    ("identifiers", "identifiers"), ("events", "events"))})
        finally:
            self.close()

    def project(self) -> dict[str, list[dict[str, Any]]]:
        """Project into a throwaway generation and return its rows per table (primary-key
        order). For small worlds and tests; ``build_store`` is the bounded path."""
        chosen = self.scratch_dir
        with tempfile.TemporaryDirectory() as tmp:
            self.scratch_dir = chosen or Path(tmp)
            builder = StoreBuilder(Path(tmp) / "projection.sqlite")
            try:
                self.project_into(builder)
                conn = builder.connection
                conn.row_factory = sqlite3.Row
                return {table: [dict(row) for row in conn.execute(
                            f"SELECT * FROM {table} ORDER BY {order}")]
                        for table, order in _TABLE_ORDER if table != "store_meta"}
            finally:
                builder.abort()
                self.scratch_dir = chosen

    def _write_versions(self, builder: StoreBuilder) -> None:
        last = 0
        while True:
            self._tick()
            rows = self._db.execute(
                """SELECT vid, type_id, object_id, handle, digest, lineage, start, end_,
                   binding FROM v WHERE vid > ? ORDER BY vid LIMIT ?""",
                (last, self.batch)).fetchall()
            if not rows:
                return
            builder.add("object_versions", [{
                "object_id": row[2], "l1_handle": row[3], "digest": row[4], "lineage": row[5],
                "markings": _markings_text(self._type_marks(row[1])), "recorded_at": row[6],
                "superseded_at": row[7]} for row in rows
                if self._emits(binding_unit(row[8]))])
            last = rows[-1][0]

    def _write_links(self, builder: StoreBuilder) -> None:
        last = 0
        while True:
            self._tick()
            chunk = self._db.execute(
                """SELECT l.seq, l.row, f.binding, t.binding FROM links l
                   JOIN v f ON f.vid = l.from_vid JOIN v t ON t.vid = l.to_vid
                   WHERE l.seq > ? ORDER BY l.seq LIMIT ?""", (last, self.batch)).fetchall()
            if not chunk:
                return
            rows = []
            for _, blob, from_binding, to_binding in chunk:
                row = _unpack(blob)
                if not self._emits(self._unit_of_link(row["link_type"], row["binding_kind"],
                                                      from_binding, to_binding)):
                    continue
                rows.append({**row, "reason": canonical(self._cited(row["reason"])),
                             "why_not": canonical(self._cited(row["why_not"])),
                             "evidence_handles": canonical(row["evidence_handles"]),
                             "markings": _markings_text(row["markings"])})
            builder.add("links", rows)
            last = chunk[-1][0]

    def _cited(self, features: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Link features in the tool contract's ``$defs/feature`` shape (``feature``,
        ``agrees``, ``citations`` of the compared cells) plus their descriptive fields."""
        out = []
        for feature in features:
            item = {k: v for k, v in feature.items() if not k.startswith("_")}
            if "_cite" in feature:
                item["citations"] = []
                for vid, path in feature["_cite"]:
                    handle, prefix, digest, lineage = self._cite(vid)
                    item["citations"].append({
                        "l1_handle": handle, "field_path": f"{prefix}.{path}",
                        "digest": digest, "lineage": lineage})
            out.append(item)
        return out


def _bc_company(version: _Version) -> str | None:
    """The capture company (``bc_<book>``) of a BC record, else None."""
    if version.binding.get("source") != "bc":
        return None
    lineage = version.rec.capture_lineage
    return lineage if lineage and _BC_LINEAGE.fullmatch(lineage) else None


def _predicate(row: Mapping[str, Any], predicate: Mapping[str, Any]) -> bool:
    """A declared row test; the source mirror's predicate semantics (text compare,
    numeric gte/lt, present = non-blank). A null or missing cell satisfies only
    ``absent``."""
    present, cell = _cell(row, predicate["path"])
    op = predicate["op"]
    filled = present and not _blank(cell)
    if op == "present":
        return filled
    if op == "absent":
        return not filled
    if not present or cell is None:
        return False
    value = predicate["value"]
    if op == "eq":
        return _text(cell) == _text(value)
    if op == "ne":
        return _text(cell) != _text(value)
    if op == "in":
        return _text(cell) in {_text(item) for item in value}
    if op == "prefix":
        text = _text(cell)
        return text is not None and text.startswith(str(value))
    left, right = _number(cell), _number(value)
    if left is None or right is None:
        if not isinstance(cell, str) or not isinstance(value, str):
            return False
        left, right = cell, value   # type: ignore[assignment]
    return left >= right if op == "gte" else left < right


def build_store(store_path: str | Path, schema: Any, sources: Mapping[str, CaptureSource], *,
                normalizer: Normalizer | None = None, as_known_at: Any = None,
                clock: Callable[[], datetime] | None = None,
                batch_size: int = DEFAULT_BATCH,
                post_project: Mapping[str, Callable[[StoreBuilder, "Projector"],
                                                    Mapping[str, Any]]] | None = None,
                checkpoint: Callable[[], None] | None = None,
                on_stage: Callable[[str, int, int], None] | None = None
                ) -> dict[str, Any]:
    """Project ``sources`` under ``schema`` into a new generation of ``store_path`` and
    swap it in atomically. Returns the build report (with ``content_digest``).

    ``on_stage``: ``Projector.on_stage`` (what each stage holds on disk; measurement only).

    ``checkpoint``: called between batches in every phase; it may raise to stop the build
    (the generation is then abandoned, never swapped in).

    ``post_project``: named steps run on the open generation after the projection and
    before the swap (``ontology.mentions.store_mention_links`` writes mention links
    there, so they are in the same generation); each returns a report fragment kept
    under ``build_report.post_project.<name>``. A step may also write the values of
    properties whose source parser is ``post_project`` (``POST_PROJECT``).

    Memory is bounded by ``batch_size`` and fixed caches, not by the capture size: rows
    go straight into the open generation and the projector's working file sits next
    to the store (same volume, never a memory-backed temp dir) and is removed after.
    ``ontology.project_incremental.build_generation`` is the incremental form."""
    projector = Projector(schema, sources, normalizer=normalizer, as_known_at=as_known_at,
                          batch_size=batch_size)
    projector.checkpoint = checkpoint
    projector.on_stage = on_stage
    sweep_stale_build_files(Path(store_path).parent, Path(store_path).name)
    with StoreBuilder(store_path) as builder:
        open_generation(builder)
        projector.scratch_dir = builder.temp.parent
        projector.project_into(builder)
        return finish_generation(builder, projector, projector.report.as_dict(),
                                 post_project=post_project, clock=clock)


def open_generation(builder: StoreBuilder) -> None:
    """Tune a generation's connection for a build: a page cache the size of the
    working file's (the generation's indexes take millions of inserts, which the
    2 MiB default turns into page churn)."""
    builder.connection.execute(f"PRAGMA cache_size = -{_SCRATCH_CACHE_KIB}")


def finish_generation(builder: StoreBuilder, projector: "Projector",
                      projected: Mapping[str, Any], *,
                      post_project: Mapping[str, Callable[[StoreBuilder, "Projector"],
                                                          Mapping[str, Any]]] | None = None,
                      clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Run the ``post_project`` steps on the open generation, write its ``store_meta``
    (build report from ``projected``: counts and gaps; the row counts are the
    generation's own; the handles of its ``RECORDED_GAPS`` go to
    ``store_meta.source_condition_unmet``) and commit it. Returns the report with
    ``content_digest``."""
    conn = builder.connection
    counts = dict(projected.get("counts", {}))
    counts.update({name: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                   for name, table in (("objects", "objects"), ("versions", "object_versions"),
                                       ("values", "object_values"), ("links", "links"),
                                       ("identifiers", "identifiers"), ("events", "events"))})
    steps = {name: dict(step(builder, projector))
             for name, step in sorted((post_project or {}).items())}
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    gaps, unmet = [], {}
    for gap in projected.get("gaps", []):
        if "handles" in gap:
            unmet[gap["where"]] = sorted(gap["handles"])
            gap = {key: value for key, value in gap.items() if key != "handles"}
        gaps.append(gap)
    # ``link_keys``: every unread compared key is a gap (``_report_unread_keys``); a
    # report without it never lets a reader rule out a link an unread key hid.
    report = {"projector": PROJECTOR, "schema_digest": projector.digest,
              "as_known_at": projector.cut, "counts": dict(sorted(counts.items())),
              "gaps": gaps, "link_keys": "reported"}
    if steps:
        report["post_project"] = steps
        counts = dict(report["counts"], links=conn.execute(
            "SELECT COUNT(*) FROM links").fetchone()[0])
        for step in steps.values():   # links.<tier> covers every written link
            for tier, added in (step.get("link_tiers") or {}).items():
                counts[f"links.{tier}"] = counts.get(f"links.{tier}", 0) + int(added)
        report["counts"] = dict(sorted(counts.items()))
    builder.set_meta("schema_digest", projector.digest)
    builder.set_meta("as_known_at", projector.cut or "")
    catalog = _get(projector.schema, "pinned_catalog_digest")
    if catalog:
        builder.set_meta("catalog_hash", catalog)
    described = {source: projector.sources[source].describe()
                 for source in sorted(projector.sources)}
    builder.set_meta("l1_generations", {source: {"generation": desc.get("generation")}
                                        for source, desc in described.items()})
    builder.set_meta("source_freshness", {source: desc.get("relations", {})
                                          for source, desc in described.items()})
    builder.set_meta("freshness", projector.freshness(described))
    builder.set_meta("relation_completeness", projector.relation_completeness(described))
    builder.set_meta("audience_keys", [row[0] for row in conn.execute(
        "SELECT DISTINCT audience_key FROM objects ORDER BY audience_key")])
    builder.set_meta("build_report", report)
    builder.set_meta("source_condition_unmet", dict(sorted(unmet.items())))
    digest = builder.commit(built_at=instant(now))
    return {**report, "content_digest": digest, "store": str(builder.target),
            "format": store_ddl.FORMAT}


__all__ = ["CAPTURED_RELATIONS", "CAPTURE_SOURCES", "CaptureSource", "CapturedRecord",
           "DEFAULT_BATCH", "ETL_REINSERTED", "MailCaptures", "MirrorCaptures", "PROJECTOR",
           "RowSample", "STAGES", "SharePointCaptures", "WORK_LAYOUT",
           "capture_source", "ProjectionError", "Projector", "binding_unit", "build_store",
           "event_id_of", "finish_generation", "link_ends", "link_unit", "merge_reports",
           "open_generation", "sweep_stale_build_files"]
