"""Read-only evaluation of a case playbook against a finished case (milestone M2, component F).

``evaluate(playbook, call, case_id)`` reads one case through the MCP read tools
(``plx.case_get`` paged to completion, ``plx.source_citation`` to re-read cited rows) and
returns a scored, reproducible report: one entry per ``success_check``, one per
``expected_gap`` (did its ``when`` condition hold, was it recorded), the authority
violations, summary counts, the playbook's ``definition_sha256`` and a ``report_sha256``.

Rules this module keeps:

* **Read-only.** ``call`` is wrapped so that any tool other than ``plx.case_get`` and
  ``plx.source_citation`` raises before it reaches the server. Nothing is written, no case
  is opened, no business system is touched.
* **Nothing is taken from the case on trust.** Conditions and arithmetic are recomputed from
  the cited rows, re-read by the evaluator through the same permission-checked citation tool,
  never from the recorded ``value`` of a calculation input or from recorded prose.
* **Conservative.** A cited row that cannot be re-read, a withheld item or a changed citation
  fails the check that needs it; it is never skipped. Ambiguity is reported, not guessed.
* **Reproducible.** The report contains no timestamps or host data; the same playbook and the
  same case content give the same ``report_sha256``.

Every check fails closed: it passes only on positive, complete evidence; unreadable, withheld,
ambiguous or partial evidence fails with a reason. Conditions are tri-state (holds /
does_not_hold / unknown); an unknown condition fails its gap check (``condition_unknown``).

Known limits, reported and never hidden behind ``passed_all``:

* **Row completeness.** The evaluator can only re-read rows the case cites (``plx.source_citation``).
  An arithmetic check over a repeat role (for example the damage rows of an assembly order) proves
  that every cited row is counted exactly once and nothing foreign is added, not that the case cited
  every row of that kind the source holds: a consistent subset of the rows is indistinguishable
  from the whole. Such checks carry ``limits: ["row_completeness_not_enumerated"]``, the report and
  summary list the limits, and ``passed_all_clean`` is false while any limit applies.
* **Units.** An input recorded ``unit_basis: caller_declared`` has a unit nobody verified against
  the source; it is counted in ``summary.unverified_unit_inputs`` (not a failure).
* **Authority.** Without a transcript ``authority_checked`` is false: the case shows recorded
  proposals, not which tools an agent called.
* **Claims.** Every evaluation runs the value-claim gate (``operating.claim_checks``) over
  the re-read rows: structured claims are checked for value, role contract and unit, and
  every number in finding/gap text and the close outcome must be covered by one; any
  failure, or a finding or gap that cannot be checked (withheld, changed), blocks
  ``passed_all`` whatever the playbook's checks say. Prose heuristics are advisory leads.

Identity binding: an evidence role's ``identity`` rule (playbook) ties each cited row to the case's
business subject (case roots, or an id join to a bound row of another role); ``evidence_cited``
fails on any cited row of the role that is unbound, and calculation inputs are taken only from
bound rows. Each expected gap is owed once per bound subject of the roles its ``match.cites``
names (reported as ``subjects`` rows); a recorded gap must cite that subject's row. Every cited api
row is re-read and its payload digest (the case store's own canonicalisation) must equal the digest
recorded at citation time, else the entry is ``reread: changed`` and nothing is scored on it.

Frozen contract: a report records the evaluator version, the case event boundary (event count,
chain_ok and a digest of the whole timeline, because ``plx.case_get`` does not expose the chain
head itself), an evidence manifest (each cited handle, the digest the case recorded, its status,
whether it was re-read, who cites it, identity class) and whether a transcript was supplied.
``compare`` refuses reports whose boundary or manifest differ unless ``allow_changed_case``.

Recorded gaps carry no playbook gap id, so a recorded gap is attributed to an expected gap only
by content: its ``state`` equals the expected gap's ``kind``, its text contains every
``match.terms`` entry and it cites a resolved row of every ``match.cites`` role. A recorded gap
that satisfies more than one expected gap is ambiguous and attributed to none (reported as
``ambiguous_gap_ids``); kind alone never matches. Conditions ``chain_inconsistent``, ``dates_unreadable``
and ``after_required_date`` use the assembly-order roles ``ao``, ``link``, ``line``, ``item``,
``order``, ``filling``; ``po_field_*`` use ``customer_po_coverage.classify_order``.
"""

from __future__ import annotations

import ast
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from integrations.live_source_guard import NotVisible

from .claim_checks import (IDENTIFIER, ISO_DATE, NUMBER, case_items, check_items,
                           returned_evidence)
from .customer_po_coverage import classify_order, parse_date_value
from .cases import _digest as _case_citation_digest, format_unit, parse_unit
from .playbook_originals import (DocumentFields, KindConfirmationsRead, check_case_originals,
                                 page_view, public_original_checks, role_fill)
from .playbook_originals import is_agent_only
from .ontology_tools import shared_schema
from .playbooks import CHECK_TYPES, Playbook, PlaybookRegistry
from .semantic_proposals import ProposalStore
from .source_units import SourceUnitContracts
from .source_reads import READS
from . import capability_guards

REPORT_SCHEMA = "plx-playbook-eval/v1"
EVALUATOR_VERSION = "plx-playbook-evaluator/14"  # 8: claim checks; 9: recorded original
# kinds; 10: Workbench confirmations (operating.kind_confirmation) replace finding confirmations;
# 11: proposals re-checked against their capability guards (operating.capability_guards);
# 12: an unexpected expected gap (recorded, condition does not hold) blocks passed_all;
# a proposal lacking its catalog person_confirms preconditions is a violation;
# 13: absence gaps are structured claims: one without property, or one the gap guard
# refuses on the re-read rows, is a violation; expected gaps declaring property are
# attributed by structure, never wording; an unattributed absence gap is a limit;
# 14: a byte-pinned document citation's fields are read through the evaluating actor's
# document reader (``document_rows``), as plx.act propose reads them
ROW_COMPLETENESS = "row_completeness_not_enumerated"
ABSENCE_UNATTRIBUTED = "absence_gap_unattributed"  # a checked absence no expected gap owns
IDENTITY_NOT_CHECKED = "identity_not_checked"  # a cited row accepted only by an "any" role
ORIGINALS_NOT_IDENTITY_CHECKED = "originals_identity_not_checked"  # audience + re-hash only
UNIT_BASES = ("source_contract", "caller_declared")
READ_TOOLS = frozenset({"plx.case_get", "plx.source_citation", "mirror.rows"})
ENUMERATION_PAGE = 500
ENUMERATION_MAX_PAGES = 200
Call = Callable[[str, dict[str, Any]], dict[str, Any]]

_HANDLE = re.compile(r"^api://([^/]+)/")
# Numbers, identifiers and ISO dates: one definition, shared with ``claim_checks``. Numbers
# take an optional sign (only when not glued to a preceding word/digit, so ``10-20`` is two
# numbers), thousands groups, decimals, leading-dot decimals; ratios and ``4242-pack``
# compounds fall out. Identifiers: 2-8 capitals, optional hyphen, at least four digits, then
# letters/digits, optionally hyphen segments that contain a letter (AO-26005B, OR1000151885,
# PO12172025, DVS-5003-3.78L-A); ``AO-77`` or ``x-1503`` are not identifiers.
_ISO_DATE, _NUMBER, _IDENTIFIER = ISO_DATE, NUMBER, IDENTIFIER
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
          "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000}
_NUMBER_WORDS = re.compile(
    r"\b(?:(?:" + "|".join([*_UNITS, *_TENS, *_SCALES]) + r")(?:[- ](?:and[ ])?|\b))+", re.I)
_HANDLE_STRIP = re.compile(r"api://\S+")
_OK_CITATION = frozenset({"resolved"})


class EvaluationError(RuntimeError):
    """The case could not be read completely or the playbook cannot be evaluated."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


# --------------------------------------------------------------------------- reading

class _ReadOnly:
    """Only the three reads pass; the record of calls lets a test prove it."""

    def __init__(self, call: Call) -> None:
        self._call = call
        self.names: list[str] = []
        self._citations: dict[str, dict[str, Any]] = {}
        self._pins: dict[str, set[Any]] = {}

    def pin(self, handle: str, recorded_digest: Any) -> None:
        """Remember a digest the case recorded for ``handle`` at citation time."""
        self._pins.setdefault(handle, set()).add(recorded_digest)

    def pins(self, handle: str) -> set[Any]:
        """Every digest the case recorded for ``handle`` (empty when it never cited it)."""
        return set(self._pins.get(handle, set()))

    def __call__(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_TOOLS:
            raise EvaluationError(f"evaluator_is_read_only: refused {name}")
        self.names.append(name)
        return self._call(name, arguments)

    def read(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """A read the evaluation needs: a refused one (rate limit, tool error, transport) is
        never an unreadable citation scored as a failed check; the evaluation is incomplete
        (``EvaluationError``), so no report is made and compare() never sees it."""
        try:
            return self(name, arguments)
        except EvaluationError:
            raise
        except Exception as error:
            raise EvaluationError(f"evaluation_incomplete: {name} read refused "
                                  f"({type(error).__name__}: {str(error)[:80]})") from None

    def citation(self, handle: str) -> dict[str, Any]:
        if handle not in self._citations:
            result = self.read("plx.source_citation", {"citation": handle})
            self._citations[handle] = result if isinstance(result, dict) else {"ok": False}
        return self._citations[handle]

    def payload_sha256(self, handle: str) -> str | None:
        """Digest of the exact payload this evaluator re-read, canonicalised exactly as the case
        store digests a citation (``cases._digest`` over the content-stable view that
        ``CaseRuntime.resolve_citation`` builds: citation, relation, key, capture id and the stored
        row). None for anything but a re-read ``api://`` row."""
        result = self.citation(handle)
        capture = result.get("capture")
        if (not handle.startswith("api://") or not result.get("ok")
                or not isinstance(capture, dict) or "capture_id" not in capture
                or "relation" not in result or "key" not in result):
            return None
        view: dict[str, Any] = {"citation": handle, "relation": result["relation"],
                                "key": result["key"], "capture_id": capture["capture_id"]}
        if "row" in result:
            view["row"] = result["row"]
        elif "rows" in result:
            view["rows"] = result["rows"]
        else:
            return None
        return _case_citation_digest(view)

    def reread_status(self, handle: str) -> str:
        """ok, failed, or changed: the re-read payload is not the content the case recorded."""
        result = self.citation(handle)
        if not (result.get("ok") and isinstance(result.get("row"), dict)):
            return "failed"
        if not handle.startswith("api://"):
            return "ok"
        payload = self.payload_sha256(handle)
        pins = self._pins.get(handle, set())
        if payload is None or not pins or any(pin != payload for pin in pins):
            return "changed"  # also unpinned or not reconstructable: never scored on trust
        return "ok"

    def row(self, handle: str) -> dict[str, Any] | None:
        if self.reread_status(handle) != "ok":
            return None  # a row the case did not cite is not a row it can be scored on
        return self.citation(handle)["row"]


_SECTIONS = ("findings", "calculations", "gaps", "proposals", "verifications", "timeline")
_SHAPE_KEYS = ("view", "complete", "counts", "reason", "sections", "paging",
               "header_truncated")


def read_case(call: Call, case_id: str) -> dict[str, Any]:
    """The whole case through ``plx.case_get``: full view, or summary plus every page."""
    view = call("plx.case_get", {"case_id": case_id})
    if view.get("view") == "full":
        if not view.get("complete"):
            raise EvaluationError("incomplete_case_read")
        return {key: value for key, value in view.items() if key not in ("view", "complete")}
    if view.get("view") != "summary":
        raise EvaluationError("unexpected_case_view")
    whole = {key: value for key, value in view.items() if key not in _SHAPE_KEYS}
    if view.get("header_truncated"):
        header = call("plx.case_get", {"case_id": case_id, "section": "header"})
        whole.update({key: value for key, value in header.items()
                      if key not in ("view", "complete")})
    whole["findings"] = {bucket: [] for bucket in view["counts"]["findings"]}
    for section in _SECTIONS:
        items: list[dict[str, Any]] = []
        offset: int | None = 0
        while offset is not None:
            page = call("plx.case_get", {"case_id": case_id, "section": section,
                                         "offset": offset, "limit": 200})
            if page.get("view") != "section" or page.get("section") != section:
                raise EvaluationError("unexpected_case_page")
            items.extend(page["items"])
            offset = page["next_offset"]
        if any(item.get("mirror_status_omitted") or item.get("oversized") for item in items):
            raise EvaluationError("incomplete_case_read")  # a shrunk item is not the case
        if section == "findings":
            for item in items:
                whole["findings"][item.pop("bucket")].append(item)
            if {bucket: len(found) for bucket, found in whole["findings"].items()} \
                    != view["counts"]["findings"]:
                raise EvaluationError("incomplete_case_read")
        else:
            whole[section] = items
            if len(items) != view["counts"][section]:
                raise EvaluationError("incomplete_case_read")
    return whole


# --------------------------------------------------------------------------- helpers

def _relation_of(handle: str) -> str:
    found = _HANDLE.match(handle or "")
    return found.group(1) if found else "original"


