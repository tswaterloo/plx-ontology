"""Workbench-checked links between two cited source-mirror rows.

A relationship claim such as "AO-26005B is linked by order line 1140 to sales
order 351" used to be checked in client code (the former AO case script)
and then recorded as a caller-authored fact whose citations merely resolved.
Here the Workbench checks it: ``check(contract_id, from_view, to_view)``
evaluates one declared **link contract** over the two rows the caller cited
(as ``CaseRuntime.resolve_citation`` returned them for that caller) and says
``verified``, ``lead``, ``contradicted`` or ``unknown``. ``CaseStore.link``
records the outcome with both citations, the contract id and digest and the
evaluated values; ``plx.case_add_finding`` link mode is the MCP tool.

Link contracts (``operating/link_contracts.json``, ``plx-case-link-contracts/v1``)
are declarative and carry their evidence (Portal ``path@commit``,
live traces). Three match kinds:

* ``fk``: one or more column pairs are equal (``from.idassembly == to.id``);
* ``json_path``: the same, where a side reads one key of a JSON-object column
  (``custom_fields.fmGeneratedPoId``), because that is the only link the
  source keeps;
* ``fk`` with ``provenance: source_set_by_text_join``: the source stores an id
  but filled it by a text join (Portal's ``app_receipt.purchase_order_id``);
  agreement is recorded as ``stored_by_text_join``, never as verified, and never
  supports a relation claim; differing ids still contradict;
* ``text_match``: two free-text values agree after trimming spaces (a receipt's
  ``po_reference`` and a PO's ``po_number``; a BC invoice's external document
  number and an order number). Agreement is recorded as a **lead**, never as a
  verified link; disagreement proves nothing and is not recorded.

Values are compared by a declared type, never loosely: ``integer_id`` accepts
an int, an integral ``Decimal``, a digit string or an integral float/decimal text (``"1140.0"``) and
compares the integer; ``text_id`` compares the exact text (ints as their
decimal text); ``text`` (text_match only) compares text with spaces trimmed
(only U+0020, like Postgres ``TRIM``), case-sensitive as Portal's
``TRIM(po_number) = ponoqc``. A verified link means the two cited rows agree
on the stored value; it never says the source value is correct. A value that is absent, empty, not
of the declared type, or a cited key that names more than one row makes the
outcome ``unknown`` (with the reason): exactly one verified answer or unknown,
never a winner. Only typed, present values that differ are ``contradicted``.

A contract that names an ``fm_source_fk`` relation must use that relation's
physical FK column from ``ontology/relation_contracts.py`` on its from side, so
the two declarations cannot drift apart.

How a case driver adopts this (the reference AO investigation of the tests does
not): instead of reading ``link.idassembly`` and comparing ids in the
client, call ``plx.case_add_finding`` with ``link`` per hop (link row -> AO, link row -> order line,
order line -> order), which records each verified hop as a Workbench-written
supported finding (or a contradicted one); then, through ``CaseStore.finding``
/ ``CaseRuntime``, a multi-hop relation claim passes ``relation_links`` (the
link ids) and may be ``supported`` only while every link is verified and its
rows still have the content that was checked.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from ontology.core import ValidationError
from ontology.relation_contracts import FM_SOURCE_FK

SCHEMA = "plx-case-link-contracts/v1"
BUILTIN_PATH = Path(__file__).with_name("link_contracts.json")
SUPPORTED_SCHEME = "api://"
MATCHES = ("fk", "json_path", "text_match")
COMPARES = ("integer_id", "text_id", "text")
OUTCOMES = ("verified", "stored_by_text_join", "lead", "contradicted")  # recorded
STRENGTH = {"fk": "verified", "json_path": "verified", "text_match": "lead"}
# How the source set the stored link value. ``source_set_by_text_join``: the
# source stores an id, but filled it by matching free text (Portal's receipt
# purchase_order_id, set by TRIM(po_number) = ponoqc): agreement is recorded as
# ``stored_by_text_join``, which, like a lead, never supports a relation claim;
# typed values that differ still contradict.
PROVENANCES = ("source_set_by_text_join",)

_CONTRACT_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
_RELATION = re.compile(r"^[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{0,63}$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PATH = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,63})(?:\.([A-Za-z_][A-Za-z0-9_]{0,63}))?$")
_INTEGER_TEXT = re.compile(r"^(?:0|[1-9][0-9]{0,17})(?:\.0+)?$")
_MAX_CONTRACTS = 200
_MAX_PAIRS = 4
_MAX_TEXT = 1200
_MAX_VALUE_CHARS = 200
_DOCUMENT_KEYS = {"schema", "note", "contracts"}
_CONTRACT_KEYS = {"id", "from", "to", "match", "pairs", "fm_source_fk", "meaning",
                  "evidence", "verification", "provenance", "provenance_note"}


def _sha(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise ValidationError(f"link_contract_invalid_{key}:{where}")
    return value.strip()


def _relation(value: Any, side: str, where: str) -> str:
    relation = value.get("relation") if isinstance(value, Mapping) else None
    if (not isinstance(value, Mapping) or set(value) != {"relation"}
            or not isinstance(relation, str) or not _RELATION.match(relation)):
        raise ValidationError(f"link_contract_invalid_{side}:{where}")
    return relation


class LinkContracts:
    """Validated, immutable ``id -> link contract`` lookup and the link check."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
            raise ValidationError("link_contracts_schema_mismatch")
        if set(document) - _DOCUMENT_KEYS:
            raise ValidationError("link_contracts_unknown_key")
        entries = document.get("contracts")
        if not isinstance(entries, list) or not entries or len(entries) > _MAX_CONTRACTS:
            raise ValidationError("link_contracts_invalid_list")
        contracts: dict[str, dict[str, Any]] = {}
        for index, entry in enumerate(entries):
            contract = self._contract(entry, str(index))
            if contract["id"] in contracts:
                raise ValidationError(f"link_contract_duplicate_id:{contract['id']}")
            contracts[contract["id"]] = contract
        self._contracts = contracts

    @staticmethod
    def _contract(entry: Any, where: str) -> dict[str, Any]:
        if not isinstance(entry, Mapping):
            raise ValidationError(f"link_contract_not_object:{where}")
        contract_id = entry.get("id")
        if not isinstance(contract_id, str) or not _CONTRACT_ID.match(contract_id):
            raise ValidationError(f"link_contract_invalid_id:{where}")
        where = contract_id
        if set(entry) - _CONTRACT_KEYS:
            raise ValidationError(f"link_contract_unknown_key:{where}")
        source, target = (_relation(entry.get("from"), "from", where),
                          _relation(entry.get("to"), "to", where))
        match = entry.get("match")
        if match not in MATCHES:
            raise ValidationError(f"link_contract_invalid_match:{where}")
        pairs = entry.get("pairs")
        if not isinstance(pairs, list) or not 1 <= len(pairs) <= _MAX_PAIRS:
            raise ValidationError(f"link_contract_invalid_pairs:{where}")
        checked = []
        for pair in pairs:
            if not isinstance(pair, Mapping) or set(pair) != {"from", "to", "compare"}:
                raise ValidationError(f"link_contract_invalid_pair:{where}")
            if pair["compare"] not in COMPARES:
                raise ValidationError(f"link_contract_invalid_compare:{where}")
            if (pair["compare"] == "text") != (match == "text_match"):
                # Free text is only ever a lead; typed ids are never text-matched.
                raise ValidationError(f"link_contract_compare_does_not_fit_match:{where}")
            for side in ("from", "to"):
                if not isinstance(pair[side], str) or not _PATH.match(pair[side]):
                    raise ValidationError(f"link_contract_invalid_field:{where}")
            checked.append({"from": pair["from"], "to": pair["to"],
                            "compare": pair["compare"]})
        nested = any("." in pair[side] for pair in checked for side in ("from", "to"))
        if match == "fk" and nested:
            raise ValidationError(f"link_contract_fk_reads_json_key:{where}")
        if match == "json_path" and not nested:
            raise ValidationError(f"link_contract_json_path_without_json_key:{where}")
        fm_fk = entry.get("fm_source_fk")
        if fm_fk is not None:
            names = FM_SOURCE_FK.get(fm_fk) if isinstance(fm_fk, str) else None
            if names is None or [pair["from"] for pair in checked] != [names[0]]:
                raise ValidationError(f"link_contract_fm_source_fk_mismatch:{where}")
        provenance = entry.get("provenance")
        if provenance is not None and (provenance not in PROVENANCES or match == "text_match"):
            raise ValidationError(f"link_contract_invalid_provenance:{where}")
        if (provenance is None) != (entry.get("provenance_note") is None):
            raise ValidationError(f"link_contract_provenance_needs_note:{where}")
        strength = "stored_by_text_join" if provenance else STRENGTH[match]
        evidence = entry.get("evidence")
        if (not isinstance(evidence, list) or not evidence or len(evidence) > 20 or
                not all(isinstance(item, str) and item.strip() and len(item) <= _MAX_TEXT
                        for item in evidence)):
            raise ValidationError(f"link_contract_evidence_required:{where}")
        contract = {"id": contract_id, "from": source, "to": target, "match": match,
                    "strength": strength, "provenance": provenance,
                    **({"provenance_note": _text(entry, "provenance_note", where)}
                       if provenance else {}),
                    "pairs": checked,
                    "fm_source_fk": fm_fk, "meaning": _text(entry, "meaning", where),
                    "evidence": [item.strip() for item in evidence],
                    "verification": _text(entry, "verification", where)}
        contract["digest"] = _sha(contract)
        return contract

    @classmethod
    def from_file(cls, path: str | Path) -> "LinkContracts":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError("link_contracts_unreadable") from exc
        return cls(document)

    @classmethod
    def builtin(cls) -> "LinkContracts":
        return cls.from_file(BUILTIN_PATH)

    def __len__(self) -> int:
        return len(self._contracts)

    def ids(self) -> list[str]:
        return sorted(self._contracts)

    def get(self, contract_id: Any) -> dict[str, Any]:
        contract = self._contracts.get(contract_id) if isinstance(contract_id, str) else None
        if contract is None:
            raise ValidationError("unknown_link_contract")
        return json.loads(json.dumps(contract))

    def check(self, contract_id: Any, from_handle: str, from_view: Mapping[str, Any],
              to_handle: str, to_view: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate one contract over two resolved citations.

        Refuses (``ValidationError``) citations that are not source-mirror rows or
        whose relations are not the contract's; otherwise returns the outcome
        (``verified``, ``lead``, ``contradicted``, ``no_match`` or ``unknown``),
        the reason for the last two, and every evaluated pair.
        """
        contract = self.get(contract_id)
        for handle, view, side in ((from_handle, from_view, "from"),
                                   (to_handle, to_view, "to")):
            if not isinstance(handle, str) or not handle.startswith(SUPPORTED_SCHEME):
                raise ValidationError(f"link_requires_api_citation:{side}")
            if not isinstance(view, Mapping) or view.get("relation") != contract[side]:
                raise ValidationError(f"link_citation_relation_is_not_contract_{side}:"
                                      f"{contract['id']}_expects_{contract[side]}")
        pairs, reasons = [], []
        for pair in contract["pairs"]:
            evaluated = {"from_field": pair["from"], "to_field": pair["to"],
                         "compare": pair["compare"]}
            normalized = {}
            for side, view in (("from", from_view), ("to", to_view)):
                raw, problem = _read(view, pair[side])
                if problem is None:
                    evaluated[f"{side}_value"] = _echo(raw)
                    value, problem = _normalize(raw, pair["compare"])
                    normalized[side] = value
                if problem is not None:
                    reasons.append(f"{side}_{problem}:{pair[side]}")
            if len(normalized) == 2 and None not in normalized.values():
                evaluated["equal"] = normalized["from"] == normalized["to"]
            pairs.append(evaluated)
        result = {"contract": {key: contract[key] for key in (
                      "id", "digest", "from", "to", "match", "strength", "provenance",
                      "meaning")},
                  "pairs": pairs}
        if reasons:
            return {**result, "outcome": "unknown", "reason": ",".join(reasons)}
        if all(item["equal"] for item in pairs):
            if contract["provenance"]:
                result["contract"]["provenance_note"] = contract["provenance_note"]
            return {**result, "outcome": contract["strength"]}
        if contract["match"] == "text_match":
            # Two texts that differ prove no relationship either way.
            return {**result, "outcome": "no_match",
                    "reason": "text_differs_no_lead"}
        return {**result, "outcome": "contradicted"}


def _read(view: Mapping[str, Any], path: str) -> tuple[Any, str | None]:
    if "row" not in view or not isinstance(view.get("row"), Mapping):
        return None, "key_not_unique_or_not_a_row"
    column, json_key = _PATH.match(path).groups()
    row = view["row"]
    if column not in row:
        return None, "field_not_in_cited_row"
    value = row[column]
    if json_key is None:
        return (None, "value_absent") if _absent(value) else (value, None)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "value_absent"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, "json_column_unparseable"
    if not isinstance(value, Mapping):
        return None, "json_column_not_object"
    if json_key not in value or _absent(value[json_key]):
        return None, "value_absent"
    return value[json_key], None


def _absent(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _echo(value: Any) -> Any:
    if isinstance(value, (bool, int, str)) and (not isinstance(value, str)
                                                or len(value) <= _MAX_VALUE_CHARS):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    return {"type": type(value).__name__, "omitted": True}


def _normalize(value: Any, compare: str) -> tuple[str | None, str | None]:
    """(normalized text, problem). Never coerces across declared types."""
    if isinstance(value, bool):
        return None, "value_not_" + compare
    if compare == "integer_id":
        if isinstance(value, int):
            return (str(value), None) if value >= 0 else (None, "value_not_integer_id")
        if isinstance(value, float):
            if value.is_integer() and 0 <= value < 2 ** 53:
                return str(int(value)), None
            return None, "value_not_integer_id"
        if isinstance(value, Decimal):  # psycopg numeric
            if value.is_finite() and value == value.to_integral_value() and value >= 0:
                return str(int(value)), None
            return None, "value_not_integer_id"
        if isinstance(value, str) and _INTEGER_TEXT.match(value.strip()):
            try:
                return str(int(Decimal(value.strip()))), None
            except InvalidOperation:  # pragma: no cover - the pattern admits only digits
                return None, "value_not_integer_id"
        return None, "value_not_integer_id"
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or len(value) > _MAX_VALUE_CHARS:
        return None, "value_not_" + compare
    if compare == "text":
        value = value.strip(" ")  # Postgres TRIM: spaces only, never NBSP or tabs
    return value, None


__all__ = ["BUILTIN_PATH", "COMPARES", "LinkContracts", "MATCHES", "OUTCOMES", "PROVENANCES",
           "SCHEMA", "STRENGTH"]
