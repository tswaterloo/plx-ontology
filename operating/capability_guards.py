"""Capability-keyed proposal guards: data-declared, Workbench-checked, generic code.

The typed action catalog (``operating/action_catalog.json``, ``plx-action-catalog/v1``)
is the one source: per owner-system proposal type, its required parameters (the
payload a proposal must carry), the relations its ``expected_effect`` row may belong
to (``target_relations``), the hosts it is offered on (``hosts``: ``case_host``
and/or ``test``) and, among its preconditions, its guards. Each guard is one of a few
generic checks over the proposal and the rows it cites, re-read for the reader:

* ``effect_fields_within``: the expected effect names a non-empty list of fields, all
  in the guard's ``fields`` (the capability's writable fields);
* ``effect_targets_cited_row``: the expected effect names a cited, current row of
  one of the entry's ``target_relations`` (an effect on any other relation is refused
  for every capability, before its guards);
* ``effect_values_match_payload``: every field the effect writes is in the payload
  with the same value (``numeric`` fields compared as numbers, non-numbers refused);
  a field any guard of the capability lists as ``numeric`` is read by ``number_state``
  everywhere (a JSON float such as ``1509.0`` is the number 1509), every other field by
  ``value_state`` (a float is unreadable there: identifiers never parse floats);
* ``payload_fields_in_cited_evidence``: the payload names, per written field, a cited
  row and field (``{field: {"citation", "field"}}`` under the guard's ``evidence`` key)
  holding the written value; never the target row itself, and when the guard lists
  ``sources`` for the field, one of them (relation and field, its ``when`` cells, and
  its ``join`` to the target row);
* ``target_fields_empty``: on the effect's row, every one of ``fields`` was read and
  holds nothing (an unreadable field refuses);
* ``effect_equals_payload_value``: the expected effect sets exactly the payload value
  (``value``) on a cited row of ``target_relation``;
* ``payload_value_matches_shape``: the value matches a declared ``shapes`` pattern;
* ``payload_value_in_cited_field``: the cited row named by the payload's ``citation``
  key holds exactly the value (trimmed text) in the payload's ``field`` key (a column,
  ``column.jsonKey`` or a document row's ``fields.<name>.value``), and that citation is
  one of the proposal's;
* ``payload_value_not_in_cited_fields``: none of ``fields`` of that cited row equals the
  value (a row's own number is no evidence of another identifier);
* ``target_fields_hold_no_shape``: on the effect's row, none of ``fields`` holds a value
  matching ``shape`` (values equal to one of ``ignore_equal_to`` do not count);
* ``payload_value_not_in_target_fields``: the value equals none of ``fields`` of the
  effect's row.

``check(capability, payload, expected_effect, citations, read)`` returns the verdict;
``read(handle)`` gives ``{"relation", "key", "row"}`` of a cited row as it is now, or
None when it cannot be read as current content (then the guard fails). Nothing here
names a kind of record: the capability entries are data. Used by ``plx.case_propose``
and ``plx.act`` mode propose (``operating/mcp_server.py``, ``operating/action_tools.py``;
refused before anything is recorded) and by the playbook evaluator (``operating/playbook_eval.py``, re-checked on
the cited rows as re-read).
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from decimal import Decimal
import re
from typing import Any, Callable, Mapping, Sequence

from .cases import effect_values

# One source: the owner-system proposal types of the typed action catalog
# (operating/action_catalog.json; decision action-catalog-format option b). Their
# preconditions whose check is one of CHECKS are the capability guards.
GUARDS_PATH = Path(__file__).with_name("action_catalog.json")
CHECKS = frozenset({"effect_equals_payload_value", "effect_fields_within",
                    "effect_targets_cited_row", "target_fields_empty",
                    "effect_values_match_payload", "payload_fields_in_cited_evidence",
                    "payload_value_matches_shape",
                    "payload_value_in_cited_field", "payload_value_not_in_cited_fields",
                    "target_fields_hold_no_shape", "payload_value_not_in_target_fields"})
Read = Callable[[str], "Mapping[str, Any] | None"]
# The one spelling of a capability name: dot-separated lower-case words, nothing else
# (no surrounding space, no case variants). Lookup, storage (operating/cases.py),
# the evaluator and action registration all use ``canonical``.
_CANONICAL = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){0,5}$")


def canonical(capability: Any) -> str | None:
    """The capability name when it is already canonical, else None (never repaired:
    ``'x.y '`` or ``'X.Y'`` is refused, not trimmed or lower-cased)."""
    return capability if isinstance(capability, str) and _CANONICAL.fullmatch(capability) \
        else None


class GuardError(ValueError):
    """The catalog's guard data is malformed."""


@lru_cache(maxsize=1)
def registry(path: str = str(GUARDS_PATH)) -> dict[str, Any]:
    """The guard view of the action catalog: per owner-system proposal type its
    ``target_relations``, ``hosts``, ``payload_required`` (its required parameters)
    and ``guards`` (its preconditions with a guard check); shapes compiled. The catalog
    loader (operating.action_catalog) validates the file first."""
    from .action_catalog import ActionCatalog, CatalogError, GUARD_CHECKS, PERSON_CHECK
    if GUARD_CHECKS != CHECKS:
        raise GuardError("capability_guard_checks_differ_from_the_catalog")
    try:
        catalog = ActionCatalog.from_file(path)
    except (CatalogError, OSError, ValueError) as exc:
        raise GuardError(f"capability_guards_catalog_invalid:{exc}") from None
    capabilities = {}
    for name in catalog.proposal_ids():
        entry = catalog.get(name)
        capabilities[name] = {
            "target_relations": entry["target_relations"], "hosts": entry["hosts"],
            "payload_required": [param for param, spec in entry["parameters"].items()
                                 if spec["required"]],
            "guards": [item for item in entry["preconditions"] if item["check"] in CHECKS],
            "person_confirms": [_person_text(item) for item in entry["preconditions"]
                                if item["check"] == PERSON_CHECK]}
    shapes = {name: re.compile(spec["pattern"]) for name, spec in catalog.shapes.items()}
    return {"capabilities": capabilities, "shapes": shapes}


