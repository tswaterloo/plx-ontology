"""Metamorphic variants of held-out oracle items over perturbed source reads (ENG-12).

A transform rewrites the oracle's source reads generically (it names no record and no
case). It wraps the read ports the oracle already uses (``sources``), so the same transform
runs over a fixture world and over the live reads of a held-out run
(``scripts/eval_oracle_dev.py heldout``), and the perturbed reads serve the run's own
cached reads (no source is read again). The oracle then answers the parent item's own root
again from the perturbed reads, and that answer is the variant's ``known_outcome``: an
expected answer is never patched by hand. What each transform does to the reads, and so to
the oracle's answer (evals/tests/test_perturb.py):

* ``shifted_dates``: every date and timestamp moves by ``days``, in any column, inside a
  JSON column (``custom_fields``, as an object or as JSON text) and the replica time, so
  dates in the answer move with it and nothing turns stale;
* ``scaled_quantity``: every quantity is multiplied by ``factor`` (Decimal, exact): the
  columns whose field type is a quantity (``keys.Relation.quantities``,
  ``keys.BC_LINE_QUANTITIES``). Quantity totals scale, a three-way verdict does not change,
  money (unit prices) is untouched;
* ``ambiguous_party``: every supplier and customer gets a twin that shares its name and
  every other value but has its own id and its own code (party codes are unique,
  app_supplier.code and app_customer.code @unique, P@b08ffc3 prisma/schema.prisma; the
  twin's code stays its own as BC holds it, trimmed, cut to ``keys.BC_PARTY_NUMBER_MAX``
  and compared ignoring case), so a party name no longer names one party. The oracle
  identifies a party by key and code, never by name: every answer is the parent's;
* ``repeated_event``: every keyed event row (receipts, shipments, their lines, FM status
  entries and raw receipts) and every keyed BC document and line is delivered twice; a
  keyed record delivered twice is one record (``source.distinct``), so nothing is counted
  twice and the answer is the parent's. A row without its key is never repeated: two equal
  keyless rows may be two real events;
* ``conflicting_units``: receipt and shipment lines name their unit differently. ``alias``
  spells the same unit another way (it normalises, so a match still holds); ``conflict``
  names another unit, which the oracle never converts: the leg is not compared, a unit gap;
* ``partial_source_failure``: the first source of the parent's answer chain other than its
  root relation fails to read; the answer is partial with an ``inaccessible`` gap, never an
  absence.

Variants are held-out only (item.schema.json: a perturbation variant has a ``ho-`` id, split
``held_out``, ``parent_id`` and ``perturbation``); the frozen dev set never gets one. The
item records the transform and its parameters, so a harness rebuilds the same reads
(``Perturbed``). Evidence label: that of the reads perturbed (``injected-transport`` over a
fixture world, ``live-read`` over a live run's reads), with the perturbation recorded.
The product is scored on a variant over the same port: ``scripts/eval_dev_baseline.py
heldout`` replays its own Portal mirror through ``portal_port`` (and its clock through
``replica_time``), so a variant's product and its label read one perturbed source.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Callable, Mapping, Sequence

from . import keys as K
from .families import Sources
from .generator import AUDIT_NOTE, Candidate, _answer, _item, oracle_code_digest
from .source import (BcReads, PortalReads, bc_keyed, canonical, decimal_of, key_text,
                     portal_keyed)

TRANSFORMS = ("shifted_dates", "scaled_quantity", "ambiguous_party", "repeated_event",
              "conflicting_units", "partial_source_failure")
DEFAULTS: dict[str, dict[str, Any]] = {
    "shifted_dates": {"days": 30}, "scaled_quantity": {"factor": "2"}, "ambiguous_party": {},
    "repeated_event": {}, "conflicting_units": {"mode": "conflict"},
    "partial_source_failure": {}}
# Item id suffix of each transform (the parent's own suffix follows it).
TAGS = {"shifted_dates": "dt", "scaled_quantity": "qty", "ambiguous_party": "party",
        "repeated_event": "rep", "conflicting_units": "unit", "partial_source_failure": "fail"}
DATE8_COLUMNS = ("audtdate",)  # Sage yyyymmdd dates (source.instant)
PARTY_RELATIONS = (K.SUPPLIER, K.CUSTOMER)
EVENT_RELATIONS = (K.RECEIPT, K.RECEIPT_LINE, K.SHIPMENT, K.SHIPMENT_LINE, K.FM_AO_STATUS,
                   K.FM_RECEIPT_RAW)
UNIT_LINES = (K.RECEIPT_LINE, K.SHIPMENT_LINE)
UNIT_MODES = ("alias", "conflict")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")
_DATE8 = re.compile(r"\d{8}")
_PARENT_ID = re.compile(r"ho-(F[1-9][0-9]?)-([0-9]{4})(?:-([a-z0-9]{1,16}))?")
_BC_SETS = {chain: entity_set for entity_set, (_, chain) in K.BC_OBJECTS.items()}


class InjectedFailure(RuntimeError):
    """The read ``partial_source_failure`` fails (its class names the gap reason)."""


def _params(transform: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """The transform's parameters over its defaults; an unknown transform or parameter, or
    a value out of range, fails closed."""
    if transform not in TRANSFORMS:
        raise ValueError(f"unknown transform {transform!r}")
    out = {**DEFAULTS[transform], **(params or {})}
    if set(out) - set(DEFAULTS[transform]) - ({"source"} if transform ==
                                              "partial_source_failure" else set()):
        raise ValueError(f"unknown parameter for {transform}")
    if transform == "shifted_dates":
        days = out["days"]
        if not isinstance(days, int) or isinstance(days, bool) or days == 0:
            raise ValueError("days must be a non-zero integer")
    elif transform == "scaled_quantity":
        try:
            factor = Decimal(str(out["factor"]))
        except InvalidOperation:
            raise ValueError("factor must be a decimal") from None
        if not factor.is_finite() or factor <= 0:
            raise ValueError("factor must be a positive finite decimal")
    elif transform == "conflicting_units" and out["mode"] not in UNIT_MODES:
        raise ValueError("mode must be alias or conflict")
    elif transform == "partial_source_failure":
        source = out.get("source")
        if not isinstance(source, str) or not (source in K.RELATIONS or source in K.BC_OBJECTS):
            raise ValueError("source must be an oracle relation or BC entity set")
    return out


def _shifted(value: Any, days: int, date8: bool = False) -> Any:
    """``value`` with every date in it moved by ``days``: ISO dates and timestamps (whole
    days: the time of day and the zone stay as written), Sage yyyymmdd in a ``date8``
    column, and dates nested in an object, a list or the JSON text of a JSON column."""
    if isinstance(value, Mapping):
        return {k: _shifted(v, days) for k, v in value.items()}
    if isinstance(value, list):
        return [_shifted(v, days) for v in value]
    if not isinstance(value, str):
        return value
    if date8 and _DATE8.fullmatch(value):
        moved = date(int(value[:4]), int(value[4:6]), int(value[6:])) + timedelta(days=days)
        return moved.strftime("%Y%m%d")
    if _ISO_DATE.fullmatch(value):
        return (date.fromisoformat(value[:10]) + timedelta(days=days)).isoformat() + value[10:]
    if value[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        moved = _shifted(parsed, days)
        return value if moved == parsed else json.dumps(moved, ensure_ascii=False)
    return value


def _scaled(value: Any, factor: Decimal) -> Any:
    amount = decimal_of(value)
    if amount is None:
        return value
    scaled = amount * factor
    if isinstance(value, str):
        return format(scaled, "f")
    if isinstance(value, int) and scaled == scaled.to_integral_value():
        return int(scaled)
    return scaled


def _other_unit(unit: Any, mode: str) -> Any:
    """The same unit spelled another way (``alias``) or a unit with another BC code."""
    if not isinstance(unit, str) or not unit.strip():
        return unit
    token = unit.strip().lower()
    canon = K.UOM_ALIASES.get(token, token)
    if mode == "alias":
        spellings = sorted(a for a, c in K.UOM_ALIASES.items() if c == canon and a != token)
        return spellings[0] if spellings else (canon.upper() if canon.upper() != unit
                                               else canon)
    # One-to-one, so two units never collapse into one: a known unit becomes the next
    # known unit (another BC code), an unknown one a token no table knows.
    if canon not in K.UOM_BC_CODE:
        return canon + "-other"
    known = sorted(K.UOM_BC_CODE)
    return known[(known.index(canon) + 1) % len(known)]


def _twin_key(value: Any, taken: set[Any], view: Callable[[Any], Any] = lambda v: v,
              width: int | None = None) -> Any:
    """A value no row of the relation has (compared as ``view`` sees it): the next integer,
    or the text with a suffix. A text longer than ``width`` with its suffix is cut first so
    the suffix survives a cut to ``width``."""
    if isinstance(value, int) and not isinstance(value, bool):
        twin: Any = max((v for v in taken if isinstance(v, int)
                         and not isinstance(v, bool)), default=0) + 1
    else:
        n = 0
        while True:
            n += 1
            suffix = "-twin" if n == 1 else f"-twin{n}"
            twin = f"{value}{suffix}"
            if width is not None and len(twin) > width:
                twin = str(value).strip()[:width - len(suffix)] + suffix
            if view(twin) not in taken:
                break
    taken.add(view(twin))
    return twin


def _bc_party_number(code: Any) -> Any:
    """A party code as BC holds it: trimmed, cut to its Code width, compared ignoring case
    (``keys.vendor_bc_number``); a code BC has no number for stays itself."""
    number = K.vendor_bc_number(code)
    return number.casefold() if number is not None else code


def _twins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each party row and its twin: the same name and values, its own id and code. The
    twin's code stays its own after BC trims, cuts and case-folds it (a long code's twin
    would otherwise share the parent's BC number and make the parent's identity doubtful)."""
    ids = {row.get("id") for row in rows}
    codes = {_bc_party_number(row.get("code")) for row in rows}
    out = []
    for row in rows:
        twin = {**row, "id": _twin_key(row.get("id"), ids)}
        if key_text(row.get("code")) is not None:
            twin["code"] = _twin_key(row["code"], codes, _bc_party_number,
                                     K.BC_PARTY_NUMBER_MAX)
        out.append(twin)
    return rows + out