def _dig(row: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(row, Mapping) and part in row:
            row = row[part]
        else:
            return None
    return row


def _field_path(field: str) -> str:
    return field[4:] if field.startswith("row.") else field


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _plain(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _scalars(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _scalars(item)
    else:
        yield value


def _number_words(text: str) -> set[str]:
    """Values of spelled-out numbers of ten or more (``seventy-seven``, ``two hundred``)."""
    found: set[str] = set()
    for match in _NUMBER_WORDS.finditer(text):
        total = current = 0
        for word in re.split(r"[- ]+", match.group(0).lower().strip(" -")):
            if word in _UNITS:
                current += _UNITS[word]
            elif word in _TENS:
                current += _TENS[word]
            elif word == "hundred":
                current = max(current, 1) * 100
            elif word in _SCALES:
                total += max(current, 1) * _SCALES[word]
                current = 0
        if total + current >= 10:
            found.add(str(total + current))
    return found


def _numbers_in(text: str) -> set[str]:
    """Normalised numbers of free text; identifiers, handles and ISO dates are removed first."""
    text = _ISO_DATE.sub(" ", _HANDLE_STRIP.sub(" ", text))
    text = _IDENTIFIER.sub(" ", text)
    found = _number_words(text)
    for match in _NUMBER.finditer(text):
        body = (match.group("int") or "0").replace(",", "") + (match.group("frac")
                                                            or match.group("lead") or "")
        number = _decimal(body)
        if number is None:
            continue
        negative = match.group("sign") in ("-", "\u2212") and number != 0
        found.add(_plain(-number if negative else number))
    return found


def _cited_values(row: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """(normalised numbers, ISO dates) present in a cited row's values."""
    numbers: set[str] = set()
    dates: set[str] = set()
    for value in _scalars(row):
        if isinstance(value, bool) or value is None:
            continue
        text = str(value)
        dates.update(_ISO_DATE.findall(text))
        number = _decimal(value)
        if number is not None:
            numbers.add(_plain(number))
        numbers.update(_numbers_in(text))
    return numbers, dates


class _Ops:
    """Decimal evaluator of the playbook expression grammar (``+ - * /``, ``sum(name)``)."""

    def __init__(self, names: Mapping[str, Decimal | list[Decimal]]) -> None:
        self.names = names

    def __call__(self, expression: str) -> Decimal:
        return self._walk(ast.parse(expression.strip(), mode="eval").body)

    def _walk(self, node: ast.AST) -> Decimal:
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult,
                                                                 ast.Div)):
            left, right = self._walk(node.left), self._walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Decimal(str(node.value))
        if isinstance(node, ast.Name) and isinstance(self.names.get(node.id), Decimal):
            return self.names[node.id]  # type: ignore[return-value]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "sum" and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and isinstance(self.names.get(node.args[0].id), list)):
            return sum(self.names[node.args[0].id], Decimal(0))  # type: ignore[arg-type]
        raise ValueError("unsupported_expression")


# --------------------------------------------------------------------------- case facts

class _Case:
    """Indexed view of one case for the evaluator."""

    def __init__(self, view: Mapping[str, Any], reader: _ReadOnly, definition: Mapping[str, Any]):
        self.view, self.reader = view, reader
        self.roles = {item["role"]: item["relation"] for item in definition["required_evidence"]}
        self.identities = {item["role"]: item["identity"]
                           for item in definition["required_evidence"]}
        self.roots = {_norm(root) for root in view.get("roots") or [] if _norm(root)}
        self._bound: dict[str, tuple[list[str], list[str], list[str], dict[str, dict[str, Any]]]] = {}
        self.unit_inputs: dict[tuple[str, str], dict[str, Any]] = {}
        self._enumerations: dict[str, dict[str, Any]] = {}
        buckets = view.get("findings") or {}
        self.counted = [item for bucket in ("supported", "contradicted", "unknown")
                        for item in buckets.get(bucket, [])]
        self.facts = [item for bucket in ("supported", "contradicted")
                      for item in buckets.get(bucket, []) if item.get("kind") == "fact"]
        self.unusable_findings = [item for bucket in ("withheld", "citation_content_changed")
                                  for item in buckets.get(bucket, [])]
        self.calculations = list(view.get("calculations") or [])
        self.gaps = list(view.get("gaps") or [])
        self.proposals = list(view.get("proposals") or [])
        self.verifications = list(view.get("verifications") or [])
        # Original roles bind by verified document kind (operating.playbook_originals), set by
        # ``bind_originals`` before anything reads a role; None binds no original at all.
        self.original_fill: dict[str, set[str]] | None = None
        self.originals_unsure = False
        # Pin every handle to the digest(s) the case recorded at citation time, before any row
        # is re-read for scoring.
        for bucket in buckets.values():
            for item in bucket:
                self._pin(item.get("citations"))
        for item in self.calculations:
            self._pin(self.calc_citations(item) or item.get("citations"))
        for items in (self.gaps, self.proposals, self.verifications):
            for item in items:
                self._pin(item.get("citations"))

    def _pin(self, cites: Iterable[Mapping[str, Any]] | None) -> None:
        for cite in cites or []:
            if cite.get("handle"):
                self.reader.pin(cite["handle"], cite.get("digest"))

    @staticmethod
    def calc_citations(calc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [spec["citation"] for spec in (calc.get("inputs") or {}).values()
                if isinstance(spec, Mapping) and spec.get("kind") == "citation"]

    def evidence_items(self) -> list[tuple[str, str, list[Mapping[str, Any]]]]:
        """(kind, id, citations) of everything that can carry evidence for a role."""
        items = [("finding", f["id"], list(f.get("citations") or [])) for f in self.counted]
        items += [("calculation", c["id"], self.calc_citations(c)) for c in self.calculations]
        items += [("gap", g["id"], list(g.get("citations") or [])) for g in self.gaps
                  if g.get("citations_status") in ("resolved", "uncited")]
        return items

    def bind_originals(self, checks: Mapping[str, Any]) -> None:
        """Bind each ``original`` role to the cited handles of the originals whose content was
        verified as the role's document kind (never every original to every role)."""
        self.original_fill, self.originals_unsure = role_fill(checks)

    def role_handles(self, role: str) -> dict[str, list[str]]:
        """Resolved handles of the role's relation -> ids (F1, C2, G3) citing each. An
        ``original`` role has only the handles its verified document kind binds."""
        relation = self.roles[role]
        allowed = None if relation != "original" else (self.original_fill or {}).get(role, set())
        found: dict[str, list[str]] = {}
        for _kind, ident, cites in self.evidence_items():
            for cite in cites:
                handle = cite.get("handle")
                if handle and cite.get("status") in _OK_CITATION \
                        and _relation_of(handle) == relation \
                        and (allowed is None or handle in allowed):
                    ids = found.setdefault(handle, [])
                    if ident not in ids:
                        ids.append(ident)
        return found

    def role_rows(self, role: str, *, bound: bool = False
                  ) -> tuple[list[dict[str, Any]], list[str]]:
        """(re-read rows of the role, handles that could not be re-read). With ``bound`` only rows
        that match the role's identity rule are rows; unbound handles are listed with the
        unreadable ones, so a condition needing the role's subject is unknown, not decided on
        another subject's row."""
        if bound:
            handles, unbound, unreadable, rows = self.binding(role)
            return [rows[handle] for handle in handles], unreadable + unbound
        rows, unreadable = [], []
        for handle in self.role_handles(role):
            row = self.reader.row(handle)
            (rows if row is not None else unreadable).append(row if row is not None else handle)
        return rows, unreadable  # type: ignore[return-value]

    def binding(self, role: str
                ) -> tuple[list[str], list[str], list[str], dict[str, dict[str, Any]]]:
        """(bound handles, unbound handles, unreadable handles, re-read rows by handle) of the
        role's cited resolved rows under its ``identity`` rule. ``any`` binds every readable row.

        Several roles may declare one relation (an AO's finished-good basis row and its other
        component rows; its Filling and Line clearance tasks): a row another such role binds
        is that role's evidence, not an unbound row of this one (``sibling_bound``)."""
        bound, unbound, unreadable, rows = self._own_binding(role)
        claimed = {handle for other, relation in self.roles.items()
                   if other != role and relation == self.roles[role]
                   for handle in self._own_binding(other)[0]}
        return bound, [handle for handle in unbound if handle not in claimed], unreadable, rows

    def sibling_bound(self, role: str, handle: str) -> bool:
        return any(handle in self._own_binding(other)[0]
                   for other, relation in self.roles.items()
                   if other != role and relation == self.roles[role])

    def _own_binding(self, role: str
                     ) -> tuple[list[str], list[str], list[str], dict[str, dict[str, Any]]]:
        if role in self._bound:
            return self._bound[role]
        rule = self.identities[role]
        rows: dict[str, dict[str, Any]] = {}
        unreadable: list[str] = []
        for handle in sorted(self.role_handles(role)):
            row = self.reader.row(handle)
            if row is None:
                unreadable.append(handle)
            else:
                rows[handle] = row
        self._bound[role] = ([], [], unreadable, rows)  # a cycle cannot bind anything
        bound: list[str] = []
        if rule == "any":
            bound = list(rows)
        elif rule["kind"] == "case_root":
            bound = [handle for handle, row in rows.items()
                     if self._root_match(handle, row, rule["fields"])]
        else:
            targets = self._target_rows(rule["to_role"])[1]
            fixed = rule.get("fixed") or {}
            bound = [handle for handle, row in rows.items()
                     if all(_fixed_same(_dig(row, _field_path(key)), value)
                            for key, value in fixed.items())
                     and any(_same(_dig(row, rule["field"]), _dig(target, rule["to_field"]))
                             for target in targets)]
        unbound = [handle for handle in rows if handle not in bound]
        self._bound[role] = (bound, unbound, unreadable, rows)
        return self._bound[role]

    def _target_rows(self, role: str) -> tuple[list[str], list[dict[str, Any]]]:
        handles, _unbound, _unreadable, rows = self.binding(role)
        return handles, [rows[handle] for handle in handles]

    def _root_match(self, handle: str, row: Mapping[str, Any], fields: list[str]) -> bool:
        if _norm(handle) in self.roots or _norm(handle.rsplit("@", 1)[0]) in self.roots:
            return True
        return any(_norm(_dig(row, field)) in self.roots for field in fields
                   if _norm(_dig(row, field)))

    def bound_handles(self, role: str) -> list[str]:
        return self.binding(role)[0]

    def enumeration(self, role: str) -> dict[str, Any]:
        """Page ``mirror.rows`` (``operating.source_reads``) for every source row the role's
        ``join`` identity selects.
        ``status`` is ``enumerated`` only when every page was read to the end (``next_offset``
        null, nothing truncated, mirror fresh and without a refresh error, counts agreeing and
        every returned row satisfying the filter). Anything else is ``not_enumerated`` with the
        reason, and the caller keeps its limit. Pages must carry one unchanged
        ``freshness.latest_capture_id``. The role's ``fixed`` columns match by exact value both
        here (the ``where`` clause) and in ``binding`` (no case folding or trimming). The mirror is a non-atomic replica read: the
        result is completeness of that read, never a proof against deletion or lag."""
        if role in self._enumerations:
            return self._enumerations[role]
        found = self._enumerate(role)
        self._enumerations[role] = found
        return found

    def _enumerate(self, role: str) -> dict[str, Any]:
        relation, rule = self.roles[role], self.identities[role]
        out: dict[str, Any] = {"role": role, "relation": relation, "status": "not_enumerated",
                               "reason": "", "keys": {}, "pages": 0}
        if rule == "any" or rule["kind"] != "join":
            out["reason"] = "the role's identity is not a join to a bound role"
            return out
        targets = self._target_rows(rule["to_role"])[1]
        if len(targets) != 1:
            out["reason"] = (f"the {rule['to_role']} role has {len(targets)} bound rows, "
                             "not exactly one to join on")
            return out
        value = _dig(targets[0], rule["to_field"])
        if value is None or _norm(value) == "":
            out["reason"] = f"the bound {rule['to_role']} row has no {rule['to_field']} value"
            return out
        fixed = rule.get("fixed") or {}
        column = _field_path(rule["field"])
        # A numeric fixed value ("1") is matched numerically ("1.0", 1.0), which a mirror
        # equality filter cannot do: it is applied to the returned rows instead.
        text_fixed = {key: want for key, want in fixed.items() if _decimal(want) is None}
        numeric_fixed = {key: want for key, want in fixed.items() if key not in text_fixed}
        where = [[column, "eq", value]] + [[_field_path(key), "eq", text_fixed[key]]
                                           for key in sorted(text_fixed)]
        out["where"] = where
        if numeric_fixed:
            out["numeric_filter"] = {key: numeric_fixed[key] for key in sorted(numeric_fixed)}
        seen: set[str] = set()
        out["rows"] = {}
        offset, total, capture = 0, None, None
        for _page in range(ENUMERATION_MAX_PAGES):
            try:
                result = self.reader("mirror.rows", {
                    "relation": relation, "where": where, "limit": ENUMERATION_PAGE,
                    "offset": offset})
            except EvaluationError:
                raise
            except Exception as error:  # a refused or failed page is an incomplete enumeration
                out["reason"] = f"mirror.rows failed: {type(error).__name__}"
                return out
            out["pages"] += 1
            problem = self._page_problem(result, relation, offset, column, value, text_fixed)
            if problem:
                out["reason"] = problem
                return out
            if total is not None and result["total_matched"] != total:
                out["reason"] = "the matched row count changed between pages"
                return out
            marker = result["freshness"].get("latest_capture_id")
            if marker is None or (capture is not None and marker != capture):
                out["reason"] = ("the mirror reports no capture marker" if marker is None else
                                 "the mirror was refreshed between pages (latest_capture_id "
                                 "changed)")
                return out
            capture = marker
            total = result["total_matched"]
            for item in result["rows"]:
                key = item["citation"].rsplit("@", 1)[0]
                seen.add(key)
                if all(_fixed_same(_dig(item["row"], _field_path(name)), want)
                       for name, want in numeric_fixed.items()):
                    out["keys"][key] = item["citation"]
                    out["rows"][key] = item["row"]
            offset = result["next_offset"]
            if offset is None:
                break
        else:
            out["reason"] = "page limit reached before the end of the rows"
            return out
        if len(seen) != total:
            out["reason"] = (f"{len(seen)} distinct keys enumerated for "
                             f"{total} matched rows")
            return out
        out["status"] = "enumerated"
        return out

    @staticmethod
    def _page_problem(result: Any, relation: str, offset: int, column: str, value: Any,
                      fixed: Mapping[str, Any]) -> str:
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            return "mirror.rows returned no rows list"
        if result.get("relation") != relation:
            return "mirror.rows answered for another relation"
        if result.get("complete") is False or result.get("atomic") is False:
            return "mirror.rows reports an incomplete result"
        if not isinstance(result.get("total_matched"), int) or result.get("offset") != offset:
            return "mirror.rows page is not the page asked for"
        following = result.get("next_offset")
        if following is None:
            if offset + len(result["rows"]) != result["total_matched"]:
                return "mirror.rows ended with fewer rows than it matched (truncated)"
        elif not isinstance(following, int) or following <= offset:
            return "mirror.rows next_offset does not advance"
        fresh = result.get("freshness")
        if not isinstance(fresh, Mapping):
            return "mirror.rows reports no freshness"
        if fresh.get("stale") or fresh.get("refresh_error") or fresh.get("last_refresh_failure"):
            return "the mirror is stale or its last refresh failed"
        for item in result["rows"]:
            row = item.get("row") if isinstance(item, Mapping) else None
            if (not isinstance(row, Mapping) or not isinstance(item.get("citation"), str)
                    or _relation_of(item["citation"]) != relation
                    or not _same(_dig(row, column), value)
                    or not all(_fixed_same(_dig(row, _field_path(key)), want)
                               for key, want in fixed.items())):
                return "an enumerated row does not satisfy the filter or has no citation"
        return ""

    def hidden_evidence(self, role: str) -> bool:
        """True when something that could cite the role is withheld or a citation of the role's
        relation is unresolved: absence of a role is then not established."""
        relation = self.roles[role]
        if self.unusable_findings or any(c.get("content") == "withheld"
                                         for c in self.calculations):
            return True
        if any(g.get("citations_status") not in ("resolved", "uncited") for g in self.gaps):
            return True
        for _kind, _ident, cites in self.evidence_items():
            if any(c.get("handle") and _relation_of(c["handle"]) == relation
                   and c.get("status") not in _OK_CITATION for c in cites):
                return True
        return False

    def role_ids(self, role: str) -> list[str]:
        return sorted({ident for ids in self.role_handles(role).values() for ident in ids},
                      key=_id_key)


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip().casefold()


def _id_key(ident: str) -> tuple[str, int]:
    return (ident[:1], int(ident[1:]) if ident[1:].isdigit() else 0)


def _same(first: Any, second: Any) -> bool:
    return first is not None and second is not None and str(first) == str(second)


def _fixed_same(value: Any, want: Any) -> bool:
    """A role's ``fixed`` column match: numerically when both parse as numbers ("1.0", 1.0
    and "1" are one value), else as exact text."""
    left, right = _decimal(value), _decimal(want)
    if left is not None and right is not None:
        return left == right
    return _same(value, want)


# --------------------------------------------------------------------------- conditions

HOLDS, NOT_HOLD, UNKNOWN = "holds", "does_not_hold", "unknown"
Condition = tuple[str, str, list[str]]  # (tri-state, detail, evidence ids)


Subject = tuple[str, str]  # (role, bound handle) one expected-gap instance is about


def _subject_rows(case: _Case, role: str, subject: Subject | None
                  ) -> tuple[list[dict[str, Any]], list[str]]:
    """Bound rows of ``role``; with a subject of that role only the subject's row (unreadable and
    unbound handles of the role still make the condition unknown)."""
    rows, unreadable = case.role_rows(role, bound=True)
    if subject is not None and subject[0] == role:
        bound, _unbound, _unread, by_handle = case.binding(role)
        rows = [by_handle[subject[1]]] if subject[1] in bound else []
    return rows, unreadable


def _condition(case: _Case, when: str, subject: Subject | None = None) -> Condition:
    """Tri-state of an expected-gap ``when`` condition. ``unknown`` whenever a row needed to
    decide it is unreadable, withheld, not cited or otherwise not positively established.
    ``subject`` scopes row conditions on the subject's role to that subject's row."""
    if when == "always":
        return HOLDS, "unconditional", []
    if when.startswith("role_missing:"):
        role = when.split(":", 1)[1]
        ids = case.role_ids(role)
        if ids:
            return NOT_HOLD, f"{len(ids)} cited item(s) for role {role}", ids
        if case.hidden_evidence(role):
            return UNKNOWN, f"role {role}: a citation or item is withheld or unresolved", []
        if case.roles.get(role) == "original" and case.originals_unsure:
            return UNKNOWN, (f"role {role}: a cited original's document kind is not verified, "
                             "so it may be this role's document"), []
        return HOLDS, f"no cited item for role {role}", []
    if when.startswith("field_empty:"):
        _, role, fields = when.split(":", 2)
        rows, unreadable = _subject_rows(case, role, subject)
        ids = case.role_ids(role)
        if unreadable:
            return UNKNOWN, f"cited {role} row not re-readable or not bound to the case subject", ids
        if not rows:
            return UNKNOWN, f"no cited row for role {role}", ids
        names = fields.split(",")
        empty = all(all(_empty(_dig(row, name)) for name in names) for row in rows)
        return (HOLDS if empty else NOT_HOLD,
                f"{fields} {'empty' if empty else 'not all empty'} on cited {role} rows", ids)
    if when in ("chain_inconsistent", "dates_unreadable", "after_required_date"):
        return _ao_condition(case, when)
    if when in ("po_field_empty", "po_field_not_po_like", "source_not_ingested"):
        return _po_condition(case, when, subject)
    raise EvaluationError(f"unsupported_condition: {when}")


def _need_roles(case: _Case, names: Iterable[str]) -> None:
    missing = [name for name in names if name not in case.roles]
    if missing:
        raise EvaluationError("condition_needs_roles: " + ", ".join(missing))


def _ao_chain(case: _Case) -> tuple[str, str, list[str], dict[str, list[dict[str, Any]]]]:
    """(HOLDS when the cited rows form one id chain, NOT_HOLD when they contradict it, UNKNOWN
    when a role is not cited or not re-readable)."""
    roles = ("ao", "link", "line", "item", "order")
    _need_roles(case, roles)
    rows: dict[str, list[dict[str, Any]]] = {}
    ids: list[str] = []
    for role in roles:
        # The anchors (ao, order) must be the case's own rows; the hops (link, line, item) are
        # read raw so that a foreign row yields a contradicted chain, not an unknown one.
        found, unreadable = case.role_rows(role, bound=role in ("ao", "order"))
        ids += case.role_ids(role)
        if unreadable or not found:
            return UNKNOWN, f"role {role} not cited or not re-readable", ids, rows
        rows[role] = found
    ao_ok = all(_same(ao.get("id"), link.get("idassembly"))
                for ao in rows["ao"] for link in rows["link"]) and \
        all(_same(ao.get("id"), item.get("assembly_order_id"))
            for ao in rows["ao"] for item in rows["item"])
    order_ok = ao_ok and all(
        _same(link.get("idoeitem"), line.get("id")) for link in rows["link"]
        for line in rows["line"]) and all(
        _same(line.get("idoe"), order.get("id")) for line in rows["line"]
        for order in rows["order"])
    return (HOLDS if order_ok else NOT_HOLD,
            "cited rows form one id chain" if order_ok
            else "cited rows do not form one id chain", sorted(set(ids), key=_id_key), rows)


def _ao_condition(case: _Case, when: str) -> Condition:
    chain, detail, ids, rows = _ao_chain(case)
    if chain == UNKNOWN:
        return UNKNOWN, detail, ids
    if when == "chain_inconsistent":
        return (NOT_HOLD if chain == HOLDS else HOLDS), detail, ids
    if chain != HOLDS:
        return NOT_HOLD, "chain not consistent; dates are not compared", ids
    _need_roles(case, ("filling",))
    filling, unreadable = case.role_rows("filling", bound=True)
    ids = sorted(set(ids) | set(case.role_ids("filling")), key=_id_key)
    if unreadable or not filling:
        return UNKNOWN, "filling row not cited or not re-readable", ids
    sources = {"required": [row.get("datereq") for row in rows["order"]],
               "manufactured": [row.get("datemanuf") for row in rows["ao"]],
               "filled": [row.get("date_start") for row in filling]}
    days: dict[str, list[date]] = {}
    bad: list[str] = []
    for name, values in sources.items():
        parsed = [parse_date_value(value) for value in values]
        if not parsed or any(value is None for value in parsed):
            bad.append(name)  # readable rows whose date is empty or not a date
        else:
            days[name] = [value.date() if isinstance(value, datetime) else value
                          for value in parsed]  # type: ignore[union-attr]
    if when == "dates_unreadable":
        return (HOLDS, "unreadable: " + ", ".join(sorted(bad)), ids) if bad else (
            NOT_HOLD, "all three dates readable", ids)
    if bad:  # the dates_unreadable gap covers this; lateness cannot be shown from them
        return NOT_HOLD, "dates unreadable: " + ", ".join(sorted(bad)), ids
    late = (max(days["manufactured"]) > min(days["required"])
            or max(days["filled"]) > min(days["required"]))
    return (HOLDS if late else NOT_HOLD,
            "production recorded after the required date" if late
            else "production on or before the required date", ids)


def _po_condition(case: _Case, when: str, subject: Subject | None = None) -> Condition:
    _need_roles(case, ("order",))
    rows, unreadable = _subject_rows(case, "order", subject)
    ids = case.role_ids("order")
    if unreadable:
        return UNKNOWN, "cited order row not re-readable or not bound to the case subject", ids
    if not rows:
        return UNKNOWN, "no cited order row", ids
    statuses = [classify_order(row, list(row))["status"] for row in rows]
    if when == "po_field_empty":
        hit = "absent_in_this_source" in statuses
    elif when == "po_field_not_po_like":
        hit = bool({"po_field_not_po_like", "po_fields_not_inspectable"} & set(statuses))
    else:  # PO documents in mail/SharePoint/BC are not ingested: true for any order without a PO
        hit = any(status != "has_customer_po" for status in statuses)
    return (HOLDS if hit else NOT_HOLD,
            f"cited order statuses: {', '.join(sorted(set(statuses))) or 'none'}", ids)


# --------------------------------------------------------------------------- gap attribution

def _gap_matches(case: _Case, gap: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Match of one recorded gap against one expected gap; kind alone never matches. An
    expected gap that declares ``property`` matches on structure only (the same state, the
    same schema names and the cited roles), never on wording; otherwise its ``match.terms``
    must all appear in the gap's text."""
    if gap.get("state") != item["kind"]:
        return False
    match = item["match"]
    if item.get("property") is not None:
        if (capability_guards.gap_properties(gap.get("property"))
                != capability_guards.gap_properties(item["property"])):
            return False
    else:
        text = f"{gap.get('missing', '')} {gap.get('why', '')}".lower()
        if not all(term.lower() in text for term in match["terms"]):
            return False
    cited = {cite.get("handle") for cite in gap.get("citations") or []
             if cite.get("status") in _OK_CITATION}
    relations = {_relation_of(handle) for handle in cited if handle}
    return all(case.roles[role] in relations for role in match.get("cites", []))


def _gap_subjects(case: _Case, item: Mapping[str, Any]) -> list[Subject]:
    """The bound subjects an expected gap is owed once each: the bound handles of the roles its
    ``match.cites`` names that bind to the case root (all named roles when none does), under the
    evaluator's own identity binding. Empty when the gap names no role or none has a bound row."""
    roles = [role for role in item["match"].get("cites", []) if role in case.roles]
    rooted = [role for role in roles
              if isinstance(case.identities[role], Mapping)
              and case.identities[role]["kind"] == "case_root"]
    return sorted({(role, handle) for role in rooted or roles
                   for handle in case.bound_handles(role)})


def _recorded_cites(case: _Case, gap_id: str, handle: str) -> bool:
    gap = next((g for g in case.gaps if g.get("id") == gap_id), {})
    return any(cite.get("handle") == handle and cite.get("status") in _OK_CITATION
               for cite in gap.get("citations") or [])


def _attribute_gaps(case: _Case, expected: list[dict[str, Any]]
                    ) -> tuple[dict[str, list[str]], list[str]]:
    """expected gap id -> recorded gap ids; and recorded ids that satisfied several expected
    gaps (attributed to none)."""
    matched: dict[str, list[str]] = {item["id"]: [] for item in expected}
    ambiguous: list[str] = []
    for gap in case.gaps:
        if gap.get("citations_status") not in ("resolved", "uncited"):
            continue  # withheld or changed content is not a recorded gap for this reader
        hits = [item["id"] for item in expected if _gap_matches(case, gap, item)]
        if len(hits) == 1:
            matched[hits[0]].append(gap["id"])
        elif len(hits) > 1:
            ambiguous.append(gap["id"])
    return matched, ambiguous


# --------------------------------------------------------------------------- checks

def _result(check: Mapping[str, Any], passed: bool, detail: str,
            evidence: Iterable[str]) -> dict[str, Any]:
    return {"id": check["id"], "type": check["type"], "passed": bool(passed), "detail": detail,
            "evidence": sorted(set(evidence), key=_id_key)}


def _check_every_fact_cites(case: _Case, check: Mapping[str, Any]) -> dict[str, Any]:
    bad = [f["id"] for f in case.facts
           if not f.get("citations")
           or any(c.get("status") not in _OK_CITATION or not c.get("handle")
                  or case.reader.row(c["handle"]) is None for c in f["citations"])]
    bad += [f["id"] for f in case.unusable_findings]
    ids = [f["id"] for f in case.facts]
    if bad:
        return _result(check, False, "facts without resolved, re-readable citations (or "
                       "withheld/changed): " + ", ".join(sorted(set(bad), key=_id_key)), ids)
    return _result(check, True, f"{len(ids)} fact(s), each citing resolved rows", ids)


def _statement_texts(case: _Case) -> list[tuple[str, list[Mapping[str, Any]], list[str]]]:
    items = [(f["id"], f.get("citations") or [], [f.get("statement") or ""])
             for f in case.counted]
    items += [(g["id"], g.get("citations") or [],
               [g.get("missing") or "", g.get("why") or ""]) for g in case.gaps
              if g.get("citations_status") in ("resolved", "uncited")]
    items += [(p["id"], p.get("citations") or [], [json.dumps(p.get("payload"), sort_keys=True)])
              for p in case.proposals if p.get("citations_status") in ("resolved", "uncited")]
    return items


def _check_no_uncited_numbers(case: _Case, check: Mapping[str, Any]) -> dict[str, Any]:
    problems, ids = [], []
    for ident, cites, texts in _statement_texts(case):
        ids.append(ident)
        numbers: set[str] = set()
        dates: set[str] = set()
        unread = False
        for cite in cites:
            row = case.reader.row(cite["handle"]) if cite.get("handle") else None
            if row is None or cite.get("status") not in _OK_CITATION:
                unread = True
                continue
            found_numbers, found_dates = _cited_values(row)
            numbers |= found_numbers
            dates |= found_dates
        text = _HANDLE_STRIP.sub(" ", " ".join(texts))
        stray = sorted(_numbers_in(text) - numbers)
        stray += sorted(set(_ISO_DATE.findall(text)) - dates)
        if stray:
            problems.append(f"{ident}: {', '.join(stray)}" + (" (a cited row was unreadable)"
                                                              if unread else ""))
        elif unread:
            problems.append(f"{ident}: a cited row was unreadable or unresolved")
    problems += [f"{item['id']}: withheld or changed, cannot be checked"
                 for item in case.unusable_findings]
    if problems:
        return _result(check, False, "numbers not found in cited rows or statement not "
                       "checkable: " + "; ".join(problems), ids)
    if not ids:
        return _result(check, False, "no statement recorded; nothing was checked", [])
    return _result(check, True, f"{len(ids)} statement(s); every number and date is in a "
                   "cited row", ids)


def _check_evidence_cited(case: _Case, check: Mapping[str, Any]) -> dict[str, Any]:
    """Passes only when every cited row of the role's relation is re-readable and is bound to the
    case's business subject by the role's ``identity`` rule (``any`` binds every readable row)."""
    role = check["params"]["role"]
    handles = case.role_handles(role)
    bound, unbound, unreadable, _rows = case.binding(role)
    ids = case.role_ids(role)
    if unreadable:
        return _result(check, False, f"role {role} cites row(s) the evaluator cannot re-read: "
                       + ", ".join(unreadable), ids)
    if unbound:
        rule = case.identities[role]
        why = ("the case has no roots to bind to" if rule != "any" and rule["kind"] == "case_root"
               and not case.roots else "identity rule " + json.dumps(rule, sort_keys=True))
        return _result(check, False, f"role {role} cites row(s) not bound to the case subject ("
                       + why + "): " + ", ".join(unbound), ids)
    if not bound:
        return _result(check, False, f"no resolved citation of {case.roles[role]} "
                       f"(role {role})", ids)
    bound_ids = sorted({ident for handle in bound for ident in handles[handle]}, key=_id_key)
    return _result(check, True, f"role {role} cited by {', '.join(bound_ids)}", bound_ids)


def _check_basis_row_agrees(case: _Case, check: Mapping[str, Any]) -> dict[str, Any]:
    """The role's one cited row is the basis by rule: its ``field`` equals ``equals``
    numerically, and every source row the role's identity selects (enumerated, with numeric
    ``fixed`` columns matched numerically) agrees with it on each ``agree`` field. A source
    that cannot be enumerated completely keeps the agreement unproven (``limits``)."""
    params = check["params"]
    role, field, want = params["role"], _field_path(params["field"]), params["equals"]
    agree = [_field_path(name.strip()) for name in params["agree"].split(",") if name.strip()]
    bound, unbound, unreadable, rows = case.binding(role)
    ids = case.role_ids(role)
    if unreadable or unbound:
        return _result(check, False, f"role {role} cites unreadable or unbound row(s): "
                       + ", ".join(unreadable + unbound), ids)
    if len(bound) != 1:
        return _result(check, False, f"role {role} has {len(bound)} bound cited rows, not "
                       "exactly one basis row", ids)
    row = rows[bound[0]]
    if not _fixed_same(_dig(row, field), want):
        return _result(check, False, f"the cited {role} row has {params['field']} "
                       f"{_dig(row, field)!r}, not {want}", ids)
    values = {name: _decimal(_dig(row, name)) for name in agree}
    if any(value is None for value in values.values()):
        return _result(check, False, f"the cited {role} row has an empty or non-numeric "
                       + ", ".join(name for name, value in values.items() if value is None), ids)
    found = case.enumeration(role)
    if found["status"] != "enumerated":
        result = _result(check, True, f"the cited {role} row is the {params['field']} = {want} "
                         "basis; agreement with the other source rows not proven: "
                         + found["reason"], ids)
        result.update(limits=[ROW_COMPLETENESS], row_completeness="not_enumerated")
        return result
    key = bound[0].rsplit("@", 1)[0]
    if key not in found["rows"]:
        return _result(check, False, f"the cited {role} row is not among the enumerated "
                       "source rows", ids)
    disagree = sorted(found["keys"][other] for other, source in found["rows"].items()
                      if any(_decimal(_dig(source, name)) != value
                             for name, value in values.items()))
    if disagree:
        return _result(check, False, f"source rows with {params['field']} = {want} disagree "
                       f"with the cited basis on {params['agree']}: " + ", ".join(disagree), ids)
    result = _result(check, True, f"the cited {role} row is the {params['field']} = {want} "
                     f"basis and all {len(found['rows'])} such source row(s) agree on "
                     f"{params['agree']}", ids)
    result.update(row_completeness="enumerated", rows_enumerated=len(found["rows"]),
                  row_completeness_basis="non_atomic_mirror_replica_read")
    return result


def _check_component_sums(case: _Case, check: Mapping[str, Any]) -> dict[str, Any]:
    """Rows of ``role`` are summed per ``group`` value (one component), never across groups.

    Every calculation that cites ``field`` of a ``role`` row must cite only such rows, all
    bound, all of one group, each once, in a unit no other group's calculation uses, and its
    recorded result and expression must equal the plain sum of the re-read values (checked on
    the rows and under perturbed values). Every bound cited row is counted exactly once, and
    every source row of the role (enumerated) is cited, else ``limits``."""
    params = check["params"]
    role, field, group = params["role"], _field_path(params["field"]), _field_path(params["group"])
    relation = case.roles[role]
    bound, unbound, unreadable, rows = case.binding(role)
    if unreadable or unbound:
        return _result(check, False, f"role {role} cites unreadable or unbound row(s): "
                       + ", ".join(unreadable + unbound), case.role_ids(role))
    problems: list[str] = []
    covered: dict[str, str] = {}
    units: dict[str, str] = {}
    ids: list[str] = []
    for calc in case.calculations:
        inputs = calc.get("inputs") or {}
        touches = [name for name, item in inputs.items()
                   if isinstance(item, Mapping) and item.get("kind") == "citation"
                   and _relation_of((item.get("citation") or {}).get("handle") or "") == relation
                   and _field_path(item.get("field", "")) == field]
        if not touches:
            continue
        ids.append(calc["id"])
        if set(touches) != set(inputs):
            problems.append(f"{calc['id']}: adds inputs that are not {role} {params['field']}")
            continue
        handles = {name: inputs[name]["citation"]["handle"] for name in touches}
        if any(handle not in bound for handle in handles.values()):
            problems.append(f"{calc['id']}: cites a {role} row that is not bound")
            continue
        if len(set(handles.values())) != len(handles):
            problems.append(f"{calc['id']}: counts a row twice")
            continue
        groups = {_norm(_dig(rows[handle], group)) for handle in handles.values()}
        if len(groups) != 1 or "" in groups:
            problems.append(f"{calc['id']}: sums rows of {len(groups)} different "
                            f"{params['group']} values (or an empty one)")
            continue
        [key] = groups
        unit = _unit_key(calc.get("unit"))
        if key in covered:
            problems.append(f"{calc['id']}: {params['group']} {key} is already summed by "
                            f"{covered[key]}")
            continue
        if unit in units.values():
            problems.append(f"{calc['id']}: unit {calc.get('unit')} is used by another group")
            continue
        covered[key], units[key] = calc["id"], unit
        values = {name: _decimal(_dig(rows[handle], field)) for name, handle in handles.items()}
        if any(value is None for value in values.values()):
            problems.append(f"{calc['id']}: a cited row cannot be re-read as a number")
            continue
        try:
            recorded = _Ops(values)(calc.get("expression") or "")
            perturbed_ok = all(
                _Ops({name: _perturbed(round_no, handle, field)
                      for name, handle in handles.items()})(calc.get("expression") or "")
                == sum((_perturbed(round_no, handle, field) for handle in handles.values()),
                       Decimal(0))
                for round_no in range(1, _PERTURBATION_ROUNDS + 1))
        except (ValueError, ArithmeticError, SyntaxError):
            problems.append(f"{calc['id']}: expression cannot be evaluated")
            continue
        if (_decimal(calc.get("result")) != sum(values.values(), Decimal(0))
                or recorded != _decimal(calc.get("result")) or not perturbed_ok):
            problems.append(f"{calc['id']}: not the plain sum of its cited rows")
    counted = {inputs["citation"]["handle"] for calc in case.calculations
               if calc["id"] in covered.values()
               for inputs in (calc.get("inputs") or {}).values()}
    missing = sorted(handle for handle in bound if handle not in counted)
    if not ids:
        return _result(check, False, f"no calculation sums {role} rows per {params['group']}",
                       [])
    if problems or missing:
        return _result(check, False, "; ".join(problems + (
            [f"cited {role} rows never counted: " + ", ".join(missing)] if missing else [])),
            ids)
    result = _result(check, True, f"{len(covered)} {params['group']} group(s), each summed "
                     "alone in its own unit", ids)
    found = case.enumeration(role)
    if found["status"] != "enumerated":
        result.update(limits=[ROW_COMPLETENESS], row_completeness="not_enumerated",
                      row_completeness_reason=found["reason"])
        return result
    keys = {handle.rsplit("@", 1)[0] for handle in bound}
    absent = sorted(found["keys"][key] for key in set(found["keys"]) - keys)
    if absent:
        result.update(passed=False, detail="source rows not counted in any group: "
                      + ", ".join(absent), limits=[ROW_COMPLETENESS],
                      row_completeness="not_proven_rows_missing")
        return result
    result.update(row_completeness="enumerated", rows_enumerated=len(found["keys"]),
                  row_completeness_basis="non_atomic_mirror_replica_read")
    return result


def _check_gap_recorded(case: _Case, check: Mapping[str, Any],
                        gap_rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    row = gap_rows[check["params"]["gap"]]
    if row["condition_state"] == UNKNOWN:
        return _result(check, False, f"condition_unknown: {row['condition_detail']}",
                       row["condition_evidence"])
    if row["condition_state"] == NOT_HOLD:
        return _result(check, True, f"not applicable: {row['condition_detail']}", [])
    missing = [item["subject"] or "case" for item in row["subjects"]
               if item["condition_state"] == HOLDS and not item["recorded"]]
    if not missing:
        return _result(check, True, "condition held and gap recorded", row["recorded_gap_ids"])
    return _result(check, False, f"condition held ({row['condition_detail']}) but no matching "
                   f"{row['kind']} gap was recorded for: {', '.join(missing)}", [])


def _check_proposals_recorded_only(case: _Case, check: Mapping[str, Any]) -> dict[str, Any]:
    bad = [violation["id"] for violation in _proposal_violations(case)]
    withheld = [item["id"] for item in case.proposals + case.verifications
                if item.get("content") == "withheld"]
    ids = [p["id"] for p in case.proposals] + [v["id"] for v in case.verifications]
    if bad or withheld:
        return _result(check, False, "dispatched/approved or unreadable: "
                       + ", ".join(sorted(set(bad + withheld), key=_id_key)), ids)
    return _result(check, True, f"{len(case.proposals)} proposal(s) recorded only", ids)


def _check_proposals_cite_support(case: _Case, check: Mapping[str, Any]) -> dict[str, Any]:
    supported = {f["id"]: f for f in case.counted
                 if f.get("status") == "supported" and f.get("kind") == "fact"}
    bad, ids = [], []
    for proposal in case.proposals:
        ids.append(proposal["id"])
        cites = proposal.get("citations") or []
        finding = supported.get(proposal.get("evidence_finding_id") or "")
        handles = {c.get("handle") for c in cites}
        ok = (proposal.get("citations_status") == "resolved" and bool(handles)
              and all(c.get("status") in _OK_CITATION for c in cites)
              and finding is not None
              and handles <= {c.get("handle") for c in finding.get("citations") or []})
        if not ok:
            bad.append(proposal["id"])
        elif finding is not None:
            ids.append(finding["id"])
    if bad:
        return _result(check, False, "proposal without a supported cited evidence finding "
                       "covering its citations: " + ", ".join(bad), ids)
    return _result(check, True, f"{len(case.proposals)} proposal(s), each backed by a "
                   "supported finding", ids)


def _computation_inputs(case: _Case, definition: Mapping[str, Any], name: str
                        ) -> dict[tuple[str, str], str]:
    spec = next(item for item in definition["required_computations"] if item["name"] == name)
    pairs: dict[tuple[str, str], str] = {}
    for input_name, item in spec["inputs"].items():
        pair = (case.roles[item["role"]], _field_path(item["field"]))
        if pair in pairs:
            raise EvaluationError(f"ambiguous_computation_inputs: {name}")
        pairs[pair] = input_name
    return pairs


def _bind(calc: Mapping[str, Any], pairs: Mapping[tuple[str, str], str]
          ) -> dict[str, tuple[str, str]] | None:
    """calc input name -> (playbook input name, cited handle); None when any input is not one of
    the playbook's (relation, field) inputs."""
    inputs = calc.get("inputs") or {}
    binding: dict[str, tuple[str, str]] = {}
    for input_name, item in inputs.items():
        if not isinstance(item, Mapping) or item.get("kind") != "citation":
            return None
        handle = (item.get("citation") or {}).get("handle")
        pair = (_relation_of(handle), _field_path(item.get("field", ""))) if handle else None
        if pair not in pairs:
            return None
        binding[input_name] = (pairs[pair], handle)
    return binding or None


def _cited_pairs(calc: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {(_relation_of(handle), _field_path(item.get("field", "")))
            for item in (calc.get("inputs") or {}).values()
            if isinstance(item, Mapping) and item.get("kind") == "citation"
            for handle in [(item.get("citation") or {}).get("handle")] if handle}


def _unit_key(unit: Any) -> str:
    """Canonical text of a unit via the case unit algebra; lower-cased text if unparseable."""
    try:
        return format_unit(parse_unit(unit)).lower()
    except Exception:
        return str(unit or "").strip().lower()


def _in_declared_unit(calc: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    """Whether ``calc`` is in this computation's result unit.

    Cases record no calculation name, so within a declared computation's unit any
    calculation that is not an exact binding of some declared computation cannot be
    told apart from a fudged version of it: it fails every declared computation with
    that unit (a closed world per unit), whatever mix of literals and cited rows it
    has. Units compare canonically ('EA*pack/pack' is EA); aliases such as 'pcs' for
    EA are different units to this rule.
    """
    return _unit_key(calc.get("unit")) == _unit_key(spec.get("unit"))


def _exact_binding(calc: Mapping[str, Any], pairs: Mapping[tuple[str, str], str],
                   spec: Mapping[str, Any]) -> dict[str, tuple[str, str]] | None:
    """The binding when every input of ``calc`` is a cited input of ``spec`` and every input
    of ``spec`` is bound; None otherwise."""
    binding = _bind(calc, pairs)
    if binding is None or {bound for bound, _h in binding.values()} != set(spec["inputs"]):
        return None
    return binding


def _exact_elsewhere(case: _Case, definition: Mapping[str, Any], calc: Mapping[str, Any],
                     name: str) -> str | None:
    """Name of another declared computation ``calc`` binds exactly, if any."""
    for other in definition["required_computations"]:
        if other["name"] == name:
            continue
        if _exact_binding(calc, _computation_inputs(case, definition, other["name"]),
                          other) is not None:
            return other["name"]
    return None


def _structure(case: _Case, spec: Mapping[str, Any], binding: Mapping[str, tuple[str, str]],
               role_handles: Mapping[str, list[str]]) -> str:
    """Empty when the calculation binds the playbook inputs exactly: each non-repeat input once,
    each repeat input once per distinct row of its role the case cites, never twice."""
    for name, item in spec["inputs"].items():
        handles = [handle for bound, handle in binding.values() if bound == name]
        known = role_handles[name]
        if not item["repeat"]:
            if len(handles) != 1:
                return f"input {name} is bound {len(handles)} time(s), not once"
            if len(known) != 1:
                return (f"input {name}: the case cites {len(known)} {item['role']} rows, so a "
                        "single-row binding is ambiguous")
            if handles[0] != known[0]:
                return f"input {name} cites a row that is not the case's {item['role']} row"
            continue
        duplicated = sorted({h for h in handles if handles.count(h) > 1})
        if duplicated:
            return f"input {name} binds the same row more than once: {', '.join(duplicated)}"
        missing = [h for h in known if h not in handles]
        extra = [h for h in handles if h not in known]
        if missing or extra:
            return (f"input {name} binds {len(handles)} of the {len(known)} {item['role']} rows "
                    "the case cites" + (f" (missing {', '.join(missing)})" if missing else "")
                    + (f" (not among the cited rows: {', '.join(extra)})" if extra else ""))
    return ""


def _check_calculation(case: _Case, check: Mapping[str, Any],
                       definition: Mapping[str, Any]) -> dict[str, Any]:
    name = check["params"]["calculation"]
    spec = next(item for item in definition["required_computations"] if item["name"] == name)
    pairs = _computation_inputs(case, definition, name)
    role_handles = {key: sorted(case.bound_handles(item["role"]))
                    for key, item in spec["inputs"].items()}
    withheld = [calc["id"] for calc in case.calculations if calc.get("content") == "withheld"]
    matches, problems = [], []
    for calc in case.calculations:
        if calc.get("content") == "withheld":
            continue
        # Attribution, not skip rules: a calculation that touches any input of this
        # computation must be an exact binding of it (then it is checked) or an exact
        # binding of another declared computation (then it is that one's); anything
        # else - an added literal, a foreign row, a partial cover - is a problem here.
        cited = _cited_pairs(calc)
        if not cited & set(pairs) and not _in_declared_unit(calc, spec):
            continue
        binding = _exact_binding(calc, pairs, spec)
        if binding is None:
            other = _exact_elsewhere(case, definition, calc, name)
            if other is not None:
                continue
            problems.append(f"{calc['id']}: touches the inputs or unit of {name} but is not an "
                            "exact binding of it or of any other declared computation (an added "
                            "literal, a foreign cited row or a partial cover)")
            matches.append(calc)
            continue
        problem = _structure(case, spec, binding, role_handles) or _recompute(
            case, spec, calc, binding, role_handles)
        matches.append(calc)
        if problem:
            problems.append(f"{calc['id']}: {problem}")
    ids = [calc["id"] for calc in matches]
    if not matches and not problems:
        detail = ("no recorded calculation over the playbook's cited inputs"
                  + (f" (withheld: {withheld[0]})" if withheld else ""))
        return _result(check, False, detail, [])
    if problems:
        return _with_limits(_result(check, False, "; ".join(problems), ids), spec)
    if withheld:
        return _result(check, False, "a calculation is withheld from this reader: "
                       + ", ".join(withheld), ids)
    result = _result(check, True, f"{name} = {matches[-1]['result']} {spec['unit']} matches "
                     "the arithmetic on the cited rows", ids)
    basis = [case.unit_inputs[key]["unit_basis"] for key in sorted(case.unit_inputs)
             if case.unit_inputs[key]["calculation"] in ids]
    result["unit_basis"] = {key: basis.count(key) for key in (*UNIT_BASES, "missing")
                            if basis.count(key)}
    if any(item["repeat"] for item in spec["inputs"].values()):
        return _row_completeness(case, spec, result, role_handles)
    return result


def _row_completeness(case: _Case, spec: Mapping[str, Any], result: dict[str, Any],
                      role_handles: Mapping[str, list[str]]) -> dict[str, Any]:
    """Prove a repeat input's rows complete by enumerating the source, or keep the limit.
    A source row the calculation does not bind fails the check (a row missing from the sum)."""
    reasons, counted, missing = [], 0, []
    for name, item in spec["inputs"].items():
        if not item["repeat"]:
            continue
        found = case.enumeration(item["role"])
        if found["status"] != "enumerated":
            reasons.append(f"{name}: {found['reason']}")
            continue
        bound = {handle.rsplit("@", 1)[0] for handle in role_handles[name]}
        absent = sorted(set(found["keys"]) - bound)
        if absent:
            missing += [f"{name}: {found['keys'][key]}" for key in absent]
        elif bound - set(found["keys"]):
            reasons.append(f"{name}: a bound cited row is not among the enumerated source rows")
        else:
            counted += len(found["keys"])
    if missing:
        result["passed"] = False
        result["detail"] = ("the source has rows the calculation does not bind (missing from "
                            "the sum): " + "; ".join(missing))
        result["limits"] = [ROW_COMPLETENESS]
        result["row_completeness"] = "not_proven_rows_missing"
    elif reasons:
        result["limits"] = [ROW_COMPLETENESS]
        result["row_completeness"] = "not_enumerated"
        result["row_completeness_reason"] = "; ".join(reasons)
    else:
        result["row_completeness"] = "enumerated"
        result["rows_enumerated"] = counted
        result["row_completeness_basis"] = "non_atomic_mirror_replica_read"
    return result


def _with_limits(result: dict[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    if any(item["repeat"] for item in spec["inputs"].values()):
        result["limits"] = [ROW_COMPLETENESS]
    return result


def _recompute(case: _Case, spec: Mapping[str, Any], calc: Mapping[str, Any],
               binding: Mapping[str, tuple[str, str]],
               role_handles: Mapping[str, list[str]]) -> str:
    """Empty when the recorded calculation equals the playbook arithmetic recomputed from the
    evaluator's own re-read of every row of each input's role."""
    names: dict[str, Decimal | list[Decimal]] = {}
    for key, item in spec["inputs"].items():
        values: list[Decimal] = []
        for handle in role_handles[key]:
            row = case.reader.row(handle)
            value = _decimal(_dig(row, _field_path(item["field"]))) if row is not None else None
            if value is None:
                return f"{item['role']} row {handle} cannot be re-read as a number"
            values.append(value)
        names[key] = values if item["repeat"] else values[0]
    recorded_names: dict[str, Decimal] = {}
    for input_name, item in calc["inputs"].items():
        case.unit_inputs[(calc["id"], input_name)] = {
            "calculation": calc["id"], "input": input_name, "unit": item.get("unit"),
            "unit_basis": item.get("unit_basis") if item.get("unit_basis") in UNIT_BASES
            else "missing"}
    for input_name, item in calc["inputs"].items():
        bound, handle = binding[input_name]
        basis = item.get("unit_basis")
        if basis not in UNIT_BASES:
            return f"input {input_name} records no valid unit_basis (source_contract or caller_declared)"
        if basis == "source_contract" and not (item.get("unit_contract")
                                               and item.get("unit_contract_digest")):
            return f"input {input_name} claims a source unit contract but records none"
        if item["citation"].get("status") not in _OK_CITATION:
            return f"input {input_name} citation is {item['citation'].get('status')}"
        row = case.reader.row(handle)
        value = _decimal(_dig(row, _field_path(item["field"]))) if row is not None else None
        if value is None:
            return f"input {input_name} cannot be re-read as a number"
        if str(item.get("unit", "")).lower() != spec["inputs"][bound]["unit"].lower():
            return f"input {input_name} unit {item.get('unit')} != {spec['inputs'][bound]['unit']}"
        recorded = _decimal(item.get("value"))
        if recorded is None or recorded != value:
            return f"input {input_name} recorded {item.get('value')} but the cited row holds {value}"
        recorded_names[input_name] = value
    if str(calc.get("unit", "")).lower() != spec["unit"].lower():
        return f"unit {calc.get('unit')} != {spec['unit']}"
    try:
        expected = _Ops(names)(spec["expression"])
        recorded_result = _decimal(calc.get("result"))
        replay = _Ops(recorded_names)(calc.get("expression") or "")
    except (ValueError, ArithmeticError, SyntaxError):
        return "expression cannot be evaluated"
    if recorded_result is None:
        return "recorded result is not a number"
    tolerance = abs(expected) * Decimal("1e-9") if calc.get("inexact") else Decimal(0)
    if abs(recorded_result - expected) > tolerance:
        return (f"recorded result {calc['result']} but the playbook arithmetic on the cited "
                f"rows gives {_plain(expected)}")
    if abs(replay - recorded_result) > tolerance:
        return (f"recorded expression '{calc.get('expression')}' on the cited rows gives "
                f"{_plain(replay)}, not the recorded {calc['result']}")
    return _declared_expression_problem(spec, calc, binding, role_handles)


_PERTURBATION_ROUNDS = 5


def _perturbed(round_no: int, handle: str, field: str) -> Decimal:
    """An independent deterministic value per (round, cited row, field), in 2..999983."""
    seed = hashlib.sha256(f"{round_no}\x00{handle}\x00{field}".encode()).digest()
    return Decimal(2 + int.from_bytes(seed[:8], "big") % 999982)


def _declared_expression_problem(spec: Mapping[str, Any], calc: Mapping[str, Any],
                                 binding: Mapping[str, tuple[str, str]],
                                 role_handles: Mapping[str, list[str]]) -> str:
    """Empty when the recorded expression is the declared one up to renaming of inputs.

    Chosen method: deterministic perturbation, not AST equality. The declared expression may
    use ``sum(name)`` over a repeat input while a case records ``d0 + d1 + d2``; an AST
    comparison would reject that legitimate binding. Each cited (row, field) - not each row,
    since two inputs can be two fields of one row - gets an independent pseudo-random value
    per round (the same value under the declared input name and under the recorded name that
    cites it), and both expressions must agree in every round, so a hardcoded or reweighted
    expression that merely hits the real result is caught.
    """
    field_of = {key: item["field"] for key, item in spec["inputs"].items()}
    for round_no in range(1, _PERTURBATION_ROUNDS + 1):
        declared_names: dict[str, Decimal | list[Decimal]] = {}
        for key, item in spec["inputs"].items():
            values = [_perturbed(round_no, h, field_of[key]) for h in role_handles[key]]
            declared_names[key] = values if item["repeat"] else values[0]
        recorded_names = {name: _perturbed(round_no, handle, field_of[bound])
                          for name, (bound, handle) in binding.items()}
        try:
            declared = _Ops(declared_names)(spec["expression"])
            recorded = _Ops(recorded_names)(calc.get("expression") or "")
        except (ValueError, ArithmeticError, SyntaxError):
            return "expression cannot be evaluated"
        tolerance = max(abs(declared) * Decimal("1e-20"), Decimal("1e-30"))
        if abs(declared - recorded) > tolerance:
            return (f"recorded expression '{calc.get('expression')}' is not the declared "
                    f"expression '{spec['expression']}' up to renaming of inputs "
                    "(differs when the cited values change)")
    return ""


# --------------------------------------------------------------------------- authority

def _proposal_violations(case: _Case) -> list[dict[str, str]]:
    out = []
    for kind, items in (("proposal", case.proposals), ("verification", case.verifications)):
        for item in items:
            payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
            detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else {}
            flagged = [key for key in ("dispatched", "approved")
                       if payload.get(key) is True or item.get(key) is True
                       or detail.get(key) is True]
            status = str(item.get("status") or "").lower()
            if kind == "proposal" and status and not status.startswith("proposed"):
                flagged.append("status:" + status[:40])
            out += [{"id": item["id"], "kind": kind + "_marked", "detail": key}
                    for key in flagged]
    return out


def _guard_reader(case: _Case, documents: capability_guards.Read | None
                  ) -> Callable[[str], dict[str, Any] | None]:
    """``capability_guards`` read over this evaluator's re-reads: an ``api://`` row, or a
    byte-pinned document citation (``DOCUMENT_SCHEMES``) the case cites through
    ``documents``, the evaluating actor's ``capability_guards.current_reader`` (its one
    documentFields reader; None: no document is read)."""
    def read(handle: str) -> dict[str, Any] | None:
        if handle.startswith(capability_guards.DOCUMENT_SCHEMES):
            return (documents(handle) if documents is not None and case.reader.pins(handle)
                    else None)
        row = case.reader.row(handle) if handle.startswith("api://") else None
        if row is None:
            return None
        cited = case.reader.citation(handle)
        return {"relation": cited.get("relation"), "key": cited.get("key"), "row": row}
    return read


def _absence_violations(case: _Case, documents: capability_guards.Read | None
                        ) -> list[dict[str, str]]:
    """Every recorded absence gap (``capability_guards.ABSENCE_STATES``) re-checked as a
    structured claim, never by its wording: it must name what is absent (``property``)
    and the gap guard (``capability_guards.check_gap``) must hold on the cited rows as
    this evaluator re-reads them, on every field the schema binds that property to. An
    unstructured or contradicted absence is a wrong answer, so it never passes."""
    read = _guard_reader(case, documents)
    out = []
    for gap in case.gaps:
        if (gap.get("citations_status") not in ("resolved", "uncited")
                or gap.get("state") not in capability_guards.ABSENCE_STATES):
            continue
        if not gap.get("property"):
            out.append({"id": gap["id"], "kind": "absence_gap_unstructured",
                        "detail": "no property names what is absent"})
            continue
        verdict = capability_guards.check_gap(
            gap["state"], gap["property"],
            [cite.get("handle") for cite in gap.get("citations") or ()
             if isinstance(cite.get("handle"), str)], read, shared_schema())
        if not verdict["passed"]:
            out.append({"id": gap["id"], "kind": "absence_gap_contradicted",
                        "detail": ",".join(f["id"] for f in verdict["failures"])[:80]})
    return out


def _guard_violations(case: _Case, documents: capability_guards.Read | None
                     ) -> list[dict[str, str]]:
    """Every proposal's capability guards (operating/action_catalog.json) re-checked on
    the cited rows as this evaluator re-read them: an undeclared, non-canonical or
    test-host-only capability, or a guard that does not hold, is a violation, so such a
    proposal never passes."""
    read = _guard_reader(case, documents)
    out = []
    for item in case.proposals:
        handles = [cite.get("handle") for cite in item.get("citations") or ()
                   if isinstance(cite.get("handle"), str)]
        if (capability_guards.declared(item.get("capability"))
                and not capability_guards.available(item.get("capability"))):
            # A test-host-only capability (fixture.*, test.*) is never a production
            # proposal, whatever its guards say.
            out.append({"id": item["id"], "kind": "proposal_capability_test_only",
                        "detail": str(item.get("capability"))[:80]})
            continue
        missing = [text for text in capability_guards.person_preconditions(
            item.get("capability")) if text not in (item.get("preconditions") or ())]
        if missing:
            # A condition the catalog says a person must confirm (e.g. the evidence
            # belongs to this row) is missing from the record: never a clean proposal.
            out.append({"id": item["id"], "kind": "person_precondition_missing",
                        "detail": missing[0][:80]})
        verdict = capability_guards.check(item.get("capability"), item.get("payload") or {},
                                          item.get("expected_effect"), handles, read)
        if not verdict["passed"]:
            out.append({"id": item["id"], "kind": ("capability_guard_failed"
                                                   if verdict["guarded"]
                                                   else "proposal_capability_undeclared"),
                        "detail": ",".join(f["id"] for f in verdict["failures"])[:80]})
    return out


def _transcript_violations(allowed: Iterable[str], transcript: Iterable[Any] | None
                           ) -> list[dict[str, str]]:
    if transcript is None:
        return []
    # Direct L1 reads of an in-process driver (operating.source_reads) and the host-code
    # citation read (mcp_server.HOST_READS) are not host tools: read-only by construction,
    # so the tool allowlist does not govern them.
    from .mcp_server import HOST_READS
    permitted, out = set(allowed) | READS | HOST_READS, []
    for index, entry in enumerate(transcript):
        name = entry if isinstance(entry, str) else (
            entry.get("tool") or entry.get("name") if isinstance(entry, Mapping) else None)
        if not isinstance(name, str) or name not in permitted:
            out.append({"id": f"T{index + 1}", "kind": "tool_outside_allowed_tools",
                        "detail": str(name)[:80]})
        elif isinstance(entry, Mapping) and entry.get("business_system_writes") not in (None, 0):
            out.append({"id": f"T{index + 1}", "kind": "business_system_write",
                        "detail": name})
    return out


# --------------------------------------------------------------------------- entry points

def _case_boundary(view: Mapping[str, Any]) -> dict[str, Any]:
    """The event boundary and subject roots of the evaluated case. ``plx.case_get`` exposes the event count and
    whether the hash chain verifies, not the chain head itself, so the head is pinned by a digest of
    the complete timeline (seq, at, actor, kind, ref of every event)."""
    integrity, timeline = view.get("integrity"), view.get("timeline")
    if not isinstance(integrity, Mapping) or not isinstance(timeline, list):
        raise EvaluationError("case_integrity_missing")
    last = timeline[-1] if timeline else {}
    # The case's subject boundary: its roots as recorded when it was opened (normalised). Two
    # investigations of one transaction share roots, which is how adoption counts cases.
    roots = sorted({_norm(root) for root in view.get("roots") or [] if _norm(root)})
    return {"events": integrity.get("events"), "chain_ok": integrity.get("chain_ok") is True,
            "roots": roots,
            "timeline_entries": len(timeline),
            "last_event": {key: last.get(key) for key in ("seq", "kind", "at")},
            "timeline_sha256": hashlib.sha256(_canonical(timeline)).hexdigest()}


def _citations(case: _Case) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """({handle: entry} of every citation the case recorded, the citations shown without a
    handle). Withheld items carry no handle (by design)."""
    view, entries, unresolved = case.view, {}, []

    def visit(owner: str, cites: Iterable[Mapping[str, Any]]) -> None:
        for index, cite in enumerate(cites or []):
            handle = cite.get("handle")
            if not handle:
                unresolved.append({"cited_by": owner, "index": cite.get("index", index),
                                   "status": cite.get("status")})
                continue
            entry = entries.setdefault(handle, {
                "handle": handle, "recorded_digest": cite.get("digest"),
                "current_digest": cite.get("current_digest"), "status": cite.get("status"),
                "cited_by": []})
            if owner not in entry["cited_by"]:
                entry["cited_by"].append(owner)
    for bucket in (view.get("findings") or {}).values():
        for item in bucket:
            visit(item["id"], item.get("citations"))
    for item in case.calculations:
        visit(item["id"], case.calc_citations(item) or item.get("citations"))
    for section in ("gaps", "proposals", "verifications"):
        for item in view.get(section) or []:
            visit(item["id"], item.get("citations"))
    return entries, unresolved


def _manifest(case: _Case) -> dict[str, Any]:
    """Every citation the case recorded: handle, the digest recorded at citation time, its status
    now, whether the evaluator could re-read it, who cites it and how each role's identity rule
    classed it. Withheld items carry no handle (by design) and are listed without one."""
    entries, unresolved = _citations(case)
    for handle, entry in entries.items():
        entry["cited_by"].sort(key=_id_key)
        entry["reread"] = case.reader.reread_status(handle)
        entry["reread_payload_sha256"] = case.reader.payload_sha256(handle)
        entry["identity"] = {}
        for role in sorted(case.roles):
            if case.roles[role] == "original" and _relation_of(handle) == "original":
                entry["identity"][role] = ("verified_kind" if handle in case.role_handles(role)
                                           else "not_this_kind")
            elif _relation_of(handle) == case.roles[role]:
                bound, unbound, _unreadable, rows = case.binding(role)
                state = ("bound" if handle in bound else
                         "unbound" if handle in unbound else
                         "bound_by_sibling_role" if handle in rows else "unreadable")
                if state == "bound" and case.identities[role] == "any":
                    state = "any"  # accepted without an identity rule: never a verified join
                entry["identity"][role] = state
    unresolved.sort(key=lambda item: (_id_key(item["cited_by"]), item["index"]))
    # Enumeration reads are listed as enumerated, never as cited: nothing here is evidence the
    # case recorded, and no enumerated row can bind a role by being listed.
    enumerated = [{"role": found["role"], "relation": found["relation"],
                   "status": found["status"], "reason": found["reason"],
                   "where": found.get("where"), "pages": found["pages"],
                   "rows": sorted(found["keys"].values())}
                  for _role, found in sorted(case._enumerations.items())]
    body = {"entries": [entries[handle] for handle in sorted(entries)], "unresolved": unresolved,
            "enumerated": enumerated}
    return {**body, "manifest_sha256": hashlib.sha256(_canonical(body)).hexdigest()}


def _claim_checks(case: _Case, unit_contracts: Any, *, originals_readable: bool = True,
                  transcript: Sequence[Any] | None = None) -> dict[str, Any]:
    """The value-claim gate (``operating.claim_checks``) over the case's findings, gaps and
    close outcome, on the rows this evaluator re-read and digest-checked, with what the
    transcript's tool calls returned (an entry's ``result``, not an error) as the evidence
    count and sum claims bind to (``returned_evidence``)."""
    views: dict[str, dict[str, Any] | None] = {}

    def rows(handle: str) -> dict[str, Any] | None:
        if handle.startswith("original://"):
            # An original page view: re-read now (whole text, hashed) and equal to the digest
            # the case recorded; never for an agent-only reader (no original is read for one).
            if not originals_readable:
                return None
            if handle not in views:
                views[handle] = page_view(case.reader.read, handle,
                                          case.reader.citation(handle),
                                          case.reader.pins(handle))
            view = views[handle]
            return None if view is None else {"relation": "original", **view}
        row = case.reader.row(handle)
        if row is None:
            return None
        return {"relation": case.reader.citation(handle).get("relation"), "row": row}
    returned = None
    for entry in transcript or ():
        if isinstance(entry, Mapping) and isinstance(entry.get("result"), Mapping) \
                and not entry.get("is_error"):
            returned = returned_evidence(entry.get("tool") or entry.get("name"),
                                         entry.get("arguments"), entry["result"], returned)
    findings = case.counted + [{**item, "statement": None} for item in case.unusable_findings]
    items, unusable = case_items(findings, case.gaps)
    return check_items(items, rows=rows, calculations=case.calculations,
                       contracts=unit_contracts, unusable=unusable,
                       outcome=case.view.get("outcome"), returned=returned)


def _incomplete_on_refusal(document_fields: DocumentFields | None) -> DocumentFields | None:
    """``document_fields`` whose refusals (source outage, transport, guard) make the evaluation
    incomplete; ``NotVisible`` (this reader may not see the item) stays a check result."""
    if document_fields is None:
        return None

    def read(drive_id: str, item_id: str) -> Mapping[str, Any] | None:
        try:
            return document_fields(drive_id, item_id)
        except NotVisible:
            raise
        except Exception as error:
            raise EvaluationError("evaluation_incomplete: SharePoint document-type read "
                                  f"refused ({type(error).__name__})") from None
    return read


def _confirmations_or_incomplete(kind_confirmations: KindConfirmationsRead | None
                                 ) -> KindConfirmationsRead | None:
    """``kind_confirmations`` whose failures (an unreadable or tampered store) make the
    evaluation incomplete: a person's confirmation is never silently dropped."""
    if kind_confirmations is None:
        return None

    def lookup(original_id: str) -> Mapping[str, Any] | None:
        try:
            return kind_confirmations(original_id)
        except Exception as error:
            raise EvaluationError("evaluation_incomplete: Workbench kind confirmation read "
                                  f"refused ({type(error).__name__})") from None
    return lookup


def evaluate(playbook: Playbook, call: Call, case_id: str,
             transcript: Iterable[Any] | None = None, *,
             unit_contracts: Any = None,
             reader_principals: Iterable[str] | None = None,
             document_fields: DocumentFields | None = None,
             kind_confirmations: KindConfirmationsRead | None = None,
             originals_detail: dict[str, Any] | None = None,
             document_rows: capability_guards.Read | None = None) -> dict[str, Any]:
    """Score ``playbook`` against case ``case_id`` read through ``call`` (read tools only).

    Every evaluation also runs the value-claim gate (``claim_checks``): a structured claim
    whose cited field holds another value, role (a planned figure claimed as produced) or
    unit, a quantity type no role contract covers, or a number in a finding, gap or the
    close outcome that no claim covers fails ``passed_all`` whatever the playbook's own
    checks say. ``unit_contracts`` (a ``SourceUnitContracts``) defaults to
    the builtin contracts, the ones the case host loads by default. What the transcript's
    tool calls returned (an entry's ``result``) is the tool evidence the gate binds count
    and sum claims to; without it a count is a lower bound.

    Every ``original://`` the case cites is re-read through ``call`` (the evaluating session's
    own permission) against the digest the case recorded. When the playbook declares original
    roles, an original fills a role only through a RECORDED kind for that exact original: the
    SharePoint document-type column of its item, read through ``document_fields`` (the
    evaluating actor's GET-only listItem read; None: not read, a limit), or a person's
    Workbench confirmation read through ``kind_confirmations`` (the evaluating reader's
    ``operating.kind_confirmation.confirmed_kinds``; a case finding never confirms); its text
    only contradicts (``operating.playbook_originals``). Any original
    gap blocks passed_all. An expected gap whose status is missing, unknown or
    unexpected (recorded although its condition does not hold on the re-read rows) blocks
    passed_all. A refused read (rate limit, tool error, source outage) raises
    ``EvaluationError`` (evaluation_incomplete): it is never scored as a failed check, so no
    report is made for compare() to count. ``reader_principals`` are the evaluating session's
    principals: when they are all agent principals no original is read
    (``original_not_visible_to_agent``).

    The report (stored and served to the whole case audience by ``PlaybookReportStore``)
    carries only ``public_original_checks``: statuses, kinds, counts and gap codes, never a
    handle, file name, SharePoint id, column value or confirming principal, all of which were
    read under the evaluator's own permission. The full per-original detail is written into
    ``originals_detail`` (a caller-held dict, never stored) when one is passed.

    ``document_rows``: the evaluating actor's ``capability_guards.current_reader`` (the
    host's, as ``plx.act`` propose reads a proposal's cited rows): proposals and absence
    gaps citing a byte-pinned SharePoint file or mail attachment are re-checked on the
    fields its pinned bytes state for that actor. None: such a citation is unread, so a
    guard that needs it fails."""
    definition = playbook.definition  # verifies the hash: a changed definition raises
    if playbook.retired:
        # A version naming retired host tools is kept for the reports and ledgers that
        # bind its hash, never scored: its tool allowlist no longer describes the host.
        raise EvaluationError("playbook_retired:" + ",".join(playbook.retired_tools)[:200])
    if unit_contracts is None:
        unit_contracts = SourceUnitContracts.builtin()
    # Read twice (tool evidence, authority): an iterator would be spent by the first.
    transcript = None if transcript is None else list(transcript)
    reader = _ReadOnly(call)
    # A refused case page (rate limit, tool error) leaves the evaluation incomplete, as a
    # refused row read does: never a crash, never a scored failure.
    view = read_case(reader.read, case_id)
    case = _Case(view, reader, definition)
    boundary = _case_boundary(view)
    # Rows first (so original page reads never use up the session's calls before them), then
    # originals: their recorded kinds decide which original roles they bind.
    cited, withheld = _citations(case)
    for handle in sorted(cited):
        if handle.startswith("api://"):
            reader.citation(handle)
    original_checks = check_case_originals(
        cited.values(), definition["required_evidence"], reader.read, reader.citation,
        reader.pins, reader_principals, withheld=len(withheld),
        findings=(view.get("findings") or {}).get("supported") or [],
        document_fields=_incomplete_on_refusal(document_fields),
        kind_confirmations=_confirmations_or_incomplete(kind_confirmations))
    case.bind_originals(original_checks)
    if originals_detail is not None:
        originals_detail.clear()
        originals_detail.update(original_checks)
    claims = _claim_checks(case, unit_contracts,
                           originals_readable=not is_agent_only(reader_principals),
                           transcript=transcript)

    gap_rows: dict[str, dict[str, Any]] = {}
    recorded, ambiguous = _attribute_gaps(case, definition["expected_gaps"])
    for item in definition["expected_gaps"]:
        # One instance per bound subject of the roles the gap cites: its condition is decided
        # for that subject and a recorded gap must cite that subject's row.
        subjects: list[Subject | None] = list(_gap_subjects(case, item)) or [None]
        instances = []
        for subject in subjects:
            state, detail, ids = _condition(case, item["when"], subject)
            if subject is None:
                found = recorded[item["id"]]
            else:
                found = [gid for gid in recorded[item["id"]]
                         if _recorded_cites(case, gid, subject[1])]
            instances.append({
                "gap": item["id"], "subject": subject[1] if subject else None,
                "subject_role": subject[0] if subject else None,
                "condition_state": state, "condition_held": state == HOLDS,
                "condition_detail": detail, "condition_evidence": ids,
                "recorded": bool(found), "recorded_gap_ids": sorted(found, key=_id_key),
                "status": ("unknown" if state == UNKNOWN else
                           "ok" if (state == HOLDS) == bool(found) else
                           "missing" if state == HOLDS else "unexpected")})
        states = {inst["condition_state"] for inst in instances}
        state = UNKNOWN if UNKNOWN in states else HOLDS if HOLDS in states else NOT_HOLD
        held_inst = [inst for inst in instances if inst["condition_held"]]
        ids_recorded = sorted({gid for inst in instances for gid in inst["recorded_gap_ids"]},
                              key=_id_key)
        lead = next((inst for inst in instances if inst["condition_state"] == state),
                    instances[0])
        recorded_all = (all(inst["recorded"] for inst in held_inst) if held_inst
                        else bool(ids_recorded))
        gap_rows[item["id"]] = {
            "id": item["id"], "kind": item["kind"], "when": item["when"],
            "condition_state": state, "condition_held": state == HOLDS,
            "condition_detail": lead["condition_detail"],
            "condition_evidence": sorted({e for inst in instances
                                          for e in inst["condition_evidence"]}, key=_id_key),
            "recorded": recorded_all, "recorded_gap_ids": ids_recorded,
            "subjects": instances,
            "status": ("unknown" if UNKNOWN in {i["status"] for i in instances} else
                       "missing" if "missing" in {i["status"] for i in instances} else
                       "unexpected" if "unexpected" in {i["status"] for i in instances}
                       else "ok")}

    handlers: dict[str, Callable[..., dict[str, Any]]] = {
        "every_fact_cites": _check_every_fact_cites,
        "no_uncited_number_in_statements": _check_no_uncited_numbers,
        "evidence_cited": _check_evidence_cited,
        "proposals_recorded_only": _check_proposals_recorded_only,
        "proposals_cite_support": _check_proposals_cite_support,
        "basis_row_agrees": _check_basis_row_agrees,
        "component_sums": _check_component_sums}
    checks = []
    for check in definition["success_checks"]:
        check = {**check, "params": check.get("params", {})}
        if check["type"] == "gap_recorded":
            checks.append(_check_gap_recorded(case, check, gap_rows))
        elif check["type"] == "calculation_equals_cited_arithmetic":
            checks.append(_check_calculation(case, check, definition))
        elif check["type"] in handlers and check["type"] in CHECK_TYPES:
            checks.append(handlers[check["type"]](case, check))
        else:
            raise EvaluationError(f"unsupported_check_type: {check['type']}")

    violations = (_proposal_violations(case) + _guard_violations(case, document_rows)
                  + _absence_violations(case, document_rows)
                  + _transcript_violations(definition["allowed_tools"], transcript))
    failed = [item["id"] for item in checks if not item["passed"]]
    manifest = _manifest(case)
    unbound_rows = sum(1 for entry in manifest["entries"]
                       if "unbound" in entry["identity"].values())
    # Closed evidence world: every cited source row (findings, calculations, gaps,
    # proposals, verifications) must be bound to the case subject by some role. A row
    # of a relation no role declares, a row no role binds, or one that cannot be
    # re-read blocks passed_all. original:// citations are gated by their own audience
    # and byte re-hash, not by identity, and are counted separately.
    outside = sorted(entry["handle"] for entry in manifest["entries"]
                     if entry["handle"].startswith("api://")
                     and not {"bound", "any"} & set(entry["identity"].values()))
    # Rows accepted only by an identity "any" role are not tied to the case subject:
    # they keep passed_all (the playbook allows them) but never passed_all_clean.
    unchecked = sorted(entry["handle"] for entry in manifest["entries"]
                       if entry["handle"].startswith("api://")
                       and "bound" not in entry["identity"].values()
                       and "any" in entry["identity"].values())
    originals_cited = sum(1 for entry in manifest["entries"]
                          if entry["handle"].startswith("original://"))
    units = [case.unit_inputs[key] for key in sorted(case.unit_inputs)]
    unverified = sum(1 for item in units if item["unit_basis"] != "source_contract")
    authority_checked = transcript is not None
    attributed = {gid for ids in recorded.values() for gid in ids} | set(ambiguous)
    unattributed = [gap["id"] for gap in case.gaps
                    if gap.get("citations_status") in ("resolved", "uncited")
                    and gap.get("state") in capability_guards.ABSENCE_STATES
                    and gap["id"] not in attributed]
    limits = sorted({limit for item in checks for limit in item.get("limits", [])}
                    | ({ABSENCE_UNATTRIBUTED} if unattributed else set())
                    | ({IDENTITY_NOT_CHECKED} if unchecked else set())
                    | ({ORIGINALS_NOT_IDENTITY_CHECKED} if originals_cited else set())
                    | set(original_checks["limits"]))
    # Identity and presence apply to the whole case, not only to roles a check names:
    # a cited row of another subject, or a required role with no bound row, blocks it.
    required_missing = sorted(
        item["role"] for item in definition["required_evidence"]
        if item.get("required", True) and item["relation"] != "original"
        and not case.binding(item["role"])[0])
    changed_rereads = sorted(entry["handle"] for entry in manifest["entries"]
                             if entry["reread"] == "changed")
    passed_all = (not failed and not violations and boundary["chain_ok"]
                  and claims["passed"] and original_checks["ok"]
                  and not changed_rereads
                  and not unbound_rows and not outside and not required_missing
                  # An unexpected gap (recorded while its condition does not hold on
                  # the cited rows, e.g. absent_in_source on an order whose PO field
                  # holds a value) is a wrong answer, never a pass.
                  and not any(row["status"] in ("missing", "unknown", "unexpected")
                              for row in gap_rows.values()))
    report = {
        "schema": REPORT_SCHEMA,
        "playbook": {"id": playbook.id, "version": playbook.version},
        "definition_sha256": playbook.definition_sha256,
        "evaluator_version": EVALUATOR_VERSION,
        "case_id": case_id,
        "case_boundary": boundary,
        "evidence_manifest": manifest,
        "original_checks": public_original_checks(original_checks),
        "transcript": "supplied" if authority_checked else "none",
        "unit_semantics": units,
        "limits": limits,
        "checks": checks,
        "claim_checks": claims,
        "expected_gaps": [gap_rows[item["id"]] for item in definition["expected_gaps"]],
        "ambiguous_gap_ids": sorted(set(ambiguous), key=_id_key),
        "authority_violations": len(violations),
        "authority_violation_details": violations,
        "summary": {
            "checks": len(checks), "passed": len(checks) - len(failed), "failed": len(failed),
            "failed_ids": failed,
            "gaps_expected_held": sum(1 for row in gap_rows.values() if row["condition_held"]),
            "gaps_missing": sum(1 for row in gap_rows.values() if row["status"] == "missing"),
            "gaps_unexpected": sum(1 for row in gap_rows.values()
                                   if row["status"] == "unexpected"),
            "gaps_unknown": sum(1 for row in gap_rows.values()
                                if row["status"] == "unknown"),
            "transcript_evaluated": authority_checked,
            # Zero authority violations without a transcript is "not checked", never "clean":
            # the case itself only shows recorded proposals, not which tools an agent called.
            "authority_checked": authority_checked,
            "unbound_evidence_rows": unbound_rows,
            "changed_rereads": changed_rereads,
            "required_roles_missing": required_missing,
            "evidence_rows_outside_roles": outside,
            "evidence_rows_identity_unchecked": unchecked,
            "originals_cited_not_identity_checked": originals_cited,
            "original_gaps": len(original_checks["gaps"]),
            "unit_inputs": len(units),
            "unverified_unit_inputs": unverified,
            "claims_checked": claims["counts"]["claims"],
            "claim_failures": len(claims["failures"]),
            "claim_failure_codes": sorted({item["code"] for item in claims["failures"]}),
            "claim_items_unchecked": claims["unchecked_items"],
            "claim_leads": claims["leads"],
            "passed_all": passed_all,
            "passed_all_qualification": None if authority_checked else "authority_not_checked",
            "limits": limits,
            "passed_all_clean": passed_all and authority_checked and not limits}}
    report["report_sha256"] = hashlib.sha256(_canonical(report)).hexdigest()
    return report


def verify_report_hash(report: Mapping[str, Any]) -> bool:
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    return hashlib.sha256(_canonical(body)).hexdigest() == report.get("report_sha256")


def _manifest_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    a = {entry["handle"]: entry for entry in old.get("entries", [])}
    b = {entry["handle"]: entry for entry in new.get("entries", [])}
    changes = []
    if set(b) - set(a):
        changes.append("evidence added: " + ", ".join(sorted(set(b) - set(a))))
    if set(a) - set(b):
        changes.append("evidence removed: " + ", ".join(sorted(set(a) - set(b))))
    for handle in sorted(set(a) & set(b)):
        if a[handle] != b[handle]:
            fields = sorted(key for key in set(a[handle]) | set(b[handle])
                            if a[handle].get(key) != b[handle].get(key))
            changes.append(f"evidence changed: {handle} ({', '.join(fields)})")
    if old.get("unresolved") != new.get("unresolved"):
        changes.append("unresolved (withheld) citations differ")
    return changes


def case_changes(report_old: Mapping[str, Any], report_new: Mapping[str, Any]) -> list[str]:
    """What differs in the case two reports evaluated (empty when it is the same case content)."""
    old_b, new_b = report_old.get("case_boundary"), report_new.get("case_boundary")
    old_m, new_m = report_old.get("evidence_manifest"), report_new.get("evidence_manifest")
    if not isinstance(old_b, Mapping) or not isinstance(new_b, Mapping) \
            or not isinstance(old_m, Mapping) or not isinstance(new_m, Mapping):
        return ["a report lacks case_boundary or evidence_manifest (not a frozen evaluation)"]
    changes = [f"case boundary {key}: {old_b.get(key)!r} -> {new_b.get(key)!r}"
               for key in sorted(set(old_b) | set(new_b)) if old_b.get(key) != new_b.get(key)]
    return changes + _manifest_changes(old_m, new_m)


def compare(report_old: Mapping[str, Any], report_new: Mapping[str, Any], *,
            allow_changed_case: bool = False,
            allow_changed_evaluator: bool = False) -> dict[str, Any]:
    """Two playbook versions scored on the same case: which checks regressed.

    ``regression`` is true when a check that passed under the old version fails under the new
    one, a passing check was removed (the new version no longer asks), an expected gap that was
    ok no longer is, authority violations increased, an original gap (``original_checks``)
    appeared, or the case no longer passes as a whole (``passed_all``, e.g. a stricter version
    adds a check the case fails). Different cases or playbooks are refused, and so are two
    reports whose case event boundary or evidence manifest differ (the case moved between the
    runs, so the difference is not the playbook's) or that were scored by different evaluator
    versions, unless ``allow_changed_case`` / ``allow_changed_evaluator`` is given; the refusal
    and the result name what changed.
    """
    if report_old.get("case_id") != report_new.get("case_id"):
        raise EvaluationError("different_cases")
    if report_old["playbook"]["id"] != report_new["playbook"]["id"]:
        raise EvaluationError("different_playbooks")
    changes = case_changes(report_old, report_new)
    if changes and not allow_changed_case:
        raise EvaluationError("case_changed_between_reports: " + "; ".join(changes))
    evaluators = (report_old.get("evaluator_version"), report_new.get("evaluator_version"))
    if evaluators[0] != evaluators[1] and not allow_changed_evaluator:
        raise EvaluationError(f"different_evaluator_versions: {evaluators[0]!r} -> "
                              f"{evaluators[1]!r}")
    old = {item["id"]: item for item in report_old["checks"]}
    new = {item["id"]: item for item in report_new["checks"]}
    shared = sorted(set(old) & set(new))
    newly_failing = [i for i in shared if old[i]["passed"] and not new[i]["passed"]]
    newly_passing = [i for i in shared if not old[i]["passed"] and new[i]["passed"]]
    removed_passing = sorted(i for i in set(old) - set(new) if old[i]["passed"])
    gaps_old = {row["id"]: row for row in report_old["expected_gaps"]}
    gaps_new = {row["id"]: row for row in report_new["expected_gaps"]}
    gaps_newly_missing = sorted(i for i in set(gaps_old) & set(gaps_new)
                                if gaps_old[i]["status"] == "ok" and gaps_new[i]["status"] != "ok")
    more_violations = report_new["authority_violations"] > report_old["authority_violations"]
    originals_old, originals_new = _original_gap_keys(report_old), _original_gap_keys(report_new)
    original_gaps_new = sorted((originals_new - originals_old).elements())
    passed_all = {"old": report_old["summary"]["passed_all"] is True,
                  "new": report_new["summary"]["passed_all"] is True}
    return {
        "case_id": report_old["case_id"], "playbook_id": report_old["playbook"]["id"],
        "from": {"version": report_old["playbook"]["version"],
                 "definition_sha256": report_old["definition_sha256"],
                 "report_sha256": report_old["report_sha256"]},
        "to": {"version": report_new["playbook"]["version"],
               "definition_sha256": report_new["definition_sha256"],
               "report_sha256": report_new["report_sha256"]},
        "same_case_content": not changes, "case_changes": changes,
        "evaluator_versions": {"old": evaluators[0], "new": evaluators[1]},
        "newly_failing": newly_failing, "newly_passing": newly_passing,
        "removed_passing_checks": removed_passing,
        "added_checks": sorted(set(new) - set(old)),
        "gaps_newly_not_ok": gaps_newly_missing,
        "authority_violations": {"old": report_old["authority_violations"],
                                 "new": report_new["authority_violations"]},
        "original_gaps_new": original_gaps_new,
        "original_gaps_resolved": sorted((originals_old - originals_new).elements()),
        "passed_all": passed_all,
        "regression": bool(newly_failing or removed_passing or gaps_newly_missing
                           or more_violations or original_gaps_new
                           or (passed_all["old"] and not passed_all["new"]))}


def unread_evidence(report: Mapping[str, Any]) -> bool:
    """True when the evaluator did not read the whole case: an item withheld from it (the case
    view withholds every item one of whose citations this reader cannot resolve, and shows its
    citations without a handle) or a cited ``api://`` row it could not re-read (other schemes
    have no re-read row: an original's view carries none, and their own checks read them). Such
    a report is no score of the case: a regression on what it could not read is invisible."""
    manifest = report.get("evidence_manifest")
    if not isinstance(manifest, Mapping):
        return True
    return bool(manifest.get("unresolved")) or any(
        entry.get("reread") == "failed" and str(entry.get("handle")).startswith("api://")
        for entry in manifest.get("entries") or ())


def case_runner(call: Call, playbook_id: str, *,
                transcripts: Mapping[str, Iterable[Any]] | None = None,
                record: Callable[[dict[str, Any], str], Any] | None = None,
                unscorable: Callable[[str], Iterable[str]] | None = None,
                **kwargs: Any) -> Callable[[Any, Any, str], dict[str, Any]]:
    """The playbook item runner of ``operating.semantic_proposals`` (the one adoption path,
    registered by ``proposal_store``): ``run(before, after, case_id)`` scores the case with the
    current version of ``playbook_id`` in each staged pair of playbook layers (shipped, overlay;
    without and with the proposed version, ``PlaybookRegistry.load_layers``) and compares the
    two reports with ``compare``, the one regression rule. Each side passes on ``passed_all``,
    is ``unscored`` when the evaluator did not read the whole case (``unread_evidence``), and
    both sides are ``unscorable`` when no reader can either: ``unscorable(case_id)``, the host's
    reasons (``CaseRuntime.hidden_from_every_reader``), is non-empty; its report hash is its
    evidence; ``record(report, case_id)`` stores each report before
    anything is returned. A refused read (``evaluation_incomplete``), a refused record or a case
    that changed between the two runs (``compare``) raises, so no comparison is recorded for
    it."""
    def score(layers: tuple[Path, Path | None], case_id: str) -> dict[str, Any]:
        playbook = PlaybookRegistry().load_layers(*layers).get(playbook_id)
        report = evaluate(playbook, call, case_id, (transcripts or {}).get(case_id), **kwargs)
        if record is not None:
            record(report, case_id)
        return report

    def run(before: tuple[Path, Path | None], after: tuple[Path, Path | None],
            case_id: str) -> dict[str, Any]:
        reports = {"before": score(before, case_id), "after": score(after, case_id)}
        unread = any(unread_evidence(report) for report in reports.values())
        reasons = sorted(set(unscorable(case_id))) if unread and unscorable else []
        result = {**{side: {"outcome": "unscorable" if reasons
                            else "unscored" if unread_evidence(report)
                            else "pass" if report["summary"]["passed_all"] else "fail",
                            "evidence_sha256": report["report_sha256"]}
                     for side, report in reports.items()},
                  "comparison": compare(reports["before"], reports["after"])}
        return {**result, "unscorable": reasons} if reasons else result
    return run


PROPOSAL_RUNNER = "playbook_eval.case_runner"


def proposal_store(reports: Any, *, frozen_cases: list[str], stewards: Iterable[str] = (),
                   overlay: str | Path | None = None,
                   shipped: str | Path | None = None) -> ProposalStore:
    """The adoption path of playbook versions (``operating.semantic_proposals``) whose one
    registered runner is ``case_runner`` (``PROPOSAL_RUNNER``), its records in the change chain
    of ``reports`` (a ``playbook_reports.PlaybookReportStore``). ``evaluate(proposal_id, actor,
    PROPOSAL_RUNNER, call=..., transcripts=..., unscorable=...)`` scores every frozen case with
    the proposal's own playbook id, reading through ``call`` (the evaluator's session; the host's
    ``unscorable`` probe names a case no reader can read in full), and the store records
    every report in ``reports`` as the evaluator's, so each comparison digest names a stored
    report (the evaluator must be in the case's audience, else recording refuses and nothing
    is compared). ``overlay``: the directory adopted versions are written to
    (``playbooks.overlay_dir`` of the host's state directory; without one nothing is adopted);
    ``shipped``: the read-only playbook directory (default the shipped one)."""
    def runner(proposal: Mapping[str, Any], record: Callable[..., int], *, call: Call,
               transcripts: Mapping[str, Iterable[Any]] | None = None,
               **kwargs: Any) -> Callable[[Any, Any, str], dict[str, Any]]:
        def store(report: dict[str, Any], case_id: str) -> int:
            return record(report, transcript_source="none"
                          if (transcripts or {}).get(case_id) is None
                          else "proposal-evaluation-transcript")
        return case_runner(call, Playbook(proposal["content"]).id, transcripts=transcripts,
                           record=store, **kwargs)
    return ProposalStore(reports, runners={"playbook": {PROPOSAL_RUNNER: runner}},
                         frozen_items={"playbook": frozen_cases}, stewards=stewards,
                         shipped=None if shipped is None else {"playbook": shipped},
                         overlays={"playbook": overlay})


def _original_gap_keys(report: Mapping[str, Any]) -> Counter[str]:
    """Multiset of "gap [role]" of every original gap in a report (the public summary names
    no original; none in reports made before original checks existed)."""
    checks = report.get("original_checks") or {}
    return Counter(" ".join(filter(None, (str(item.get("gap")), item.get("role"))))
                   for item in checks.get("gaps") or [])


# (read-only harnesses) so they share its pin / re-read / digest semantics.
# (``operating.eval_cases``) so they share its pin / re-read / digest semantics.
ReadOnlyReader = _ReadOnly

__all__ = ["EVALUATOR_VERSION", "EvaluationError", "PROPOSAL_RUNNER", "READ_TOOLS", "REPORT_SCHEMA",
           "ReadOnlyReader", "case_changes", "case_runner", "compare", "evaluate", "proposal_store",
           "read_case", "unread_evidence", "verify_report_hash"]