def _person_text(item: Mapping[str, Any]) -> str:
    """The prose precondition a ``person_confirms`` entry records (with its evidence
    link label, so the record always says the link is unverified)."""
    link = item.get("evidence_link")
    return item["statement"] + (f" (evidence link: {link})" if link else "")


def person_preconditions(capability: Any) -> list[str]:
    """The prose preconditions a person must confirm for ``capability`` (its catalog
    ``person_confirms`` entries); every proposal of the type records all of them."""
    entry = registry()["capabilities"].get(canonical(capability) or "")
    return list(entry["person_confirms"]) if entry is not None else []


def with_person_preconditions(capability: Any, preconditions: Sequence[Any]) -> list[Any]:
    """``preconditions`` plus each of the capability's person preconditions it lacks."""
    out = list(preconditions or ())
    out += [text for text in person_preconditions(capability) if text not in out]
    return out


def declared(capability: Any) -> bool:
    """Whether the canonical ``capability`` is an owner-system proposal type of the
    action catalog (with guards, or none for a test-host type). Every proposal tool
    requires it."""
    name = canonical(capability)
    return name is not None and name in registry()["capabilities"]


def available(capability: Any, *, test_host: bool = False) -> bool:
    """Whether a declared capability may be proposed on this host: ``hosts`` lists
    ``case_host`` (the production case host) and/or ``test`` (a host constructed with
    test capabilities only)."""
    entry = registry()["capabilities"].get(canonical(capability) or "")
    if entry is None:
        return False
    hosts = set(entry["hosts"])
    return "case_host" in hosts or (test_host and "test" in hosts)


def test_host_only(capability: Any) -> bool:
    """A declared capability offered on test hosts only (``hosts: ["test"]``)."""
    name = canonical(capability)
    return declared(name) and not available(name)


def _text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, str)):
        text = str(value).strip()
        return text or None
    return None


# The states of one field that may carry a property (``carrier_state``).
FIELD_STATES = ("value", "absent", "self_reference", "unreadable")


def read_field(row: Mapping[str, Any], path: str,
               columns: Sequence[str] | None = None) -> tuple[bool | str, Any]:
    """(read, value) of ``column``, ``column.jsonKey`` or a nested ``column.key.key`` (a
    key holding the whole dotted rest is read first) in a row. ``read`` is True,
    False (the field was read and is not there: an empty JSON column or a missing key)
    or why it could not be read at all (``column_not_mirrored``: the column is not in
    ``columns``, by default the row's own; ``json_unparseable``; ``json_not_object``)."""
    column, _, key = str(path).partition(".")
    if column not in (row if columns is None else columns):
        return "column_not_mirrored", None
    value = row.get(column)
    if not key:
        return True, value
    if value is None or (isinstance(value, str) and not value.strip()):
        return False, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return "json_unparseable", None
    if not isinstance(value, Mapping):
        return "json_not_object", None
    if key in value:
        return True, value[key]
    head, dot, rest = key.partition(".")
    if not dot or head not in value:
        return False, None
    if value[head] is not None and not isinstance(value[head], Mapping):
        return "json_not_object", None
    return read_field({head: value[head]}, f"{head}.{rest}")


def value_state(read: bool | str, value: Any,
                own: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The one classification of a field that may carry a property, used by the gap
    guard (``check_gap``), ``customer_po_coverage.classify_field`` (so the order drivers
    and the playbook evaluator's conditions) and the capability guards:

    * ``unreadable`` (``reason``): the field could not be read (``read_field``), or it
      holds a value that is not text or an integer (a float, boolean, list or object:
      what it says is not known, so it is never read as empty);
    * ``absent`` (``reason`` ``field_absent`` or ``empty``): read and holding nothing;
    * ``self_reference`` (``matches``): text equal to one of the same row's own
      identifiers (``own``: name -> text, see ``own_identifiers``), a placeholder such
      as a fallback copy of the record's own number, never a value of the property;
    * ``value``."""
    if isinstance(read, str):
        return {"state": "unreadable", "reason": read}
    if not read:
        return {"state": "absent", "reason": "field_absent"}
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"state": "absent", "reason": "empty"}
    text = _text(value)
    if text is None:
        return {"state": "unreadable", "reason": "value_not_text"}
    matches = sorted(name for name, ident in (own or {}).items() if ident == text)
    if matches:
        return {"state": "self_reference", "value": value, "matches": matches}
    return {"state": "value", "value": value}


# Numeric text: a plain ASCII decimal (sign, digits, one point). No exponent,
# underscore, thousands separator, non-ASCII digit or spelled-out infinity.
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def _number(value: Any) -> Decimal | None:
    """A finite Decimal of an int, float, Decimal or plain ASCII decimal text; else None
    (a boolean, a non-finite value, other text or anything else is not a number). A float
    is read through its shortest repr, so ``1509.0`` is ``Decimal('1509.0')`` (equal to
    1509)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, (int, Decimal)):
        number = Decimal(value)
    elif isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value.strip()):
        number = Decimal(value.strip())
    else:
        return None
    return number if number.is_finite() else None