def _rows(transform: str, params: Mapping[str, Any], relation: str,
          rows: list[dict[str, Any]], key: Any) -> list[dict[str, Any]]:
    """The rows of one Portal read as ``transform`` changes them (new dicts where a value
    changes; the read's own rows are never changed)."""
    if transform == "shifted_dates":
        return [{c: _shifted(v, params["days"], c in DATE8_COLUMNS) for c, v in row.items()}
                for row in rows]
    if transform == "scaled_quantity":
        columns = K.RELATIONS[relation].quantities if relation in K.RELATIONS else ()
        factor = Decimal(str(params["factor"]))
        return [{**row, **{c: _scaled(row[c], factor) for c in columns if c in row}}
                for row in rows] if columns else rows
    if transform == "ambiguous_party" and relation in PARTY_RELATIONS:
        return _twins(rows)
    if transform == "repeated_event" and relation in EVENT_RELATIONS:
        keyed = portal_keyed(key)
        return rows + [dict(row) for row in rows if keyed(row)]
    if transform == "conflicting_units" and relation in UNIT_LINES:
        return [{**row, "uom": _other_unit(row.get("uom"), params["mode"])} for row in rows]
    return rows


def portal_port(read_relation: Callable[[str, Sequence[str]], Any], transform: str,
                params: Mapping[str, Any] | None = None
                ) -> Callable[[str, Sequence[str]], Any]:
    """A Portal port (``source.PortalReads``) serving ``read_relation``'s reads perturbed by
    ``transform``. An unknown transform or parameter fails closed."""
    params = _params(transform, params)

    def read(name: str, columns: Sequence[str]) -> Any:
        if transform == "partial_source_failure" and name == params["source"]:
            raise InjectedFailure(name)
        result = read_relation(name, columns)
        if not isinstance(result, Mapping) or not isinstance(result.get("rows"), list):
            return result  # PortalReads refuses the shape itself
        out = {**result, "rows": _rows(transform, params, name, result["rows"],
                                       result.get("key"))}
        health = result.get("health_before")
        if isinstance(health, Mapping):
            out["health_before"] = {**health, "generated_at": replica_time(
                health.get("generated_at"), transform, params)}
        return out
    return read


