"""Source unit contracts: the unit a source column is recorded in, with its evidence.

A calculation input read from a cited field otherwise carries whatever unit the
caller (a model) declared. That is how a 0-1 fraction ends up treated as a
percentage (Portal TASK-2117: formula fractions shown as percentages on batch
cards) or a percent-scale gold column is mixed with a fraction-scale silver
column. A contract pins the unit of one ``relation.column`` from recorded
evidence; ``CaseStore.compute`` then refuses a declared unit that contradicts
it, fills the unit when the caller omits it, and records on every input whether
its unit came from a source contract or was only declared by the caller.

A contract may instead (or also) pin the column's quantity ``role`` (planned,
manufactured, filled, ...): ``CaseStore.compute`` then records that role and
refuses to combine it with a different role without a declared relation.

A contract may also declare the ``time_zone`` (an IANA name) a timestamp column's
source keeps its business days in: the claim gate (``claim_checks``) reads a claimed
date of that column's instant as its day in that zone, never a zone it guesses.

A contract says nothing about whether the column's values are correct, current
or populated; it only fixes the scale (or role) they are recorded in. Contracts apply to
``api://`` source-mirror citations and to top-level columns only
(``row.<column>``, ``rows.<n>.<column>`` or the key copy ``key.<column>``);
any other field stays
caller-declared.

File format ``plx-source-unit-contracts/v1``::

    {"schema": "plx-source-unit-contracts/v1",
     "contracts": [{"id": "...", "relation": "schema.relation", "column": "name",
                    "unit": "1", "role": "planned", "time_zone": "...", "meaning": "...",
                    "evidence": ["..."], "verification": "..."}]}

At least one of ``unit``, ``role`` and ``time_zone`` is required.

A contract may apply only to rows where another column holds one value
(``"when": {"column": "task_type", "equals": "Filling"}``): a task table's
``qty`` is a filled count on a Filling task and something else on another task.
A column has either one unconditional contract or conditional ones with
distinct conditions; a row no condition matches has no contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ontology.core import ValidationError

from .cases import format_unit, parse_unit

SCHEMA = "plx-source-unit-contracts/v1"
BUILTIN_PATH = Path(__file__).with_name("source_unit_contracts.json")
SUPPORTED_SCHEME = "api://"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RELATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_CONTRACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_FIELD = re.compile(r"^(?:row|key|rows\.(?:0|[1-9][0-9]*))\.([A-Za-z_][A-Za-z0-9_]*)$")
_MAX_CONTRACTS = 500
_MAX_TEXT = 1000
_DOCUMENT_KEYS = {"schema", "note", "contracts"}
_CONTRACT_KEYS = {"id", "relation", "column", "unit", "role", "meaning", "evidence",
                  "verification", "when", "time_zone"}
_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise ValidationError(f"unit_contract_invalid_{key}:{where}")
    return value.strip()


def _condition_holds(condition: tuple[str, str] | None, resolved: Mapping[str, Any],
                     field: str) -> bool:
    """An unconditional contract always applies; a conditional one when the cited row's
    ``column`` holds ``equals`` (trimmed text; numbers compare by their text)."""
    if condition is None:
        return True
    row: Any = resolved.get("row")
    if field.startswith("rows."):
        rows = resolved.get("rows")
        index = int(field.split(".")[1])
        row = rows[index] if isinstance(rows, list) and index < len(rows) else None
    if not isinstance(row, Mapping):
        return False
    value = row.get(condition[0])
    if isinstance(value, bool) or value is None:
        return False
    return str(value).strip() == condition[1]


class SourceUnitContracts:
    """Validated, immutable ``(relation, column) -> contract`` lookup."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
            raise ValidationError("unit_contracts_schema_mismatch")
        if set(document) - _DOCUMENT_KEYS:
            raise ValidationError("unit_contracts_unknown_key:" +
                                  ",".join(sorted(map(str, set(document) - _DOCUMENT_KEYS))))
        entries = document.get("contracts")
        if not isinstance(entries, list) or len(entries) > _MAX_CONTRACTS:
            raise ValidationError("unit_contracts_invalid_list")
        by_column: dict[tuple[str, str], list[dict[str, Any]]] = {}
        ids: set[str] = set()
        for index, entry in enumerate(entries):
            where = str(index)
            if not isinstance(entry, Mapping):
                raise ValidationError(f"unit_contract_not_object:{where}")
            contract_id = entry.get("id")
            if not isinstance(contract_id, str) or not _CONTRACT_ID.match(contract_id):
                raise ValidationError(f"unit_contract_invalid_id:{where}")
            where = contract_id
            if set(entry) - _CONTRACT_KEYS:
                raise ValidationError(f"unit_contract_unknown_key:{where}")
            relation, column = entry.get("relation"), entry.get("column")
            if not isinstance(relation, str) or not _RELATION.match(relation):
                raise ValidationError(f"unit_contract_invalid_relation:{where}")
            if not isinstance(column, str) or not _IDENT.match(column):
                raise ValidationError(f"unit_contract_invalid_column:{where}")
            role = entry.get("role")
            if role is not None and (not isinstance(role, str) or not _ROLE.match(role)):
                raise ValidationError(f"unit_contract_invalid_role:{where}")
            zone = entry.get("time_zone")
            if zone is not None:
                try:
                    ZoneInfo(zone)
                except (ZoneInfoNotFoundError, ValueError, TypeError, OSError) as exc:
                    raise ValidationError(f"unit_contract_invalid_time_zone:{where}") from exc
            try:
                unit = (None if (role is not None or zone is not None)
                        and entry.get("unit") is None
                        else format_unit(parse_unit(entry.get("unit"))))
            except ValidationError as exc:
                raise ValidationError(f"unit_contract_invalid_unit:{where}") from exc
            evidence = entry.get("evidence")
            if (not isinstance(evidence, list) or not evidence or len(evidence) > 20 or
                    not all(isinstance(item, str) and item.strip() and len(item) <= _MAX_TEXT
                            for item in evidence)):
                raise ValidationError(f"unit_contract_evidence_required:{where}")
            when = entry.get("when")
            if when is not None and (
                    not isinstance(when, Mapping) or set(when) != {"column", "equals"}
                    or not isinstance(when["column"], str) or not _IDENT.match(when["column"])
                    or not isinstance(when["equals"], str) or not when["equals"].strip()
                    or len(when["equals"]) > 200):
                raise ValidationError(f"unit_contract_invalid_when:{where}")
            if contract_id in ids:
                raise ValidationError(f"unit_contract_duplicate_id:{where}")
            existing = by_column.setdefault((relation, column), [])
            condition = None if when is None else (when["column"], when["equals"].strip())
            if any(other.get("_when") is None or condition is None
                   or other["_when"] == condition for other in existing):
                raise ValidationError(f"unit_contract_duplicate_column:{relation}.{column}")
            ids.add(contract_id)
            existing.append({
                "id": contract_id, "relation": relation, "column": column,
                **({"unit": unit} if unit is not None else {}),
                **({"role": role} if role is not None else {}),
                **({"time_zone": zone} if zone is not None else {}),
                **({"when": {"column": condition[0], "equals": condition[1]}}
                   if condition is not None else {}),
                "_when": condition,
                "meaning": _text(entry, "meaning", where),
                "verification": _text(entry, "verification", where),
                "evidence": [item.strip() for item in evidence]})
        self._by_column = by_column

    @classmethod
    def from_file(cls, path: str | Path) -> "SourceUnitContracts":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError("unit_contracts_unreadable") from exc
        return cls(document)

    @classmethod
    def builtin(cls) -> "SourceUnitContracts":
        return cls.from_file(BUILTIN_PATH)

    def __len__(self) -> int:
        return sum(len(items) for items in self._by_column.values())

    def lookup(self, handle: str, resolved: Mapping[str, Any], field: str
               ) -> dict[str, Any] | None:
        """The contract for the column ``field`` reads from a resolved citation, if any.

        The relation comes from the resolved view (what the resolver actually
        returned for the actor), never from parsing the handle text.
        """
        if not isinstance(handle, str) or not handle.startswith(SUPPORTED_SCHEME):
            return None
        relation = resolved.get("relation") if isinstance(resolved, Mapping) else None
        match = _FIELD.match(field) if isinstance(field, str) else None
        if not isinstance(relation, str) or match is None:
            return None
        matched = [contract for contract in self._by_column.get((relation, match.group(1)), [])
                   if _condition_holds(contract["_when"], resolved, field)]
        if len(matched) != 1:
            return None
        contract = {key: value for key, value in matched[0].items() if key != "_when"}
        return dict(contract, evidence=list(contract["evidence"]))

    __call__ = lookup


__all__ = ["BUILTIN_PATH", "SCHEMA", "SourceUnitContracts"]