def number_state(read: bool | str, value: Any) -> dict[str, Any]:
    """``value_state`` for a field declared numeric (a capability guard's ``numeric``,
    a ``when`` cell): ``unreadable`` (``reason``) when it could not be read or holds
    something that is not a finite number (boolean, list, object, ``NaN``, words);
    ``absent`` when read and holding nothing; else ``value`` with the raw ``value`` as
    the source sent it (its provenance) and its ``number``."""
    if isinstance(read, str):
        return {"state": "unreadable", "reason": read}
    if not read:
        return {"state": "absent", "reason": "field_absent"}
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"state": "absent", "reason": "empty"}
    number = _number(value)
    if number is None:
        return {"state": "unreadable", "reason": "value_not_number"}
    return {"state": "value", "value": value, "number": number}


def _field_state(row: Mapping[str, Any], path: str, numeric: bool) -> tuple[str, Any]:
    """(state, raw value) of a field read by its declared kind."""
    state = number_state(*read_field(row, path)) if numeric else carrier_state(row, path)
    return state["state"], state.get("value")


def carrier_state(row: Mapping[str, Any], path: str, *, own: Mapping[str, str] | None = None,
                  columns: Sequence[str] | None = None) -> dict[str, Any]:
    """``value_state`` of the field at ``path`` of a row (``read_field``)."""
    return value_state(*read_field(row, path, columns), own)


def own_identifiers(row: Mapping[str, Any], paths: Sequence[str],
                    columns: Sequence[str] | None = None) -> dict[str, str]:
    """path -> text of the row's own identifiers at ``paths`` that hold a value."""
    out = {}
    for path in paths:
        state = carrier_state(row, path, columns=columns)
        if state["state"] == "value":
            out[path] = str(_text(state["value"]))
    return out


def field_state(row: Mapping[str, Any], path: str) -> tuple[str, Any]:
    """(state, value) of ``column`` or ``column.jsonKey`` in a row (``carrier_state``
    without own identifiers): ``value``, ``absent`` or ``unreadable`` (a column not in
    the row, JSON text that does not parse or is not an object, or a value that is not
    text), so a guard never reads "could not read" as "empty"."""
    state = carrier_state(row, path)
    return state["state"], state.get("value")