def touched_relations(transform: str, params: Mapping[str, Any] | None,
                      relations: Sequence[str]) -> list[str]:
    """The relations of ``relations`` whose reads ``portal_port`` changes (or fails) under
    ``transform``, in order: every relation under ``shifted_dates`` (a date may sit in any
    column, and the replica time moves), the quantity relations under ``scaled_quantity``,
    the parties, events or unit lines, and the failed source. A Portal read of any other
    relation is served as read, so a harness serving the perturbed source rebuilds only
    these (``scripts/eval_dev_baseline.py heldout``). An unknown transform or parameter
    fails closed."""
    params = _params(transform, params)
    if transform == "shifted_dates":
        return list(relations)
    if transform == "scaled_quantity":
        return [r for r in relations if r in K.RELATIONS and K.RELATIONS[r].quantities]
    touched = {"ambiguous_party": PARTY_RELATIONS, "repeated_event": EVENT_RELATIONS,
               "conflicting_units": UNIT_LINES}.get(transform) or (params["source"],)
    return [r for r in relations if r in touched]


def replica_time(text: Any, transform: str, params: Mapping[str, Any] | None = None) -> Any:
    """A replica time (or any instant) as the perturbed source reports it: moved by
    ``days`` under ``shifted_dates``, else as read. A harness that serves the product the
    perturbed source runs its clock by it too (``scripts/eval_dev_baseline.py heldout``), so
    a capture is never earlier than the perturbed item's time."""
    params = _params(transform, params)
    return _shifted(text, params["days"]) if transform == "shifted_dates" else text


