"""Capture-store kinds of the order-intake and meeting connectors (Zendesk order@, Shopify,
EDI X12, Granola), and the one write path from a connector sweep into the versioned capture
store (``integrations.capture_store``).

A sweep's records are saved here before its cursor moves (``vendor_sweep.run_sweep``
``persist``): a failed save leaves the cursor where it was, so the next sweep reads the same
records again; an unchanged record only refreshes ``last_seen`` (the store's digest), so a
re-read never duplicates a version.

Kinds. Every record path is ``(<unit key>, <record key>)``: the record key is the record's
own citation after ``<scheme>://`` (``ticket/42/comment/7``, ``order/450789469/line/1``).

* ``zendesk_order`` (``zendesk``): unit ``zendesk:<mailbox>`` (the ticket's original
  recipient, one of ``zendesk_scope.json`` ``order_recipients``); resource
  ``mail:<mailbox>``: whoever may read the order mailbox reads its queue, nobody else.
* ``granola_note`` (``granola``): unit ``granola:<owner address>``; resource
  ``mail:<owner address>``, as a meeting transcript is its organizer's. A note is saved only
  after ``meeting_transcripts.admit`` clears every person in it against the people
  exclusion; with no exclusion guard nothing is saved (``people_exclusion_unavailable``).
* ``shopify_record`` (``shopify``): unit ``shopify:<shop label>``; resource the same.
* ``edi_record`` (``edi``): unit ``edi:<drop>`` (one per configured drop folder or SFTP
  inbox, ``drop`` today); resource the same.

Default deny: a reader sees a record only when the live audience grants its resource
(``LiveSourceAudience``; the ``shopify`` and ``edi`` families exist for these kinds), so
switching a connector on grants nobody anything until the audience file names them.

Runs finish ``partial`` (reason ``incremental``): an incremental read is never a full
traversal, so nothing is ever marked missing from the source by these captures.

Lineage: the C0 lineage enum (``ontology/store_ddl.py`` LINEAGES) has no lineage for these
systems yet; Zendesk tickets and Granola notes use ``mail`` (as Teams and meeting transcripts
do), Shopify and EDI ``original``, until the lineage decision lands.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Callable, Mapping, Sequence

from .capture_store import (CaptureError, CaptureStore, CapturedRecord, SourceKind,
                            register_source_kind)
from .live_source_guard import jsonable

_ADDRESS = re.compile(r"[a-z0-9._%+'-]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,24}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_EDI_ID = re.compile(r"[a-z0-9][a-z0-9 ._-]{0,34}")
_KEY = re.compile(r"[^\x00-\x1f\x7f]{1,500}")
RUN_REASON = "incremental"


def _two(parts: Sequence[str]) -> Sequence[str]:
    if isinstance(parts, (str, bytes)) or len(parts) != 2 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("vendor_path_invalid")
    return parts


def _canon(unit_pattern: re.Pattern[str]) -> Callable[[Sequence[str]], tuple[str, ...]]:
    def canonical(parts: Sequence[str]) -> tuple[str, ...]:
        unit, key = _two(parts)
        unit = unit.strip().lower()
        if unit_pattern.fullmatch(unit) is None or _KEY.fullmatch(key) is None \
                or key != key.strip():
            raise ValueError("vendor_path_invalid")
        return unit, key
    return canonical


def _kind(name: str, scheme: str, family: str, resource_family: str,
          unit_pattern: re.Pattern[str]) -> SourceKind:
    return register_source_kind(SourceKind(
        name=name, scheme=scheme, path_parts=2, canonical_path=_canon(unit_pattern),
        unit=lambda p: f"{family}:{p[0]}", resources=lambda p: (f"{resource_family}:{p[0]}",),
        key=lambda p: {"unit": p[0], "record": p[1]}))


ZENDESK_KIND = _kind("zendesk_order", "zendesk", "zendesk", "mail", _ADDRESS)
GRANOLA_KIND = _kind("granola_note", "granola", "granola", "mail", _ADDRESS)
SHOPIFY_KIND = _kind("shopify_record", "shopify", "shopify", "shopify", _LABEL)
EDI_KIND = _kind("edi_record", "edi", "edi", "edi", _EDI_ID)
KINDS = (ZENDESK_KIND, GRANOLA_KIND, SHOPIFY_KIND, EDI_KIND)
LINEAGE = {"zendesk_order": "mail", "granola_note": "mail", "shopify_record": "original",
           "edi_record": "original"}


def _record_key(scheme: str, record: Mapping[str, Any]) -> str | None:
    citation = record.get("citation")
    prefix = scheme + "://"
    if not isinstance(citation, str) or not citation.startswith(prefix):
        return None
    return citation[len(prefix):] or None


def _version(row: Mapping[str, Any]) -> str:
    text = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(text.encode("utf-8")).hexdigest()[:32]


def save(store: CaptureStore, kind: SourceKind, records: Sequence[Mapping[str, Any]],
         unit_of: Callable[[Mapping[str, Any]], str | None], *, binding: str,
         source_api: str, admit: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
         ) -> dict[str, Any]:
    """Save one sweep's records (grouped by unit, one run per unit) and return the counts.

    A record with no unit, an invalid path, or one ``admit`` refuses is not saved and is
    counted in ``withheld`` by reason, and in its unit run's ``withheld`` when its unit is
    known, so that unit never reads as complete. Two records of one sweep at one path with
    different content keep the later one and are counted in ``duplicate_paths`` (never
    silent). A store error propagates: the caller's cursor must not move."""
    store.bind_source(kind.name, binding)
    groups: dict[str, list[tuple[tuple[str, ...], dict[str, Any]]]] = {}
    held: dict[str, int] = {}
    withheld: dict[str, int] = {}

    def refuse(reason: str, unit: str | None) -> None:
        withheld[reason] = withheld.get(reason, 0) + 1
        if unit is not None:
            held[unit] = held.get(unit, 0) + 1

    for record in records:
        unit_key = unit_of(record)
        key = _record_key(kind.scheme, record)
        try:
            path = kind.canonical_path((unit_key or "", key or ""))
        except ValueError:
            refuse("path_invalid" if unit_key else "no_unit", None)
            continue
        unit = kind.unit(path)
        row = jsonable(dict(record))
        if admit is not None:
            try:
                row = jsonable(dict(admit(row)))
            except Exception as exc:  # noqa: BLE001 - a refusal of any kind withholds
                refuse(str(getattr(exc, "code", None) or exc) or type(exc).__name__, unit)
                continue
        groups.setdefault(unit, []).append((path, row))
    counts: dict[str, Any] = {"saved": 0, "new_versions": 0, "unchanged": 0,
                              "duplicate_paths": 0, "units": 0,
                              "withheld": dict(sorted(withheld.items()))}
    for unit in sorted(set(groups) | set(held)):
        rows = groups.get(unit, [])
        first = rows[0][0] if rows else None
        if first is None:   # every record of this unit refused: nothing to name the run by
            continue
        latest: dict[tuple[str, ...], dict[str, Any]] = {}
        for path, row in rows:
            if path in latest and latest[path] != row:
                counts["duplicate_paths"] += 1
            latest[path] = row
        run = store.begin_run(kind.name, unit, kind.resources(first))
        page = [CapturedRecord(path, {"row": row, "meta": {"source_api": source_api}},
                               _version(row), LINEAGE[kind.name])
                for path, row in latest.items()]
        saved = store.save_page(run["run_id"], page, None, withheld=held.get(unit, 0))
        store.finish_run(run["run_id"], "partial", reason=RUN_REASON)
        counts["saved"] += saved["records"]
        counts["new_versions"] += saved["new_versions"]
        counts["unchanged"] += saved["unchanged"]
        counts["units"] += 1
    return counts


def persist_into(store: CaptureStore | None, kind: SourceKind,
                 unit_of: Callable[[Mapping[str, Any]], str | None], *, binding: str,
                 source_api: str,
                 admit: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
                 ) -> Callable[[Sequence[Mapping[str, Any]]], dict[str, Any]] | None:
    """The ``run_sweep`` ``persist`` step for ``store`` (None: records are only returned)."""
    if store is None:
        return None

    def persist(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return save(store, kind, records, unit_of, binding=binding, source_api=source_api,
                    admit=admit)
    return persist


__all__ = ["CaptureError", "EDI_KIND", "GRANOLA_KIND", "KINDS", "SHOPIFY_KIND",
           "ZENDESK_KIND", "persist_into", "save"]