def field_value(row: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """(present, value) of ``column`` or ``column.jsonKey`` in a row."""
    state, value = field_state(row, path)
    return state == "value", value


def bound(entry: Mapping[str, Any], relation: Any) -> bool:
    """Whether a capability entry binds ``relation`` (``target_relations``: exact names
    or trailing-``*`` prefixes)."""
    if not isinstance(relation, str):
        return False
    for pattern in entry.get("target_relations") or ():
        if relation == pattern or (pattern.endswith("*") and relation.startswith(pattern[:-1])):
            return True
    return False


def _same_value(left: Any, right: Any, numeric: bool) -> bool:
    if numeric:
        a, b = _number(left), _number(right)
        return a is not None and b is not None and a == b
    return _text(left) is not None and _text(left) == _text(right)


def _same_identifier(cell: Any, value: str | None) -> bool:
    """For the "is not the row's own number" checks: equal ignoring case (an order
    number in other case is still that order number)."""
    text = _text(cell)
    return text is not None and value is not None and text.casefold() == value.casefold()


def _same_key(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True,
                                                                         default=str)


def check(capability: str, payload: Mapping[str, Any], expected_effect: Any,
          citations: Sequence[str], read: Read) -> dict[str, Any]:
    """The verdict of the capability's guards: ``{guarded, passed, failures}``; each
    failure is ``{id, reason}``. An unregistered capability is ``guarded: false`` (and
    never ``passed``)."""
    reg = registry()
    entry = reg["capabilities"].get(canonical(capability) or "")
    if entry is None:
        return {"guarded": False, "passed": False,
                "failures": [{"id": "capability_undeclared",
                              "reason": "the capability is not declared in "
                                        "operating/action_catalog.json (or not canonical)"}]}
    effect = expected_effect if isinstance(expected_effect, Mapping) else None
    if effect is not None and not bound(entry, effect.get("relation")):
        # Every capability binds the relations it may write; an effect elsewhere is
        # refused first, whatever its payload and guards (a fixture capability included).
        return {"guarded": True, "passed": False,
                "failures": [{"id": "effect_relation_not_bound",
                              "reason": "the expected effect names a relation this "
                                        "capability does not write"}]}
    payload = payload if isinstance(payload, Mapping) else {}
    # The capability's declared numeric fields: read as numbers by every guard.
    numeric = frozenset(field for guard in entry.get("guards", ())
                        for field in guard.get("numeric", ()))
    missing = [name for name in entry.get("payload_required", ())
               if not (isinstance(payload.get(name), Mapping) and payload[name])
               and _text(payload.get(name)) is None
               and not (name in numeric and _number(payload.get(name)) is not None)]
    if missing:
        return {"guarded": True, "passed": False,
                "failures": [{"id": "payload_required",
                              "reason": "payload lacks " + ", ".join(missing)}]}
    cited = [str(handle) for handle in citations]
    rows: dict[str, Mapping[str, Any] | None] = {}

    def row_of(handle: str) -> Mapping[str, Any] | None:
        if handle not in rows:
            rows[handle] = read(handle) if handle in cited else None
        return rows[handle]

    failures = []
    target = None
    if effect is not None:
        for handle in cited:
            found = row_of(handle)
            if (found is not None and found.get("relation") == effect["relation"]
                    and _same_key(found.get("key"), effect.get("key"))):
                target = found
                break
    for guard in entry.get("guards", ()):
        reason = _guard(guard, payload, effect, target, row_of, reg["shapes"], numeric)
        if reason is not None:
            failures.append({"id": guard["id"], "reason": reason})
    return {"guarded": True, "passed": not failures, "failures": failures}


# The guards that keep a write from replacing what its target already holds. The
# real-system adapters (operating/action_adapters.py) re-check their fields on the
# owner's own record just before a write, not only on the cited row at registration.
NO_OVERWRITE_CHECKS = frozenset({"target_fields_empty", "target_fields_hold_no_shape"})


def no_overwrite_failures(capability: Any, row: Mapping[str, Any],
                          fields: Sequence[str]) -> list[dict[str, Any]]:
    """Each no-overwrite guard (``NO_OVERWRITE_CHECKS``) of the capability that fails
    on ``row``, a record as its owner returns it under the effect's field names, for
    its fields in ``fields`` (the fields the write sets); ``[]``: none fails.

    On the owner's record every such field must hold nothing, whichever of the two
    checks the guard is (``target_fields_empty`` is applied to its fields): the shape
    and own-number allowances of ``target_fields_hold_no_shape`` judge cited content at
    registration, but the approver approved setting a value, never replacing the one
    the owner holds now. A field the row does not carry is unreadable and fails. An
    undeclared capability fails."""
    reg = registry()
    entry = reg["capabilities"].get(canonical(capability) or "")
    if entry is None:
        return [{"id": "capability_undeclared",
                 "reason": "the capability is not declared in operating/action_catalog.json"}]
    numeric = frozenset(field for guard in entry["guards"]
                        for field in guard.get("numeric", ()))
    failures = []
    for guard in entry["guards"]:
        written = [path for path in guard.get("fields", ()) if path in fields]
        if guard["check"] not in NO_OVERWRITE_CHECKS or not written:
            continue
        empty = {"id": guard["id"], "check": "target_fields_empty", "fields": written}
        reason = _guard(empty, {}, None, {"row": dict(row)}, lambda handle: None,
                        reg["shapes"], numeric)
        if reason is not None:
            failures.append({"id": guard["id"], "reason": reason})
    return failures


def _guard(guard: Mapping[str, Any], payload: Mapping[str, Any], effect: Any, target: Any,
           row_of: Callable[[str], Any], shapes: Mapping[str, Any],
           numeric: frozenset[str] = frozenset()) -> str | None:
    value = _text(payload.get(guard.get("value"))) if guard.get("value") else None
    kind = guard["check"]
    if kind == "effect_fields_within":
        fields = list(effect_values(effect)) if isinstance(effect, Mapping) else None
        if not fields:
            return "the expected effect names no field"
        outside = [str(f) for f in fields if f not in guard["fields"]]
        return None if not outside else \
            "the expected effect names fields this capability does not write (" + \
            ", ".join(outside)[:200] + ")"
    if kind == "effect_targets_cited_row":
        return None if target is not None else \
            "the expected effect names no cited current row of the target relation"
    if kind == "target_fields_empty":
        if target is None:
            return "the expected effect names no cited current row of the target relation"
        states = {path: _field_state(target.get("row") or {}, path, path in numeric)[0]
                  for path in guard["fields"]}
        unread = [path for path, state in states.items() if state == "unreadable"]
        held = [path for path, state in states.items() if state == "value"]
        if unread:
            return "a field of the target could not be read (" + ", ".join(unread) + ")"
        return None if not held else "the target already holds " + ", ".join(held)
    if kind == "payload_value_matches_shape":
        return None if value and shapes[guard["shape"]].fullmatch(value) else \
            "the proposed value does not have the declared shape"
    if kind == "effect_equals_payload_value":
        if effect is None or target is None:
            return "the expected effect names no cited current row of the target relation"
        values = effect_values(effect)
        return None if values and all(_text(v) == value for v in values.values()) else \
            "the expected effect does not set the proposed value"
    if kind == "effect_values_match_payload":
        values = effect_values(effect) if isinstance(effect, Mapping) else {}
        if not values:
            return "the expected effect names no field"
        wrong = [field for field, expected in values.items()
                 if not _same_value(payload.get(field), expected, field in numeric)]
        return None if not wrong else \
            "the payload does not carry the value the effect writes for " + \
            ", ".join(wrong)[:200] + (" (quantities are numeric)" if numeric & set(wrong)
                                     else "")
    if kind == "payload_fields_in_cited_evidence":
        values = effect_values(effect) if isinstance(effect, Mapping) else {}
        sources = payload.get(guard["evidence"])
        if not values or not isinstance(sources, Mapping):
            return "the payload names no cited evidence per written field"
        if target is None:
            return "the expected effect names no cited current row of the target relation"
        for field, expected in values.items():
            source = sources.get(field)
            if not isinstance(source, Mapping) or set(source) != {"citation", "field"}:
                return f"no cited evidence names the value of {field} ({{citation, field}})"
            found = row_of(_text(source.get("citation")) or "")
            if found is None:
                return (f"the evidence citation of {field} is not one of the proposal's "
                        "current, readable rows")
            if (found.get("relation") == target.get("relation")
                    and _same_key(found.get("key"), target.get("key"))):
                return f"the evidence of {field} is the target row itself"
            path = str(source.get("field"))
            reason = _evidence_source(guard.get("sources", {}).get(field), field, found,
                                      path, target)
            if reason is not None:
                return reason
            state, cell = _field_state(found.get("row") or {}, path, field in numeric)
            if state != "value" or not _same_value(cell, expected, field in numeric):
                return f"the cited evidence field does not hold the value written to {field}"
        return None
    if kind in ("payload_value_in_cited_field", "payload_value_not_in_cited_fields"):
        handle = _text(payload.get(guard["citation"]))
        found = row_of(handle) if handle else None
        if found is None:
            return "the evidence citation is not one of the proposal's current, readable rows"
        row = found.get("row") or {}
        if kind == "payload_value_in_cited_field":
            present, cell = field_value(row, str(payload.get(guard["field"])))
            return None if present and _text(cell) == value else \
                "the cited evidence field does not hold exactly the proposed value"
        echoed = [path for path in guard["fields"]
                  if _same_identifier(field_value(row, path)[1], value)]
        return None if not echoed else \
            "the value is the evidence row's own number (" + ", ".join(echoed) + ")"
    if target is None:
        return "the expected effect names no cited current row of the target relation"
    row = target.get("row") or {}
    if kind == "target_fields_hold_no_shape":
        states = {path: field_state(row, path) for path in guard["fields"]}
        unread = [path for path, (state, _) in states.items() if state == "unreadable"]
        if unread:
            return ("a field of the target could not be read (" + ", ".join(unread) +
                    "): whether it already holds such a value is unknown")
        own = {_text(field_value(row, path)[1]) for path in guard.get("ignore_equal_to", ())}
        held = [path for path, (state, value) in states.items()
                if state == "value" and (cell := _text(value)) not in own
                and shapes[guard["shape"]].fullmatch(cell or "")]
        return None if not held else "the target already holds such a value (" + \
            ", ".join(held) + ")"
    echoed = [path for path in guard["fields"]
              if _same_identifier(field_value(row, path)[1], value)]
    return None if not echoed else \
        "the value equals the target row's own " + ", ".join(echoed)


def _evidence_source(allowed: Any, field: str, found: Mapping[str, Any], path: str,
                     target: Mapping[str, Any]) -> str | None:
    """When the guard lists ``sources`` for ``field``, the cited evidence must be one of
    them: that relation and field, its ``when`` cells (numerically) and its ``join`` to
    the target row (evidence row field == target row field)."""
    if allowed is None:
        return None
    row, target_row = found.get("row") or {}, target.get("row") or {}
    for spec in allowed:
        if spec["relation"] != found.get("relation") or spec["field"] != path:
            continue
        if any(_field_state(row, column, True)[0] != "value"
               or not _same_value(_field_state(row, column, True)[1], want, True)
               for column, want in (spec.get("when") or {}).items()):
            return (f"the evidence row of {field} does not meet the source's condition (" +
                    ", ".join(spec["when"]) + ")")
        join = spec.get("join")
        if join is not None:
            mine, theirs = field_state(row, join["field"]), field_state(target_row,
                                                                        join["target_field"])
            if mine[0] != "value" or theirs[0] != "value" or not (
                    _same_value(mine[1], theirs[1], True)
                    or _same_value(mine[1], theirs[1], False)):
                return f"the evidence row of {field} does not belong to the target row"
        return None
    return (f"the evidence of {field} is not a declared source for it (" +
            ", ".join(f"{spec['relation']}.{spec['field']}" for spec in allowed)[:200] + ")")


# Gap states that claim absence: the cited evidence must show it, so such a gap is
# structured (``property`` names what is absent) and checked before it is recorded.
ABSENCE_STATES = frozenset({"absent_in_source"})
MAX_GAP_PROPERTIES = 5
PROPERTY_SPELLING = ("<ObjectType>.<property> (a value of the cited row), "
                     "<ObjectType>.<link> (related rows) or <ObjectType> (the object itself), "
                     "names from plx.schema")


def gap_properties(about: Any) -> list[str] | None:
    """``property`` of a gap as a sorted list of distinct names (a string is one name);
    None when it is absent. Never validates against the schema (``resolve_gap_subject``)."""
    if about is None:
        return None
    names = [about] if isinstance(about, str) else about
    if not isinstance(names, (list, tuple)):
        return []
    return sorted({str(name).strip() for name in names if isinstance(name, str)
                   and str(name).strip()})


def _relations(spec: Mapping[str, Any]) -> set[str]:
    return {str(b.get("relation")) for b in spec.get("bindings") or ()
            if isinstance(b, Mapping) and b.get("source") == "portal_api"}


def property_carriers(object_types: Mapping[str, Any], type_id: str, name: str
                      ) -> dict[str, list[dict[str, str]]]:
    """Where property ``name`` of ``type_id`` is held on Portal relations, as schema data
    (``object_types``: compiled or as written in a domain file): ``sources``, its
    carrier fields, and ``placeholders``, the fields of the properties it declares in
    ``placeholder_when_equal_to`` (the record's own identifiers: a carrier value equal
    to one of them on the same row is a ``self_reference``, never a value of it). Each is
    ``[{relation, path}]``."""
    bindings = {b.get("id"): b for t in object_types.values() if isinstance(t, Mapping)
                for b in t.get("bindings") or () if isinstance(b, Mapping)}
    props = {item.get("name"): item for item in
             (object_types.get(type_id) or {}).get("properties") or ()
             if isinstance(item, Mapping)}

    def fields(prop: Any) -> list[dict[str, str]]:
        out = []
        for item in (prop or {}).get("sources") or ():
            binding = bindings.get(item.get("binding")) if isinstance(item, Mapping) else None
            if (binding is not None and binding.get("source") == "portal_api"
                    and isinstance(item.get("path"), str)):
                out.append({"relation": str(binding.get("relation")), "path": item["path"]})
        return out

    prop = props.get(name) or {}
    return {"sources": fields(prop),
            "placeholders": [field for other in prop.get("placeholder_when_equal_to") or ()
                             for field in fields(props.get(other))]}


def link_holding_relations(schema: Any, type_id: str) -> dict[str, list[str]]:
    """relation -> link fields of the Portal relations whose rows hold a link to, or an
    identifier of, an object of ``type_id``, as schema data: per link binding with stored
    ``pairs`` (fk, same-record or text-match, compared as ids, identifiers or text) where
    ``type_id`` is one end, the other end's relation and its pair fields that are neither
    its key nor one of its own identifiers (the properties of its type's
    ``identity_keys``): a PO line holds ``purchase_order_id``; a supplier row holds only
    its own ``id``, and a PO row's ``po_number`` is the PO's own number, not a link to the
    receipts that name it. Such a row, with those fields read empty, is where an object
    of the type can be shown absent from."""
    types = getattr(schema, "object_types", None) or {}
    bindings = {b.get("id"): b for t in types.values() if isinstance(t, Mapping)
                for b in t.get("bindings") or () if isinstance(b, Mapping)}
    own: dict[Any, set[str]] = {}
    for spec in types.values():
        if not isinstance(spec, Mapping):
            continue
        names = {name for key in spec.get("identity_keys") or () if isinstance(key, Mapping)
                 for name in key.get("properties") or ()}
        for prop in spec.get("properties") or ():
            if isinstance(prop, Mapping) and prop.get("name") in names:
                for item in prop.get("sources") or ():
                    if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                        own.setdefault(item.get("binding"), set()).add(item["path"])
    out: dict[str, set[str]] = {}
    for link in (getattr(schema, "link_types", None) or {}).values():
        if not isinstance(link, Mapping):
            continue
        for item in link.get("bindings") or ():
            pairs = item.get("pairs") if isinstance(item, Mapping) else None
            if not pairs:
                continue
            for end, other, field in (("to", "from_binding", "from"),
                                      ("from", "to_binding", "to")):
                holder = bindings.get(item.get(other))
                if (link.get(end) != type_id or holder is None
                        or holder.get("source") != "portal_api"):
                    continue
                fields = {str(pair.get(field)) for pair in pairs if isinstance(pair, Mapping)
                          and isinstance(pair.get(field), str)} - set(holder.get("key") or ()) \
                    - own.get(holder.get("id"), set())
                if fields:
                    out.setdefault(str(holder.get("relation")), set()).update(fields)
    return {relation: sorted(fields) for relation, fields in sorted(out.items())}


def resolve_gap_subject(schema: Any, about: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(``{type, relations, entries}``, None) for a gap ``property`` naming one object type
    of the schema, or (None, reason). Entries: ``{name, kind, sources?}`` with kind
    ``property`` (its source fields per relation: ``sources`` ``[{relation, path}]``),
    ``link`` (a link type from the type by name, or into it by reverse name) or ``object``
    (the bare type, alone, with ``holders``: ``link_holding_relations``). Which fields
    carry a property is schema data only."""
    names = gap_properties(about)
    if not names or len(names) > MAX_GAP_PROPERTIES or any(len(n) > 200 for n in names):
        return None, (f"property must name 1..{MAX_GAP_PROPERTIES} of: {PROPERTY_SPELLING}")
    types = getattr(schema, "object_types", None) or {}
    links = getattr(schema, "link_types", None) or {}
    type_ids = {name.partition(".")[0] for name in names}
    if len(type_ids) != 1:
        return None, "every property of one gap names the same object type"
    type_id = type_ids.pop()
    spec = types.get(type_id)
    if not isinstance(spec, Mapping):
        return None, f"{type_id} is not an object type of plx.schema"
    relations = _relations(spec)
    entries: list[dict[str, Any]] = []
    for name in names:
        _, dot, member = name.partition(".")
        if not dot:
            if len(names) > 1:
                return None, f"the bare type {type_id} (the object itself) stands alone"
            entries.append({"name": name, "kind": "object",
                            "holders": link_holding_relations(schema, type_id)})
            continue
        if any(isinstance(item, Mapping) and item.get("name") == member
               for item in spec.get("properties") or ()):
            entries.append({"name": name, "kind": "property",
                            **property_carriers(types, type_id, member)})
            continue
        if any(isinstance(link, Mapping)
               and ((link.get("from") == type_id and link.get("name") == member)
                    or (link.get("to") == type_id and link.get("reverse_name") == member))
               for link in links.values()):
            entries.append({"name": name, "kind": "link"})
            continue
        return None, f"{type_id} declares no property or link {member!r} (see plx.schema)"
    return {"type": type_id, "relations": relations, "entries": entries}, None


def properties_of_fields(schema: Any, relation: str, fields: Sequence[str]) -> list[str]:
    """The schema names (``<ObjectType>.<property>``) of the properties that ``relation``
    carries in ``fields`` (source paths), in field order: how a driver that read columns
    names them for a gap. A field no property is bound to on that relation is skipped."""
    types = getattr(schema, "object_types", None) or {}
    bindings = {b.get("id"): (tid, b) for tid, t in types.items() if isinstance(t, Mapping)
                for b in t.get("bindings") or () if isinstance(b, Mapping)}
    out: list[str] = []
    for path in fields:
        for tid, t in types.items():
            for prop in (t.get("properties") or ()) if isinstance(t, Mapping) else ():
                hit = any(isinstance(item, Mapping) and item.get("path") == path
                          and bindings.get(item.get("binding"), (None, {}))[1].get("relation")
                          == relation for item in prop.get("sources") or ())
                if hit and f"{tid}.{prop['name']}" not in out:
                    out.append(f"{tid}.{prop['name']}")
    return out


def _refused(failure_id: str, reason: str, checked: list[dict[str, Any]] | None = None
             ) -> dict[str, Any]:
    return {"guarded": True, "passed": False, "checked": checked or [],
            "failures": [{"id": failure_id, "reason": reason}]}


def check_gap(state: str, about: Any, citations: Sequence[str], read: Read,
              schema: Any) -> dict[str, Any]:
    """The verdict of the gap guard: ``{guarded, passed, failures, checked}``.

    ``about`` is the gap's ``property``: what is missing, as schema names
    (``resolve_gap_subject``). An absence gap (``ABSENCE_STATES``) must name it, and is
    checked on every cited ``api://`` row as it is now (``read``):

    * a property: each cited row of a relation that carries it is read on every field
      the schema binds the property to on that relation (all carriers, e.g. a column and
      a JSON key), each by ``carrier_state``: a held value refuses the gap (the evidence
      contradicts it); a value equal to the row's own identifier named by the schema
      (``placeholder_when_equal_to``, e.g. a PO field holding the order's own number)
      is a ``self_reference``, absent for this property, and reported in ``checked``
      (``self_references``: field -> the identifiers it equals); an unreadable row,
      field, JSON text or non-text value refuses it (absence is not shown: record
      ``unknown``; a column the row does not carry is unreadable, never empty), and a
      gap citing no carrying row is refused;
    * a link: the gap must cite a row of the object it is absent from (the subject);
      the related rows' absence is the completed read the caller did, not re-checked;
    * the bare object type: a cited row of that type contradicts it, and the gap must
      cite the subject record it is absent from: a readable row, with a key, of a
      relation that holds a link to or an identifier of the type
      (``link_holding_relations``), on which every such link field is present and
      empty (``carrier_state`` ``absent``). None cited: ``absence_needs_citation``; a
      set link (it names an object of the type: whether that one exists needs a search,
      and a search shows nothing) contradicts it; a link column the row does not carry
      is unreadable; a row with no key is ``gap_subject_key_missing``. Record
      ``unknown`` in each case.

    Only ``api://`` rows are evidence of absence: any other citation (an original, a
    mail) may be carried along but never counts. An absence gap citing nothing is
    refused (``absence_needs_citation``). Any other state may name a property; it must
    then be declared."""
    names = gap_properties(about)
    if names is None:
        if state in ABSENCE_STATES:
            return _refused("absence_needs_property",
                            f"an {state} gap must say what is absent: pass property as "
                            f"{PROPERTY_SPELLING}, and cite the row(s) it is absent from; "
                            "if absence cannot be shown, record the gap as unknown")
        return {"guarded": False, "passed": True, "failures": [], "checked": []}
    subject, reason = resolve_gap_subject(schema, names)
    if subject is None:
        return _refused("gap_property_undeclared", str(reason))
    if state not in ABSENCE_STATES:
        return {"guarded": True, "passed": True, "failures": [], "checked": []}
    if not any(str(handle).strip() for handle in citations):
        return _refused("absence_needs_citation",
                        f"an {state} gap must cite the row(s) or search it is absent from; "
                        "if absence cannot be shown, record the gap as unknown")
    failures: list[dict[str, str]] = []
    checked: list[dict[str, Any]] = []
    rows: list[tuple[str, Mapping[str, Any]]] = []
    for handle in dict.fromkeys(str(handle) for handle in citations):
        if not handle.startswith("api://"):
            continue
        found = read(handle)
        if found is None:
            return _refused("gap_evidence_unreadable",
                            "a cited row cannot be read as current content; absence is not "
                            "shown (record the gap as unknown)")
        rows.append((handle, found))
    subject_rows = [handle for handle, found in rows
                    if found.get("relation") in subject["relations"]]
    for entry in subject["entries"]:
        if entry["kind"] == "object":
            if subject_rows:
                failures.append({"id": "gap_contradicted_by_cited_evidence",
                                 "reason": f"a cited row is a {entry['name']} row, so the "
                                           "object is not absent (record unknown if it is "
                                           "not the one asked for)"})
                checked.append({"property": entry["name"], "kind": "object"})
                continue
            holders = [(handle, found) for handle, found in rows
                       if found.get("relation") in entry["holders"]]
            if not holders:
                failures.append({"id": "absence_needs_citation",
                                 "reason": f"absence of {entry['name']} must cite a readable "
                                           "api:// row of a relation that links to or "
                                           "identifies it, the row it is absent from; a "
                                           "search that found nothing shows nothing (record "
                                           "the gap as unknown)"})
                checked.append({"property": entry["name"], "kind": "object"})
                continue
            for handle, found in holders:
                failures.extend(_object_link_failures(entry, handle, found, checked))
            continue
        if entry["kind"] == "link":
            if not subject_rows:
                failures.append({"id": "gap_subject_not_cited",
                                 "reason": f"absence of {entry['name']} must cite the "
                                           f"{subject['type']} row it is absent from"})
            checked.append({"property": entry["name"], "kind": "link", "rechecked": False})
            continue
        carried = False
        for handle, found in rows:
            paths = list(dict.fromkeys(item["path"] for item in entry["sources"]
                                       if item["relation"] == found.get("relation")))
            if not paths:
                continue
            carried = True
            row = found.get("row") or {}
            own = own_identifiers(row, [item["path"] for item in entry["placeholders"]
                                        if item["relation"] == found.get("relation")])
            states = {path: carrier_state(row, path, own=own) for path in paths}
            fields = {path: item["state"] for path, item in states.items()}
            record: dict[str, Any] = {"property": entry["name"], "citation": handle,
                                      "fields": fields}
            echoes = {path: item["matches"] for path, item in states.items()
                      if item["state"] == "self_reference"}
            if echoes:
                record["self_references"] = echoes
            checked.append(record)
            held = [path for path, status in fields.items() if status == "value"]
            if held:
                failures.append({"id": "gap_contradicted_by_cited_evidence",
                                 "reason": f"the cited row holds a value for {entry['name']} "
                                           f"({', '.join(held)}); it is not absent in this "
                                           "source (if the value is not a usable one, record "
                                           "the gap as unknown)"})
            elif "unreadable" in fields.values():
                failures.append({"id": "gap_property_unreadable",
                                 "reason": f"a field that carries {entry['name']} on a cited "
                                           "row could not be read (missing column, invalid "
                                           "JSON or a non-text value); absence is not shown "
                                           "(record the gap as unknown)"})
        if not carried:
            failures.append({"id": "gap_property_not_in_cited_rows",
                             "reason": f"absence of {entry['name']} must cite a row of a "
                                       "relation the schema binds it to"})
    return {"guarded": True, "passed": not failures, "failures": failures,
            "checked": checked}


def _object_link_failures(entry: Mapping[str, Any], handle: str, found: Mapping[str, Any],
                          checked: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Failures of one cited subject row for an object-kind absence (``check_gap``):
    every link field of its relation to the type must read ``absent``."""
    row = found.get("row") or {}
    paths = entry["holders"][found.get("relation")]
    fields = {path: carrier_state(row, path)["state"] for path in paths}
    checked.append({"property": entry["name"], "kind": "object", "citation": handle,
                    "fields": fields})
    if not (isinstance(found.get("key"), Mapping) and found["key"]):
        return [{"id": "gap_subject_key_missing",
                 "reason": f"absence of {entry['name']} must cite the subject record by its "
                           "key; a row with no key shows nothing (record the gap as unknown)"}]
    held = [path for path, state in fields.items() if state in ("value", "self_reference")]
    if held:
        return [{"id": "gap_contradicted_by_cited_evidence",
                 "reason": f"the cited row links to a {entry['name']} ({', '.join(held)}); "
                           "whether that one exists needs a search, which shows nothing "
                           "(record the gap as unknown)"}]
    if "unreadable" in fields.values():
        return [{"id": "gap_property_unreadable",
                 "reason": f"a field that links the cited row to {entry['name']} could not "
                           "be read (missing column, invalid JSON or a non-text value); "
                           "absence is not shown (record the gap as unknown)"}]
    return []


def current_reader(runtime: Any, actor: Any, max_staleness: float | None = None) -> Read:
    """``read`` over the case runtime for ``actor``: an ``api://`` citation resolved to
    current content (same capture, not superseded, returned by the latest traversal),
    or a byte-pinned document citation (``_document``); anything else None. A row the
    mirror filled from a persisted capture while the live source was out
    (``source_freshness``) is never current: a copy is advisory, never action authority,
    however recent it is. The row is cut to the relation's captured column list (the
    mirror's own), so a column the upstream did not serve reads as unreadable, never as
    an empty value."""
    columns: dict[str, frozenset[str]] = {}

    def captured(relation: str) -> frozenset[str] | None:
        if not columns:
            try:
                listing = runtime.mirror.catalog(actor)
            except Exception:  # noqa: BLE001 - no column list: nothing is readable
                return None
            columns.update({item["name"]: frozenset(item.get("columns") or ())
                            for item in listing.get("relations") or ()})
        return columns.get(relation)

    def read(handle: str) -> Mapping[str, Any] | None:
        if isinstance(handle, str) and handle.startswith(DOCUMENT_SCHEMES):
            return _document(runtime, actor, handle)
        if not isinstance(handle, str) or not handle.startswith("api://"):
            return None
        try:
            result = runtime.mirror.resolve_citation(handle, actor, max_staleness=(
                runtime.mirror.default_max_staleness_seconds if max_staleness is None
                else max_staleness))
        except Exception:  # noqa: BLE001 - unreadable for this reader: the guard fails
            return None
        if (not result.get("ok") or result.get("status") != "current"
                or result.get("source_freshness") is not None
                or result.get("changed_since") or "row" not in result
                or (result.get("refresh") or {}).get("error")
                or result.get("upstream_presence") in ("absent_from_latest_full_traversal",
                                                       "absent_from_latest_key_scan")):
            return None
        served = captured(result.get("relation"))
        if served is None:
            return None
        row = {column: value for column, value in result["row"].items() if column in served}
        return {"relation": result.get("relation"), "key": result.get("key"), "row": row}
    return read


# Byte-pinned document citations the guards read (decision chain-document-evidence,
# option 2): a SharePoint file version and a mail file attachment.
DOCUMENT_SCHEMES = ("sharepoint://", "graph-mail://")


def _document(runtime: Any, actor: Any, handle: str) -> Mapping[str, Any] | None:
    """The ``documentFields`` row of a byte-pinned document citation as ``actor`` may read
    it now, or None: the pinned bytes served through the case runtime's capture gate
    (``CaseRuntime.captures``, the gate a case citation and ``plx.read`` of the capture
    use, current versions only), re-hashed and re-extracted
    (``ontology.document_fields.pinned_document_row``). A row carries what the page
    states (``fields.<name>.value``), never a record's own fields; ``name`` is the file name
    the capture records for the version (the approver is told which file, never as a
    field). A host without document captures, a version the capture does not hold for this
    reader, a refused, superseded or other citation and bytes of another digest read as
    None (never live, never the projection store)."""
    captures = getattr(runtime, "captures", None)
    if captures is None or not callable(getattr(captures, "document", None)):
        return None
    try:
        from ontology.document_fields import pinned_document_row
        served = captures.document(handle, actor)
        if served is None:
            return None
        found = pinned_document_row(handle, served["l1_handle"], served["content"],
                                    served["row"], served["text"],
                                    reading=served.get("reading"))
    except Exception:  # noqa: BLE001 - unreadable for this reader: the guard fails
        return None
    if not found.get("ok"):
        return None
    row = found["row"]
    return {"relation": found["relation"],
            "key": {"source_key": row["source_key"], "profile": row["profile"]}, "row": row,
            "name": served["row"].get("name")}


__all__ = ["ABSENCE_STATES", "CHECKS", "FIELD_STATES", "GuardError", "MAX_GAP_PROPERTIES",
           "available", "bound", "canonical", "carrier_state", "check", "check_gap",
           "current_reader", "declared", "field_state", "field_value", "gap_properties",
           "link_holding_relations", "NO_OVERWRITE_CHECKS", "no_overwrite_failures",
           "number_state", "own_identifiers",
           "person_preconditions", "properties_of_fields", "property_carriers", "read_field",
           "registry", "resolve_gap_subject", "value_state", "with_person_preconditions"]