class _BcPort:
    """A BC port (``source.BcPort``) serving another one's records perturbed."""

    def __init__(self, port: Any, transform: str, params: Mapping[str, Any]) -> None:
        self._port = port
        self._transform = transform
        self._params = params

    def _fail(self, entity_set: str) -> None:
        if self._transform == "partial_source_failure" and entity_set == self._params["source"]:
            raise InjectedFailure(entity_set)

    def _records(self, found: Mapping[str, Any], lines: bool) -> dict[str, Any]:
        records = [r for r in found.get("records") or [] if isinstance(r, Mapping)
                   and isinstance(r.get("fields"), Mapping)]
        if self._transform == "shifted_dates":
            records = [_shifted(r, self._params["days"]) for r in records]
        elif self._transform == "scaled_quantity" and lines:
            factor = Decimal(str(self._params["factor"]))
            records = [{**r, "fields": {**r["fields"], **{
                f: _scaled(r["fields"][f], factor) for f in K.BC_LINE_QUANTITIES
                if f in r["fields"]}}} for r in records]
        elif self._transform == "repeated_event":
            records += [{**r, "fields": dict(r["fields"])} for r in records if bc_keyed(r)]
        return {**found, "records": records}

    def find(self, entity_set: str, field: str, value: str) -> Mapping[str, Any]:
        self._fail(entity_set)
        return self._records(self._port.find(entity_set, field, value), lines=False)

    def lines(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._records(self._port.lines(record), lines=True)

    def sample(self, entity_set: str, limit: int) -> Mapping[str, Any]:
        self._fail(entity_set)
        return self._records(self._port.sample(entity_set, limit), lines=False)


def bc_port(port: Any, transform: str, params: Mapping[str, Any] | None = None) -> Any:
    """A BC port serving ``port``'s records perturbed by ``transform``."""
    return _BcPort(port, transform, _params(transform, params))


class _CachedBc:
    """A BC port over a run's ``BcReads``: the perturbed reads use its cached finds and
    lines, and its failures (with their reason and cause)."""

    def __init__(self, reads: BcReads) -> None:
        self._reads = reads

    def find(self, entity_set: str, field: str, value: str) -> Mapping[str, Any]:
        records, complete = self._reads.find(entity_set, field, value)
        return {"records": records, "complete": complete}

    def lines(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        records, complete = self._reads.lines(record)
        return {"records": records, "complete": complete}

    def sample(self, entity_set: str, limit: int) -> Mapping[str, Any]:
        records = self._reads.sample(entity_set, limit)
        state = self._reads.sample_state(entity_set) or {}
        return {"records": records, "complete": state.get("complete") is True,
                "capped": bool(state.get("capped")), "withheld": state.get("withheld", 0),
                "source": state.get("tried", []),
                **({"companies": state["companies"]} if "companies" in state else {})}


def sources(base: Sources, transform: str, params: Mapping[str, Any] | None = None) -> Sources:
    """``base``'s reads perturbed by ``transform`` (``base`` is never changed), read in full
    once so one answer cites one generation. An unknown transform or parameter fails
    closed."""
    out = Sources(PortalReads(portal_port(base.portal.read, transform, params)),
                  BcReads(bc_port(_CachedBc(base.bc), transform, params))
                  if base.bc is not None else None)
    out.portal.prefetch()  # the replica time is the answer's time
    return out


def perturbation_key(item: Mapping[str, Any]) -> str:
    """Items with one key read the same perturbed sources (a standard item: ``base``)."""
    return canonical(item.get("perturbation"))


class Perturbed:
    """The reads each item is answered from: ``base`` for a standard item, ``base``
    perturbed as a variant's ``perturbation`` records. One perturbation is held at a time,
    so a caller answers items grouped by ``perturbation_key`` (each perturbed read is then
    built once, and memory stays at one perturbed copy)."""

    def __init__(self, base: Sources) -> None:
        self.base = base
        self._key: str | None = None
        self._sources: Sources | None = None

    def of(self, item: Mapping[str, Any]) -> Sources:
        perturbation = item.get("perturbation")
        if perturbation is None:
            return self.base
        key = perturbation_key(item)
        if key != self._key or self._sources is None:
            self._sources = sources(self.base, perturbation["transform"],
                                    perturbation["params"])
            self._key = key
        return self._sources


def failing_source(parent: Mapping[str, Any]) -> str | None:
    """The first source of the parent's answer chain other than its root relation (a
    Portal relation, or a BC entity set); None when the chain reaches no other source."""
    root = (parent.get("root") or {}).get("relation")
    for entry in (parent.get("known_outcome") or {}).get("chain") or []:
        relation = entry.get("relation")
        if relation != root and (relation in K.RELATIONS or relation in _BC_SETS):
            return _BC_SETS.get(relation, relation)
    return None


def _parent_note(parent: Mapping[str, Any]) -> str | None:
    note = (parent.get("label") or {}).get("note") or ""
    return note.replace(AUDIT_NOTE, "").strip() or None


def variants(base: Sources, parents: Sequence[Mapping[str, Any]], *, now: str,
             transforms: Sequence[str] = TRANSFORMS, rotate: bool = False
             ) -> list[dict[str, Any]]:
    """One variant per standard held-out parent and transform (``partial_source_failure``
    only where the parent's chain reaches a source besides its root), each answered by the
    oracle over ``base``'s reads perturbed (``Perturbed``), in groups of one perturbation.
    ``rotate``: one variant per parent instead, the transforms in turn (parent n takes the
    n-th, or the next one that applies to it), so a set grows by its own size and every
    transform covers about an equal share of its parents. A dev or non-standard parent, or
    an unknown transform, fails closed."""
    unknown = [t for t in transforms if t not in TRANSFORMS]
    if unknown:
        raise ValueError(f"unknown transforms {unknown}")
    numbers: dict[str, tuple[int, str]] = {}
    for parent in parents:
        match = _PARENT_ID.fullmatch(str(parent.get("id")))
        if (match is None or parent.get("split") != "held_out"
                or parent.get("variant") != "standard"):
            raise ValueError("variants are made from standard held-out items only")
        numbers[parent["id"]] = (int(match[2]), match[3] or "")
    code = oracle_code_digest()
    planned: list[tuple[Mapping[str, Any], str, dict[str, Any]]] = []
    for n, parent in enumerate(parents):
        turn = [transforms[(n + k) % len(transforms)] for k in range(len(transforms))] \
            if rotate else transforms
        for transform in turn:
            params = dict(DEFAULTS[transform])
            if transform == "partial_source_failure":
                source = failing_source(parent)
                if source is None:
                    continue
                params["source"] = source
            planned.append((parent, transform, params))
            if rotate:
                break
    planned.sort(key=lambda p: canonical([TRANSFORMS.index(p[1]), p[2]]))  # stable groups
    perturbed = Perturbed(base)
    out: list[dict[str, Any]] = []
    for parent, transform, params in planned:
        number, parent_suffix = numbers[parent["id"]]
        root = parent["root"]
        candidate = Candidate(stratum="", family=parent["family"],
                              identifier=root["identifier"], kind=root["identifier_kind"],
                              lineage=parent["lineage_stratum"],
                              root_relation=root["relation"],
                              root_key=tuple(sorted(root["key"].items())),
                              object_type=root["object_type"])
        perturbation = {"transform": transform, "params": params}
        reads = perturbed.of({"perturbation": perturbation})
        suffix = (TAGS[transform] + parent_suffix)[:16]
        item = _item(reads, candidate, number, now, code, split="held_out",
                     id_suffix=suffix, evidence_note=_parent_note(parent),
                     answered=_answer(reads, candidate, now))
        item["variant"] = transform
        item["parent_id"] = parent["id"]
        item["perturbation"] = perturbation
        out.append(item)
    return out


__all__ = ["DEFAULTS", "TRANSFORMS", "InjectedFailure", "Perturbed", "bc_port",
           "failing_source", "perturbation_key", "portal_port", "replica_time", "sources",
           "touched_relations", "variants"]
