"""Durable investigation case record for an agent-driven case runtime.

A case is an append-only, hash-chained event log in SQLite. Every mutation
(open, finding, supersede, compute, gap, propose, verification, close) is one event carrying
actor, time and a dense per-case sequence, written in a single IMMEDIATE
transaction, so a crashed writer leaves either the whole event or nothing. The
current view is a pure projection (fold) of those events.

What this module does not do: it never executes a proposal, never talks to a
source system, and never decides what a citation means. Citations are opaque
handles resolved through an injected ``citation_resolver(handle, actor)`` that
returns a dict or raises a typed error. Cases are visible only to their audience
(tenant, clearance and principal intersection, as in ``ontology.core``), and
every read re-resolves citations for the reading actor: content whose citation
is no longer resolvable for that actor is withheld, never leaked. The same gate
applies to findings, calculations, gaps and proposals: a gap or proposal that
quotes cited data records those citations, and a reader who cannot resolve all
of them now sees only a withheld marker (no text, payload or preconditions). A
citation that still resolves but whose content digest differs from the one
recorded at write time is reported as ``citation_content_changed``; such a
finding is never presented as currently supported.

Arithmetic is Decimal only, over a whitelisted expression grammar parsed with
``ast`` (no eval), with symbolic unit checking. A quantity may also carry a
role (planned, manufactured, filled, accepted, ...): roles are different
quantities of one dimension, so two different roles are never added,
subtracted, multiplied or divided unless the calculation declares that relation
(``relations``); a source unit contract may pin a column's role. A relation's
``as`` is a label; the result's quantity type comes from the expression tree
(``operating.quantity_types``), and a label must fit it.

Links (``link``): the Workbench checks a relationship between two cited
source rows against a declared link contract (``operating.case_links``) and
records the outcome (verified, lead or contradicted) with both citations, the
contract and the evaluated values; a verified or contradicted link also records
a Workbench-written finding. A finding that claims a relationship names its
``relation_links`` and may be ``supported`` only while every one of them is
verified over unchanged content. Other findings are unchanged.

Header provenance (``header_relations``): a case's question and roots may name
identifiers read from source rows. The opener may record, per such identifier,
the source relations that hold it; every read then also requires the reader to
be allowed now, by the injected ``header_gate(relations, actor)``, on at least
one relation of every recorded identifier. So a revoked source grant hides the
whole case (get, list and every view built on them) at once, and a restored
grant shows it again. Cases opened without header relations are unaffected.

Claims (``claim``): a lease that keeps concurrent sessions from writing over each
other on one case. ``case.claimed`` names the holder (the session's reference and
its subject) and an expiry from the store clock; ``case.released`` ends it, with an
optional handoff note for whoever continues. Both are compare-and-set inside the
write transaction: another holder's unexpired claim refuses the claim, and every
other write while it lasts (``case_claimed_until:<expires_at>``) unless it runs as
the holder (``writing_as``, set by the session host per call); a write that names
no holder is not the holder. One write is never refused: the read-back of a business
write already dispatched (``verification`` with ``dispatched_read_back``, which only
``ActionPath`` sets), since refusing it leaves the intent dispatched with what the owner
shows unrecorded. A release (never a claim or renewal) is also accepted
from another session of the holder's principal when that subject is one employee
(``employee:<oid>``): the same person, restarted. A subject of group principals
names no one person, so a co-member's session may neither release nor take over.
``case.claim_broken`` ends another holder's active claim with a recorded reason, the
approver who broke it and the author kind of the session that wrote the reason
(``break_claim``; the caller checks the approver, as ``ActionPath.break_claim`` does
against its roster). Every break stays in the claim's ``breaks`` history; the broken
principal's claim right after it (no other claim in between) is recorded on the break
(``reclaimed``). Expiry is judged at read time. The claim, its breaks and the last
handoff note are case state: only the case audience sees them.

Objective claims (``claim_objective``): an objective spanning cases is claimed as those
cases' own claims, taken all or none. Every claimant takes them in one order (sorted
case ids), so two sessions racing for overlapping sets first meet on their least shared
case and the loser never holds part. Every case is claimed in one write transaction: the
first case that refuses rolls it back, so every lease is exactly as it was (none taken,
none renewed or shortened, nothing journalled) and ``ObjectiveRefused`` names that case.
A release or a break goes to every case (``each_objective_case``) and counts what it
ended and what refused (``summary``); no other store or lease exists.
"""

from __future__ import annotations

import ast
import contextvars
import decimal
import hashlib
import json
import keyword
import logging
import math
import re
import sqlite3
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from uuid import uuid4

from ontology.core import LEVELS, Actor, ValidationError

from . import quantity_types as _qt
from .entitlements import _AGENT_PREFIXES  # agents are never principals (one rule)

_LOG = logging.getLogger(__name__)

FINDING_KINDS = ("fact", "interpretation")
FINDING_STATUSES = ("supported", "contradicted", "unknown")
GAP_STATES = ("absent_in_source", "inaccessible", "stale", "not_ingested", "unknown")
CASE_STATUSES = ("open", "closed")
# Who wrote a case's question (``CaseStore.open``); absent on a case = unknown.
OBJECTIVE_AUTHOR_KINDS = ("human", "agent", "unknown")
# Who wrote a finding's statement (``CaseStore.finding``); absent is unknown.
AUTHOR_KINDS = ("human", "agent")
# What a read-back of a proposal's expected effect found; never "failed": a value
# not seen is not proof that nothing happened, and a value seen is not proof of who
# changed it or that anything was approved.
VERIFICATION_OUTCOMES = ("effect_observed", "effect_not_observed", "unknown")
LINK_OUTCOMES = ("verified", "stored_by_text_join", "lead", "contradicted")
EXPECTED_EFFECT_KEYS = frozenset({"relation", "key", "fields", "equals"})
EXPECTED_EFFECT_VALUE_KEYS = frozenset({"relation", "key", "values"})

PROPOSAL_STATUS = "proposed — requires approval through the existing approval journal"
FACT_LABEL = "cited_fact"
INTERPRETATION_LABEL = "model_output_interpretation"
WITHHELD = "withheld_citation_unavailable"
CITATION_UNAVAILABLE = "citation_unavailable"
CITATION_CONTENT_CHANGED = "citation_content_changed"

_SCHEMA_VERSION = 1
_GENESIS = "0" * 64
_MAX_TEXT = 4000
_MAX_CITATIONS = 20
_MAX_ROOTS = 20
_MAX_INPUTS = 20
_MAX_LIST = 200
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_PAYLOAD_DEPTH = 8
_MAX_EFFECT_FIELDS = 5
_MAX_HEADER_GROUPS = 64
_MAX_HEADER_RELATIONS = 50
_RELATION = re.compile(r"^[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{0,63}$")
# A column, or one key of a JSON-object column: ``custom_fields.custPoCode``.
_EFFECT_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63})?$")
MAX_LEASE_SECONDS = 3600
MAX_OBJECTIVE_CASES = 64
CLAIM_KINDS = ("case.claimed", "case.released", "case.claim_broken")
SESSION_KIND = "session.recorded"  # an agent session (``record_session``)
_SESSION_REF = re.compile(r"^[0-9a-f]{64}\Z")
# A claim subject that names one person: exactly one employee principal (its Entra oid).
_EMPLOYEE_SUBJECT = re.compile(
    r"employee:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class CaseNotFound(LookupError):
    """Unknown case, or a case this actor may not see (deliberately identical)."""


class CaseClosed(ValidationError):
    pass


class CaseClaimed(ValidationError):
    """Another holder's unexpired claim refuses this write."""


class CaseIntegrityError(RuntimeError):
    pass


class ObjectiveRefused(ValidationError):
    """One case of an objective refused it: ``objective_refused:<case_id>:<refusal>``,
    the refusal its one-case call gives (an unseen case: ``case_not_found``)."""

    def __init__(self, case_id: str, refusal: str) -> None:
        super().__init__(f"objective_refused:{case_id}:{refusal}")
        self.case_id, self.refusal = case_id, refusal


class CitationError(Exception):
    """Base for typed errors a citation resolver may raise."""


class CitationUnresolvable(CitationError):
    """Raised by the store when a citation cannot be resolved for the actor."""


class ExpressionError(ValidationError):
    pass


class UnitMismatch(ExpressionError):
    pass


class QuantityRoleMismatch(UnitMismatch):
    """Different quantity roles combined without a declared relation."""


CitationResolver = Callable[[str, Actor], Mapping[str, Any]]
# (handle, resolved citation, field) -> {"id", "unit", ...} or None; see operating.source_units
UnitContractLookup = Callable[[str, Mapping[str, Any], str], Mapping[str, Any] | None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def actor_label(actor: Actor) -> str:
    """Audit label. ``Actor`` carries no individual identity, only principals."""
    return ",".join(sorted(actor.principals))


def _text(value: Any, name: str, maximum: int = _MAX_TEXT) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum or
            any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise ValidationError(f"invalid_{name}")
    return value.strip()


def _digest(resolved: Mapping[str, Any]) -> str:
    """A resolved view's content digest. Keys the view itself declares ``derived`` (a list
    of key names: e.g. the row a pinned file's bytes are re-extracted to, which the claim
    gate re-reads) and ``derived`` itself are left out, so the digest binds the source
    content only; a ``derived`` of any other shape hashes the view whole."""
    derived = resolved.get("derived")
    if isinstance(derived, list) and all(isinstance(key, str) for key in derived):
        resolved = {key: value for key, value in resolved.items()
                    if key != "derived" and key not in derived}
    return _sha(json.dumps(resolved, sort_keys=True, default=str, ensure_ascii=False))


def _handle(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 2048 or
            any(ord(char) < 32 for char in value)):
        raise ValidationError("invalid_citation_handle")
    return value.strip()


def _strings(values: Any, name: str, maximum: int) -> list[str]:
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise ValidationError(f"invalid_{name}")
    out = [_text(item, name, 500) for item in values]
    if len(out) > maximum:
        raise ValidationError(f"too_many_{name}")
    return out


def _plain_json(value: Any, depth: int = 0) -> Any:
    """Validate JSON-able data and refuse floats (money and quantities are strings)."""
    if depth > _MAX_PAYLOAD_DEPTH:
        raise ValidationError("payload_too_deep")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise ValidationError("float_not_allowed_use_string_or_decimal")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValidationError("non_finite_decimal")
        return _dec_str(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValidationError("payload_keys_must_be_strings")
        return {key: _plain_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item, depth + 1) for item in value]
    raise ValidationError("payload_not_json")


# --------------------------------------------------------------------------
# Units and Decimal arithmetic
# --------------------------------------------------------------------------

Unit = tuple[tuple[str, int], ...]
_UNIT_TERM = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)(?:\^(-?[1-9][0-9]?))?$")
_NUMBER = re.compile(r"^(?:[0-9]+\.?[0-9]*|\.[0-9]+)$")
_EXPR_CHARS = re.compile(r"^[A-Za-z0-9_ .+\-*/()]+$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UNICODE_OPS = {ord("×"): "*", ord("÷"): "/", ord("−"): "-"}
_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_MAX_RELATIONS = 10
_NEUTRAL = ""  # role of a literal-only term: it takes the other operand's role
_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_MAX_EXPR = 500
_MAX_NODES = 400
_MAX_DEPTH = 100
_MAX_DIGITS = 50


def _effect_value(value: Any) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError("invalid_expected_effect_equals")
    if isinstance(value, str):
        return _text(value, "expected_effect_equals", 500).strip()
    if abs(value) > 10 ** 18:
        raise ValidationError("invalid_expected_effect_equals")
    return value


def parse_expected_effect(value: Any) -> dict[str, Any]:
    """Validate the shape of a proposal's expected effect on one source row.

    One value per field: ``{"relation": "schema.relation", "key": {<key columns>},
    "values": {<column or column.jsonKey>: <non-empty text or integer>, ...}}`` (1..5
    fields), or the one-field short form ``{"relation", "key", "fields": [<field>],
    "equals": <value>}``. The effect is observed when every listed field of that row
    holds its value. Returned as ``{"relation", "key", "fields", "values"}``. Whether the
    row is one the proposal cites is checked by ``CaseStore.propose``.
    """
    if not isinstance(value, Mapping) or set(value) not in (EXPECTED_EFFECT_KEYS,
                                                           EXPECTED_EFFECT_VALUE_KEYS):
        raise ValidationError("invalid_expected_effect")
    relation = value["relation"]
    if not isinstance(relation, str) or _RELATION.fullmatch(relation) is None:
        raise ValidationError("invalid_expected_effect_relation")
    key = value["key"]
    if not isinstance(key, Mapping) or not key:
        raise ValidationError("invalid_expected_effect_key")
    if "values" in value:
        values = value["values"]
        if not isinstance(values, Mapping):
            raise ValidationError("invalid_expected_effect_fields")
        fields = list(values)
    else:
        fields = value["fields"]
        if (isinstance(fields, (str, Mapping)) or not isinstance(fields, Sequence)):
            raise ValidationError("invalid_expected_effect_fields")
        if len(fields) > 1:
            # A multi-field write names each field's own value (``values``), so a read-back
            # verifies every field, never "any one of them".
            raise ValidationError("invalid_expected_effect_one_value_per_field")
        values = {fields[0]: value["equals"]} if fields and isinstance(fields[0], str) else {}
    if (not 1 <= len(fields) <= _MAX_EFFECT_FIELDS
            or any(not isinstance(item, str) or _EFFECT_FIELD.fullmatch(item) is None
                   for item in fields)
            or len(set(fields)) != len(fields)):
        raise ValidationError("invalid_expected_effect_fields")
    return {"relation": relation, "key": _plain_json(dict(key)), "fields": list(fields),
            "values": {field: _effect_value(values[field]) for field in fields}}


def effect_values(effect: Mapping[str, Any]) -> dict[str, Any]:
    """field -> expected value of a stored effect (also a record from before per-field
    values, whose one ``equals`` applies to each listed field)."""
    if isinstance(effect.get("values"), Mapping):
        values = {str(field): value for field, value in effect["values"].items()}
        order = effect.get("fields")
        if isinstance(order, (list, tuple)) and sorted(map(str, order)) == sorted(values):
            # The listed field order (stored JSON may sort the mapping's keys).
            return {str(field): values[str(field)] for field in order}
        return values
    fields = effect.get("fields")
    if not isinstance(fields, (list, tuple)):
        return {}
    return {field: effect.get("equals") for field in fields if isinstance(field, str)}


LEGACY_EFFECT_AMBIGUOUS = "legacy_effect_one_value_several_fields"


def legacy_one_value_effect(effect: Mapping[str, Any]) -> bool:
    """A stored effect from before per-field values naming several fields with one
    ``equals`` (new records name one value per field)."""
    fields = effect.get("fields")
    return (not isinstance(effect.get("values"), Mapping) and "equals" in effect
            and isinstance(fields, (list, tuple)) and len(fields) > 1)


def effect_outcome(effect: Mapping[str, Any], statuses: Mapping[str, str]) -> tuple[str, Any]:
    """(outcome, reason) of a read-back from per-field statuses (``field_status``): the
    effect is observed only when every field holds its value.

    A record from before per-field values (one ``equals`` for several fields) does not
    say which field the write sets: observed when every field holds the value, not
    observed when none does, and unknown (``legacy_effect_one_value_several_fields``)
    when some do and some do not, whatever the catalog says today."""
    found = list(statuses.values())
    if found and all(s == "equals_expected" for s in found):
        return "effect_observed", None
    if "not_inspectable" in found:
        return "unknown", "field_not_inspectable"
    if legacy_one_value_effect(effect) and "equals_expected" in found:
        return "unknown", LEGACY_EFFECT_AMBIGUOUS
    return "effect_not_observed", None


def parse_unit(text: Any) -> Unit:
    """``USD/case``, ``EA``, ``m^2``, ``1`` (dimensionless) -> canonical vector.

    Grammar: ``terms ('/' term)*`` with terms joined by ``*`` only before the
    first ``/``, so ``a/b*c`` is rejected rather than read ambiguously.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("unit_required_use_1_for_dimensionless")
    parts = [part.strip() for part in text.strip().split("/")]
    powers: dict[str, int] = {}

    def add(term: str, sign: int) -> None:
        if term == "1":
            return
        match = _UNIT_TERM.match(term)
        if not match:
            raise ValidationError(f"invalid_unit:{text}")
        exponent = int(match.group(2) or 1) * sign
        powers[match.group(1)] = powers.get(match.group(1), 0) + exponent

    for term in parts[0].split("*"):
        add(term.strip(), 1)
    for term in parts[1:]:
        if "*" in term:
            raise ValidationError(f"ambiguous_unit_use_repeated_slash:{text}")
        add(term, -1)
    return tuple(sorted((name, power) for name, power in powers.items() if power))


def format_unit(unit: Unit) -> str:
    def term(name: str, power: int) -> str:
        return name if power == 1 else f"{name}^{power}"

    numerator = [term(name, power) for name, power in unit if power > 0]
    denominator = [term(name, -power) for name, power in unit if power < 0]
    return ("*".join(numerator) if numerator else "1") + "".join("/" + d for d in denominator)


def _combine(left: Unit, right: Unit, sign: int) -> Unit:
    powers = dict(left)
    for name, power in right:
        powers[name] = powers.get(name, 0) + sign * power
    return tuple(sorted((name, power) for name, power in powers.items() if power))


def _dec_str(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _to_decimal(value: Any, what: str) -> Decimal:
    """Exact Decimal from int, str or Decimal. Floats are refused, never coerced."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationError(f"float_or_bool_value_not_allowed:{what}")
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, str):
            number = Decimal(value.strip())
        else:
            raise ValidationError(f"value_not_numeric:{what}")
    except decimal.InvalidOperation as exc:
        raise ValidationError(f"value_not_numeric:{what}") from exc
    if (not number.is_finite() or number.adjusted() > 100 or
            (number != 0 and number.adjusted() < -100) or
            len(number.as_tuple().digits) > _MAX_DIGITS):
        raise ValidationError(f"value_out_of_range:{what}")
    return number


_BINARY64_DIGITS = 15  # significant digits a binary64 float carries meaningfully


def _cited_float(value: float, what: str) -> tuple[Decimal, dict[str, Any]]:
    """Decimal of a JSON number the source sent with a fraction, plus how it was read.

    A JSON decoder hands such numbers over as binary floats. The value used is
    the float's shortest round-trip text (what the source most plausibly sent);
    when that text has a fraction and more than 15 significant digits it is a
    binary artifact (``35.099999999999994``) and is rounded half-even to 15
    significant digits. The raw text and the rule are recorded with the
    calculation, so the reading is visible, never silent. Literals typed by a
    caller stay strict: floats are refused there.
    """
    if not math.isfinite(value):
        raise ValidationError(f"value_out_of_range:{what}")
    raw = repr(value)
    exact = Decimal(raw)
    rule, number = "binary64-shortest-repr", exact
    if len(exact.as_tuple().digits) > _BINARY64_DIGITS and exact.as_tuple().exponent < 0:
        rounded = Context(prec=_BINARY64_DIGITS, rounding=decimal.ROUND_HALF_EVEN).plus(exact)
        rule, number = "binary64-artifact-rounded-to-15-significant-digits", rounded.normalize()
    number = _to_decimal(number, what)
    return number, {"raw": raw, "raw_type": "float", "rule": rule,
                    "changed_by_normalization": number != exact}


def _new_context() -> Context:
    return Context(prec=_MAX_DIGITS, rounding=decimal.ROUND_HALF_EVEN, Emin=-999, Emax=999,
                   capitals=1, clamp=0, flags=[],
                   traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow])


def evaluate(expression: str, values: Mapping[str, tuple[Decimal, Unit]]
             ) -> tuple[Decimal, Unit, bool, set[str]]:
    """Evaluate ``expression`` over named (Decimal, Unit) values.

    Returns (result, unit, inexact, names_used). Only + - * / unary +/-,
    parentheses, decimal literals and names are accepted; anything else raises
    ExpressionError before any evaluation happens.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("expression_required")
    if len(expression) > _MAX_EXPR:
        raise ExpressionError("expression_too_long")
    text = expression.translate(_UNICODE_OPS).strip()
    if not _EXPR_CHARS.match(text):
        raise ExpressionError("expression_contains_disallowed_characters")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise ExpressionError("expression_syntax_error") from exc

    ctx = _new_context()
    used: set[str] = set()
    nodes = 0

    def walk(node: ast.AST, depth: int) -> tuple[Decimal, Unit]:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            raise ExpressionError("expression_too_complex")
        kind = type(node)
        if kind is ast.Expression:
            return walk(node.body, depth + 1)
        if kind is ast.Constant:
            if type(node.value) not in (int, float):
                raise ExpressionError("disallowed_literal")
            segment = ast.get_source_segment(text, node) or ""
            if not _NUMBER.match(segment) or len(segment) > _MAX_DIGITS:
                raise ExpressionError("disallowed_number_format")
            return Decimal(segment), ()
        if kind is ast.Name:
            if not isinstance(node.ctx, ast.Load) or node.id not in values:
                raise ExpressionError(f"unknown_name:{node.id}")
            used.add(node.id)
            return values[node.id]
        if kind is ast.UnaryOp and type(node.op) in (ast.USub, ast.UAdd):
            number, unit = walk(node.operand, depth + 1)
            return (ctx.minus(number) if type(node.op) is ast.USub else ctx.plus(number)), unit
        if kind is ast.BinOp and type(node.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div):
            left, left_unit = walk(node.left, depth + 1)
            right, right_unit = walk(node.right, depth + 1)
            op = type(node.op)
            try:
                if op in (ast.Add, ast.Sub):
                    if left_unit != right_unit:
                        raise UnitMismatch(
                            f"unit_mismatch:{format_unit(left_unit)}_vs_{format_unit(right_unit)}")
                    return ((ctx.add if op is ast.Add else ctx.subtract)(left, right), left_unit)
                if op is ast.Mult:
                    return ctx.multiply(left, right), _combine(left_unit, right_unit, 1)
                if right == 0:
                    raise ExpressionError("division_by_zero")
                return ctx.divide(left, right), _combine(left_unit, right_unit, -1)
            except decimal.DecimalException as exc:
                raise ExpressionError(f"arithmetic_error:{type(exc).__name__}") from exc
        operator = type(getattr(node, "op", None)).__name__ if hasattr(node, "op") else ""
        raise ExpressionError(f"disallowed_syntax:{kind.__name__}{':' + operator if operator else ''}")

    result, unit = walk(tree, 0)
    if (not result.is_finite() or len(result.as_tuple().digits) > _MAX_DIGITS or
            result.adjusted() > 100 or (result != 0 and result.adjusted() < -100)):
        raise ExpressionError("result_out_of_range")
    return result, unit, bool(ctx.flags[decimal.Inexact]), used


def parse_relations(value: Any) -> list[dict[str, str]]:
    """Declared role relations: ``[{"left", "op", "right", "as"}]`` (at most 10)."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_RELATIONS:
        raise ValidationError("invalid_role_relations")
    relations: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if (not isinstance(item, Mapping) or set(item) != {"left", "op", "right", "as"} or
                item["op"] not in ("+", "-", "*", "/") or
                not all(isinstance(item[key], str) and _ROLE.match(item[key])
                        for key in ("left", "right", "as")) or
                item["left"] == item["right"]):
            raise ValidationError(f"invalid_role_relation:{index}")
        if item["as"] in (item["left"], item["right"]):
            # 'planned - actual as actual' would relabel a difference as one operand.
            raise ValidationError(f"role_relation_result_names_an_operand:{index}")
        if any((other["left"], other["op"], other["right"]) ==
               (item["left"], item["op"], item["right"]) for other in relations):
            raise ValidationError(f"duplicate_role_relation:{index}")
        relations.append({key: item[key] for key in ("left", "op", "right", "as")})
    return relations


def quantity_role(expression: str, roles: Mapping[str, str | None],
                  relations: Sequence[Mapping[str, str]],
                  units: Mapping[str, Unit],
                  negative: Iterable[str] = (),
                  seen: list[tuple[int, ast.AST]] | None = None
                  ) -> tuple[str | None, set[int]]:
    """Role of an (already validated) expression's result, and the relations it used.

    This is the **combination guard and label** of a calculation: it refuses roles
    combined without a declared relation and names the result (a source role or a
    relation's ``as``). It is not the result's quantity type: that is derived by the
    closed type algebra of ``operating.quantity_types`` from the expression tree, and a
    relation's ``as`` is only a label that must be consistent with it. ``seen``, when
    given, receives ``(relation index, node)`` for every relation used, so the caller can
    check each label against the form of the subtree it names.

    ``roles[name]`` is the input's role, ``None`` for a cited input without one, or
    ``""`` for a literal without one. ``units[name]`` is the input's unit;
    ``negative`` names the literal inputs whose value is negative. Units are checked
    by ``evaluate``; this checks roles only.

    Order-independent by construction. Every maximal product/quotient subtree is
    flattened into the multisets of its numerator and denominator factors, so
    reordering or regrouping ``*`` and ``/`` cannot change the answer. A factor is a
    leaf or a sum/difference, typed by:

    * a **role vector**: +1 per role in the numerator, -1 per role in the
      denominator; number literals and unroled leaves (a cited value without a role,
      a literal with a unit, a unit-1 count without a role) add nothing;
    * the **cancelled** roles: roles found in both the numerator and the denominator
      of some product (``p / q``), carried through every enclosing product and sum.

    A product's vector, after a declared ``*`` / ``/`` relation that matches it
    exactly (``{L: 1, R: 1}`` or ``{L: 1, R: -1}`` -> ``{as: 1}``):

    * ``{Y: 1}`` is a quantity of role ``Y``, refused
      (``quantity_role_cancelled_into_other_role``) when any role other than ``Y`` was
      cancelled on the way (``p / q * a`` is not an actual quantity);
    * ``{}`` is a pure ratio or an unroled quantity (no role);
    * only negative entries (``x / p``, per planned unit) has no role;
    * anything else (``planned * planned``, ``planned * actual``) is refused.

    A sum or difference needs identical vectors (a factor made only of numbers is
    neutral), or two single roles with a declared ``+`` / ``-`` relation; signs are
    tracked through unary minus and negative literals (``-p + a`` is ``a - p``), and a
    difference of one role has no known sign beside another role. Cancelled roles
    are united. A result role whose own inputs carry a unit but that ends
    dimensionless (``p / x`` in ``1``) is a bare ratio: no role.

    Raises ``QuantityRoleMismatch``.
    """
    used: set[int] = set()
    negatives = frozenset(negative)

    def vec(items: dict[str, int]) -> dict[str, int]:
        return {role: count for role, count in items.items() if count}

    def show(term: Mapping[str, Any]) -> str:
        vector = term["vector"]
        if not vector:
            return ("number" if term["num"] else
                    f"ratio({'/'.join(sorted(term['cancelled']))})" if term["cancelled"]
                    else "unroled")
        if list(vector.values()) == [1]:
            return next(iter(vector))
        return "*".join(f"{role}^{count}" if count != 1 else role
                        for role, count in sorted(vector.items()))

    def refuse(left: Mapping[str, Any], op: str, right: Mapping[str, Any]
               ) -> QuantityRoleMismatch:
        return QuantityRoleMismatch(
            f"quantity_role_mismatch:{show(left)}{op}{show(right)}: "
            "different quantity roles combine only through a declared relation "
            "(relations: left, op, right, as); give every quantity in a sum or "
            "difference a role; a literal with a unit is a quantity, not a scale")

    def flip(sign: bool | None) -> bool | None:
        return None if sign is None else not sign

    def single(term: Mapping[str, Any]) -> str | None:
        vector = term["vector"]
        return next(iter(vector)) if list(vector.values()) == [1] else None

    def check(term: dict[str, Any], where: str) -> dict[str, Any]:
        """The vector rules every product and sum result must satisfy."""
        vector = term["vector"]
        role = single(term)
        if role is not None:
            other = sorted(term["cancelled"] - {role})
            if other:
                raise QuantityRoleMismatch(
                    f"quantity_role_cancelled_into_other_role:{role}:{','.join(other)}: "
                    f"{where} cancels {', '.join(other)} against itself and leaves "
                    f"a quantity of role {role}; a ratio of one role is not a scale for another "
                    "(declare a relation, or compute the ratio in its own calculation)")
            return term
        if any(count > 0 for count in vector.values()):
            code = ("quantity_role_product_unsupported" if len(vector) == 1
                    else "quantity_role_mismatch")
            raise QuantityRoleMismatch(
                f"{code}:{show(term)}: {where} is no quantity "
                "of one role (a role times itself or times another role needs a declared "
                "relation; a count times a quantity is a count without a role)")
        return term

    # term: vector, cancelled (frozenset), num (only numbers), dimensioned (roles whose
    # leaves carry a unit), negative (None = unknown sign)
    def leaf(node: ast.AST) -> dict[str, Any]:
        if isinstance(node, ast.Constant):
            return {"vector": {}, "cancelled": frozenset(), "num": True,
                    "dimensioned": frozenset(), "negative": False}
        role, unit = roles[node.id], units[node.id]
        return {"vector": {role: 1} if role else {}, "cancelled": frozenset(),
                "num": role == _NEUTRAL and not unit,
                "dimensioned": frozenset({role}) if role and unit else frozenset(),
                "negative": node.id in negatives}

    def factors(node: ast.AST, power: int, out: list[tuple[int, ast.AST]],
                sign: list[bool]) -> None:
        """Flatten a product subtree into (power, factor) pairs and its sign flips."""
        if isinstance(node, ast.BinOp) and _OPS[type(node.op)] in "*/":
            factors(node.left, power, out, sign)
            factors(node.right, power if _OPS[type(node.op)] == "*" else -power, out, sign)
        elif isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                sign[0] = not sign[0]
            factors(node.operand, power, out, sign)
        else:
            out.append((power, node))

    def product(node: ast.AST) -> dict[str, Any]:
        pairs: list[tuple[int, ast.AST]] = []
        flipped = [False]
        factors(node, 1, pairs, flipped)
        vector: dict[str, int] = {}
        above: set[str] = set()
        below: set[str] = set()
        cancelled: set[str] = set()
        dimensioned: set[str] = set()
        negative: bool | None = flipped[0]
        num = True
        for power, factor in pairs:
            term = walk(factor)
            num = num and term["num"]
            cancelled |= term["cancelled"]
            dimensioned |= term["dimensioned"]
            negative = (None if negative is None or term["negative"] is None
                        else negative != term["negative"])
            for role, count in term["vector"].items():
                vector[role] = vector.get(role, 0) + power * count
                (above if power * count > 0 else below).add(role)
        cancelled |= above & below
        vector = vec(vector)
        if single({"vector": vector}) is None and any(c > 0 for c in vector.values()):
            for index, item in enumerate(relations):
                want = ({item["left"]: 1, item["right"]: 1} if item["op"] == "*" else
                        {item["left"]: 1, item["right"]: -1} if item["op"] == "/" else None)
                if want == vector:
                    used.add(index)
                    if seen is not None:
                        seen.append((index, node))
                    vector = {item["as"]: 1}
                    break
        return check({"vector": vector, "cancelled": frozenset(cancelled), "num": num,
                      "dimensioned": frozenset(dimensioned), "negative": negative},
                     "this product")

    def addition(node: ast.BinOp) -> dict[str, Any]:
        left, right = walk(node.left), walk(node.right)
        op = _OPS[type(node.op)]
        right = {**right, "negative": flip(right["negative"]) if op == "-"
                 else right["negative"]}
        if right["num"]:
            return left
        if left["num"]:
            return right
        cancelled = left["cancelled"] | right["cancelled"]
        dimensioned = left["dimensioned"] | right["dimensioned"]
        if left["vector"] == right["vector"]:
            same = left["negative"] is not None and left["negative"] == right["negative"]
            return check({"vector": left["vector"], "cancelled": cancelled, "num": False,
                          "dimensioned": dimensioned,
                          "negative": left["negative"] if same else None}, "this sum")
        lrole, rrole = single(left), single(right)
        if lrole is None or rrole is None:
            raise refuse(left, op, right)
        lneg, rneg = left["negative"], right["negative"]
        if lneg is None or rneg is None:
            raise QuantityRoleMismatch(
                f"quantity_role_sign_unknown:{lrole}{op}{rrole}: a difference of one role "
                "has no known sign, so it cannot be matched to a relation with another "
                "role; write the combination as one declared relation per pair")
        if lneg and rneg:
            shown, negative = (left, "+", right), True
        elif lneg:
            shown, negative = (right, "-", left), False
        else:
            shown, negative = (left, "-" if rneg else "+", right), False
        result = None
        a, sym, b = single(shown[0]), shown[1], single(shown[2])
        for index, item in enumerate(relations):
            if item["op"] == sym and ((item["left"], item["right"]) == (a, b) or
                                      sym == "+" and (item["left"], item["right"]) == (b, a)):
                used.add(index)
                if seen is not None:
                    seen.append((index, node))
                result = item["as"]
                break
        if result is None:
            raise refuse(*shown)
        return check({"vector": {result: 1}, "cancelled": cancelled, "num": False,
                      "dimensioned": dimensioned, "negative": negative}, "this sum")

    def walk(node: ast.AST) -> dict[str, Any]:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BinOp) and _OPS[type(node.op)] in "+-":
            return addition(node)
        if isinstance(node, (ast.BinOp, ast.UnaryOp)):
            return product(node)
        return leaf(node)

    def units_of_result(node: ast.AST) -> Unit:
        if isinstance(node, ast.Expression):
            return units_of_result(node.body)
        if isinstance(node, ast.Constant):
            return ()
        if isinstance(node, ast.Name):
            return units[node.id]
        if isinstance(node, ast.UnaryOp):
            return units_of_result(node.operand)
        op = _OPS[type(node.op)]
        if op in "+-":
            return units_of_result(node.left)
        return _combine(units_of_result(node.left), units_of_result(node.right),
                        1 if op == "*" else -1)

    tree = _qt.parse(expression)[1]
    result = walk(tree)
    if result["num"]:
        return _NEUTRAL, used
    role = single(result)
    if role is None:
        return None, used
    if not units_of_result(tree) and role in result["dimensioned"]:
        return None, used  # a role turned dimensionless by an unroled quantity: a ratio
    return role, used


# --------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------

HeaderGate = Callable[[Sequence[str], Actor], bool]


def _visible(case: Mapping[str, Any], actor: Actor,
             header_allows: Callable[[tuple[str, ...]], bool] | None = None) -> bool:
    """Tenant match, clearance >= classification, audience ∩ principals and, for a
    case with recorded header provenance, a current grant on at least one relation
    of every header identifier (``header_allows``; absent = not visible).

    Unlike an ontology ACL, a ``*`` audience entry grants nothing here.
    """
    if case["tenant"] != actor.tenant:
        return False
    if LEVELS[case["classification"]] > LEVELS[actor.clearance]:
        return False
    if not set(case["audience"]) & set(actor.principals):
        return False
    groups = case.get("header_relations")
    if not groups:
        return True
    return header_allows is not None and all(header_allows(tuple(group)) for group in groups)


def _header_relations(value: Any) -> list[list[str]]:
    """Validated header provenance: one non-empty, sorted relation set per identifier."""
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ValidationError("invalid_header_relations")
    groups: list[list[str]] = []
    for group in value:
        if isinstance(group, (str, bytes, Mapping)) or not isinstance(group, Iterable):
            raise ValidationError("invalid_header_relations")
        relations = set()
        for relation in group:
            if not isinstance(relation, str) or _RELATION.match(relation) is None:
                raise ValidationError("invalid_header_relation")
            relations.add(relation)
        if not relations or len(relations) > _MAX_HEADER_RELATIONS:
            raise ValidationError("invalid_header_relations")
        if sorted(relations) not in groups:
            groups.append(sorted(relations))
    if len(groups) > _MAX_HEADER_GROUPS:
        raise ValidationError("too_many_header_relations")
    return groups


def _check_actor(actor: Any) -> Actor:
    if not isinstance(actor, Actor):
        raise ValidationError("actor_required")
    return actor


def claim_holder(value: Any) -> dict[str, str]:
    """A claim holder: ``session`` (a session's SHA-256 reference) and ``subject``."""
    if not isinstance(value, Mapping) or set(value) != {"session", "subject"} \
            or not isinstance(value["session"], str) or not _SESSION_REF.match(value["session"]):
        raise ValidationError("invalid_claim_holder")
    return {"session": value["session"], "subject": _text(value["subject"], "claim_holder", 500)}


# The holder the current write runs as (``writing_as``); None names no holder.
_WRITER: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "case_writer", default=None)


@contextmanager
def writing_as(holder: Mapping[str, Any] | None) -> Iterator[None]:
    """Run case writes as ``holder`` (a session host wraps each of its calls)."""
    token = _WRITER.set(None if holder is None else claim_holder(holder))
    try:
        yield
    finally:
        _WRITER.reset(token)


def _same_principal(holder: Mapping[str, str], other: Mapping[str, str] | None) -> bool:
    """Whether ``other`` is ``holder``: the same session, or a session of the same
    subject when that subject is one employee (a group subject names no one person)."""
    return other is not None and (holder == other or (
        holder["subject"] == other["subject"]
        and _EMPLOYEE_SUBJECT.fullmatch(holder["subject"]) is not None))


def _claim_step(claim: Mapping[str, Any] | None, kind: str, payload: Mapping[str, Any],
                at: str) -> dict[str, Any]:
    """The claim after one claim event at ``at``. A transition changes who holds the
    case (taken, taken over after expiry, released or broken); a renewal is none. Every
    break stays in ``breaks``; the first claim after a break (``since_break``) taken by
    the broken principal is recorded on it (``reclaimed``)."""
    held = (claim is not None and claim["holder"] is not None
            and datetime.fromisoformat(at) < datetime.fromisoformat(claim["expires_at"]))
    same = held and claim["holder"] == payload["holder"]
    transitions = 0 if claim is None else claim["transitions"]
    breaks = [] if claim is None else [dict(item) for item in claim.get("breaks", ())]
    since_break = claim is not None and claim.get("since_break", False)
    if kind == "case.claimed":
        if since_break and _same_principal(breaks[-1]["holder"], payload["holder"]):
            breaks[-1]["reclaimed"] = {"at": at, "holder": payload["holder"]}
        return {"holder": payload["holder"], "expires_at": payload["expires_at"],
                "transitions": transitions + (0 if same else 1),
                **({"breaks": breaks} if breaks else {})}
    # Only the holder, its employee restarted or a break ends an active claim.
    ended = {"holder": None, "expires_at": None, "transitions": transitions + int(held),
             "since_break": since_break}
    if kind == "case.claim_broken":
        breaks.append({"holder": payload["holder"], "reason": payload["reason"],
                       "approver": payload["approver"],
                       "author_kind": payload.get("author_kind"), "at": at})
        ended["since_break"] = True
    return {**ended, **({"breaks": breaks} if breaks else {})}


# What one case of an objective may refuse; any other error is no case's answer.
_CASE_REFUSALS = (CaseNotFound, CaseIntegrityError, ValidationError)


def _refusal(exc: Exception) -> str:
    """A case's refusal code, as its one-case call reports it to a client."""
    if isinstance(exc, CaseNotFound):
        return "case_not_found"
    return "case_integrity_error" if isinstance(exc, CaseIntegrityError) else str(exc)


def objective_cases(case_ids: Any) -> list[str]:
    """An objective's cases in the one order every claimant takes them: sorted. Refused
    unless 1..``MAX_OBJECTIVE_CASES`` distinct case ids."""
    if not isinstance(case_ids, (list, tuple)) \
            or not 1 <= len(case_ids) <= MAX_OBJECTIVE_CASES \
            or not all(isinstance(case_id, str) and case_id for case_id in case_ids):
        raise ValidationError("invalid_objective_cases")
    if len(set(case_ids)) != len(case_ids):
        raise ValidationError("duplicate_objective_case")
    return sorted(case_ids)


def each_objective_case(case_ids: Any,
                        step: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """``step(case_id)`` on every case of an objective in order, a release or a break:
    a case that refuses is listed (``refused``: case_id, error) and never keeps the
    others; ``summary`` counts the cases whose lease it ended (``released``) and those
    that refused. When every case refuses, ``ObjectiveRefused`` names the first."""
    done: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    for case_id in objective_cases(case_ids):
        try:
            done.append(step(case_id))
        except _CASE_REFUSALS as exc:
            refused.append({"case_id": case_id, "error": _refusal(exc)})
    if not done:
        raise ObjectiveRefused(refused[0]["case_id"], refused[0]["error"])
    return {"cases": done, "refused": refused,
            "summary": {"released": len(done), "refused": len(refused)}}


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS case_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  case_seq INTEGER NOT NULL CHECK(case_seq >= 1),
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL UNIQUE,
  UNIQUE(case_id, case_seq)
);
CREATE INDEX IF NOT EXISTS idx_case_event_kind ON case_event(kind, seq);
CREATE INDEX IF NOT EXISTS idx_case_event_case_kind ON case_event(case_id, kind);
CREATE TRIGGER IF NOT EXISTS case_event_no_update BEFORE UPDATE ON case_event
BEGIN SELECT RAISE(ABORT, 'case_event_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS case_event_no_delete BEFORE DELETE ON case_event
BEGIN SELECT RAISE(ABORT, 'case_event_is_append_only'); END;
"""
# Every object ``_DDL`` creates: a file holding them all needs no DDL at open.
_DDL_OBJECTS = frozenset(re.findall(
    r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|VIEW|TRIGGER)\s+IF\s+NOT\s+EXISTS\s+(\w+)",
    _DDL))


# Where each session's read position lives: beside the case store, never in it (the case
# log and its tables hold the case's record only).
_CURSOR_DDL = ("CREATE TABLE IF NOT EXISTS case_cursor (case_id TEXT NOT NULL, "
               "session_ref TEXT NOT NULL, case_seq INTEGER NOT NULL, "
               "PRIMARY KEY (case_id, session_ref))")

# Findings a session sees on opening a case besides what it has not read, and the most
# unread ones it is shown (the rest are counted).
TAIL_FINDINGS = 10
MAX_UNREAD_SHOWN = 20

_LIST_SQL = """
SELECT o.seq, o.case_id, o.payload_json, o.at AS opened_at,
  (SELECT COUNT(*) FROM case_event e WHERE e.case_id = o.case_id) AS events,
  (SELECT at FROM case_event e WHERE e.case_id = o.case_id
     ORDER BY case_seq DESC LIMIT 1) AS updated_at,
  EXISTS(SELECT 1 FROM case_event c WHERE c.case_id = o.case_id AND c.kind = 'case.closed')
    AS closed
FROM case_event o WHERE o.kind = 'case.opened' AND o.seq < ?
ORDER BY o.seq DESC LIMIT ?
"""

# The claim events of a page of cases (``?`` per case id), in one query.
_CLAIMS_SQL = ("SELECT case_id, kind, at, payload_json FROM case_event WHERE kind IN ("
               + ",".join(f"'{kind}'" for kind in CLAIM_KINDS) + ") AND case_id IN ({}) "
               "ORDER BY case_id, case_seq")



def bind_cited_unit(contracts: Any, handle: str, resolved: Mapping[str, Any], field: str,
                    name: str, declared: Any) -> tuple[Unit, dict[str, Any]]:
    """The one input-binding rule for a cited number's unit.

    A source unit contract covering the cited column fixes the unit (declaring
    it is optional; a different declared unit is refused); otherwise the unit is
    the caller's declaration. Returns the unit and the basis fields to record.
    Shared by ``CaseStore.compute`` and ``operating.landed_cost``.
    """
    contract = contracts(handle, resolved, field) if contracts is not None else None
    if contract is None or contract.get("unit") is None:
        return parse_unit(declared), {"unit_basis": "caller_declared"}
    contract_unit = parse_unit(contract["unit"])
    if declared is not None and parse_unit(declared) != contract_unit:
        raise UnitMismatch(
            f"unit_contradicts_source_contract:{name}:declared_"
            f"{format_unit(parse_unit(declared))}_but_{contract['id']}"
            f"_records_{format_unit(contract_unit)}")
    return contract_unit, {"unit_basis": "source_contract",
                           "unit_contract": str(contract["id"]),
                           "unit_contract_digest": _sha(_canon(contract))}


def bind_role(contracts: Any, handle: str | None, resolved: Mapping[str, Any] | None,
              field: str | None, name: str, declared: Any) -> dict[str, Any]:
    """The input-binding rule for a quantity role (same shape as ``bind_cited_unit``).

    A source contract that pins the cited column's role fixes it (a different
    declared role is refused); otherwise the role is the caller's declaration,
    or absent. Returns the fields to record on the input.
    """
    if declared is not None and (not isinstance(declared, str) or not _ROLE.match(declared)):
        raise ValidationError(f"invalid_quantity_role:{name}")
    contract = (contracts(handle, resolved, field)
                if contracts is not None and handle is not None else None)
    pinned = contract.get("role") if contract else None
    if pinned is None:
        return {} if declared is None else {"role": declared, "role_basis": "caller_declared"}
    if declared is not None and declared != pinned:
        raise QuantityRoleMismatch(f"role_contradicts_source_contract:{name}:declared_"
                                   f"{declared}_but_{contract['id']}_records_{pinned}")
    return {"role": pinned, "role_basis": "source_contract", "role_contract": str(contract["id"])}


class CaseStore:
    """Durable case journal. Safe for concurrent writers across connections/processes."""

    def __init__(self, path: str | Path, *, citation_resolver: CitationResolver | None = None,
                 resolver_errors: tuple[type[BaseException], ...] = (
                     CitationError, LookupError, PermissionError),
                 clock: Callable[[], datetime] | None = None,
                 unit_contracts: UnitContractLookup | None = None,
                 link_contracts: Any = None, header_gate: HeaderGate | None = None):
        self.path = str(path)
        # header_gate(relations, actor): may ``actor`` read at least one of these
        # source relations now? Required to open (and to read) a case that records
        # header provenance; see the module docstring.
        self._header_gate = header_gate
        self._link_contracts = link_contracts  # operating.case_links.LinkContracts
        self._resolver = citation_resolver
        self._unit_contracts = unit_contracts
        self._resolver_errors = tuple(resolver_errors)
        self._clock = clock or _now
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise CaseIntegrityError("case_store_schema_newer_than_code")
            # A current file is opened without a write (no lock wait behind another
            # process's write); a fresh or older one is created or upgraded.
            have = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            if (version != _SCHEMA_VERSION or not _DDL_OBJECTS <= have or str(db.execute(
                    "PRAGMA journal_mode").fetchone()[0]).lower() != "wal"):
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(_DDL)
                db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        finally:
            db.close()

    # ---- plumbing -----------------------------------------------------

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            if not write:
                yield db
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                self._before_commit()
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def _before_commit(self) -> None:
        """Seam for crash-safety tests: raising here must leave no event behind."""

    def _moment(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValidationError("clock_must_be_timezone_aware")
        return moment.astimezone(timezone.utc)

    def _at(self) -> str:
        return self._moment().isoformat(timespec="microseconds")

    @staticmethod
    def _event_hash(case_id: str, case_seq: int, kind: str, actor: str, at: str,
                    payload_json: str, prev_hash: str) -> str:
        return _sha(_canon({"case_id": case_id, "case_seq": case_seq, "kind": kind,
                            "actor": actor, "at": at, "payload": payload_json,
                            "prev_hash": prev_hash}))

    def _append(self, db: sqlite3.Connection, case_id: str, kind: str, actor: str,
                payload: Mapping[str, Any]) -> dict[str, Any]:
        last = db.execute("SELECT case_seq, event_hash FROM case_event WHERE case_id=? "
                          "ORDER BY case_seq DESC LIMIT 1", (case_id,)).fetchone()
        case_seq = (last["case_seq"] + 1) if last else 1
        prev_hash = last["event_hash"] if last else _GENESIS
        payload_json = _canon(payload)
        if len(payload_json.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValidationError("event_payload_too_large")
        at = self._at()
        event_hash = self._event_hash(case_id, case_seq, kind, actor, at, payload_json, prev_hash)
        db.execute("INSERT INTO case_event(case_id,case_seq,kind,actor,at,payload_json,"
                   "prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                   (case_id, case_seq, kind, actor, at, payload_json, prev_hash, event_hash))
        return {"case_id": case_id, "seq": case_seq, "kind": kind, "actor": actor, "at": at}

    def _header_allows(self, actor: Actor) -> Callable[[tuple[str, ...]], bool]:
        """The header gate for one read by ``actor``, memoised per relation set so a
        list over many cases asks the gate once per distinct set."""
        memo: dict[tuple[str, ...], bool] = {}
        gate = self._header_gate

        def allows(relations: tuple[str, ...]) -> bool:
            if relations not in memo:
                memo[relations] = gate is not None and bool(gate(relations, actor))
            return memo[relations]

        return allows

    def _events(self, db: sqlite3.Connection, case_id: str) -> list[sqlite3.Row]:
        return db.execute("SELECT * FROM case_event WHERE case_id=? ORDER BY case_seq",
                          (case_id,)).fetchall()

    # ---- projection ---------------------------------------------------

    def _fold(self, case_id: str, rows: Sequence[sqlite3.Row]) -> dict[str, Any] | None:
        """Pure projection of a case's events. Returns None when there are none."""
        if not rows:
            return None
        state: dict[str, Any] = {}
        prev = _GENESIS
        chain_ok = True
        for index, row in enumerate(rows, 1):
            if (row["case_seq"] != index or row["prev_hash"] != prev or row["event_hash"] !=
                    self._event_hash(case_id, index, row["kind"], row["actor"], row["at"],
                                     row["payload_json"], row["prev_hash"])):
                chain_ok = False
            prev = row["event_hash"]
            kind, payload = row["kind"], json.loads(row["payload_json"])
            ref: str | None = None
            if index == 1:
                if kind != "case.opened":
                    raise CaseIntegrityError("case_missing_open_event")
                state = {
                    "case_id": case_id, "tenant": payload["tenant"],
                    "classification": payload["classification"],
                    "audience": payload["audience"], "owner": payload["owner"],
                    "question": payload["question"], "roots": payload["roots"],
                    "header_relations": payload.get("header_relations"),
                    "objective_author_kind": payload.get("objective_author_kind"),
                    "status": "open", "opened_at": row["at"], "closed_at": None,
                    "closed_by": None, "outcome": None,
                    "findings": {}, "calculations": [], "gaps": [], "proposals": [],
                    "verifications": [], "links": [], "timeline": [],
                    "claim": None, "handoff": None,
                }
            elif kind == "finding.recorded":
                ref = payload["finding_id"]
                state["findings"][ref] = {
                    "id": ref, "kind": payload["kind"], "status": payload["status"],
                    "statement": payload["statement"], "citations": payload["citations"],
                    "label": FACT_LABEL if payload["kind"] == "fact" else INTERPRETATION_LABEL,
                    "recorded_by": row["actor"], "at": row["at"], "seq": index,
                    "superseded": None}
                if "claim" in payload:
                    state["findings"][ref]["claim"] = payload["claim"]
                if "value_claims" in payload:
                    state["findings"][ref]["value_claims"] = payload["value_claims"]
                if payload.get("statement_by") == "workbench":
                    state["findings"][ref]["statement_by"] = "workbench"
                if payload.get("author_kind") in AUTHOR_KINDS:
                    state["findings"][ref]["author_kind"] = payload["author_kind"]
            elif kind == "link.recorded":
                ref = payload["link_id"]
                state["links"].append({**payload, "id": ref, "recorded_by": row["actor"],
                                       "at": row["at"], "seq": index})
            elif kind == "finding.superseded":
                ref = payload["finding_id"]
                state["findings"][ref]["superseded"] = {
                    "reason": payload["reason"], "replaced_by": payload.get("replaced_by"),
                    "by": row["actor"], "at": row["at"], "seq": index}
            elif kind == "calculation.recorded":
                ref = payload["calc_id"]
                state["calculations"].append({**payload, "id": ref, "recorded_by": row["actor"],
                                              "at": row["at"], "seq": index})
            elif kind == "gap.recorded":
                ref = payload["gap_id"]
                state["gaps"].append({**payload, "id": ref, "recorded_by": row["actor"],
                                      "at": row["at"], "seq": index})
            elif kind == "proposal.recorded":
                ref = payload["proposal_id"]
                state["proposals"].append({**payload, "id": ref, "recorded_by": row["actor"],
                                           "at": row["at"], "seq": index})
            elif kind == "verification.recorded":
                ref = payload["verification_id"]
                state["verifications"].append({**payload, "id": ref,
                                               "recorded_by": row["actor"],
                                               "at": row["at"], "seq": index})
            elif kind == "case.closed":
                state.update(status="closed", closed_at=row["at"], closed_by=row["actor"],
                             outcome=payload["outcome"])
            elif kind in CLAIM_KINDS:
                state["claim"] = _claim_step(state["claim"], kind, payload, row["at"])
                if "handoff" in payload:
                    state["handoff"] = {"note": payload["handoff"], "by": payload["holder"],
                                        "at": row["at"], "seq": index,
                                        "author_kind": payload.get("author_kind")}
            state["timeline"].append({"seq": index, "at": row["at"], "actor": row["actor"],
                                      "kind": kind, "ref": ref})
        state["chain_ok"] = chain_ok
        state["last_seq"] = len(rows)
        state["updated_at"] = rows[-1]["at"]
        return state

    def _state(self, db: sqlite3.Connection, actor: Actor, case_id: str) -> dict[str, Any]:
        if not isinstance(case_id, str) or not case_id:
            raise CaseNotFound("case_not_found")
        state = self._fold(case_id, self._events(db, case_id))
        if state is None or not _visible(state, actor, self._header_allows(actor)):
            raise CaseNotFound("case_not_found")
        return state

    def _readable(self, actor: Actor, case_id: str) -> dict[str, Any]:
        with self._connect() as db:
            return self._state(db, actor, case_id)

    def _claim_view(self, claim: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """A folded claim as of now (expiry is judged at read time); None: never claimed."""
        if claim is None:
            return None
        active = (claim["holder"] is not None
                  and self._moment() < datetime.fromisoformat(claim["expires_at"]))
        return {"holder": claim["holder"], "expires_at": claim["expires_at"],
                "active": active, "transitions": claim["transitions"],
                **({"breaks": [dict(item) for item in claim["breaks"]]}
                   if claim.get("breaks") else {})}

    def _refuse_claimed(self, state: Mapping[str, Any], holder: Mapping[str, str] | None,
                        release: bool = False) -> None:
        """``CaseClaimed`` while another holder's claim is active; a ``release`` is also
        accepted from the holder's own employee in another session (a restart)."""
        claim = self._claim_view(state["claim"])
        if claim is not None and claim["active"] and claim["holder"] != holder and not (
                release and _same_principal(claim["holder"], holder)):
            raise CaseClaimed(f"case_claimed_until:{claim['expires_at']}")

    # ---- citations ----------------------------------------------------

    def _resolve_strict(self, handle: str, actor: Actor) -> Mapping[str, Any]:
        if self._resolver is None:
            raise CitationUnresolvable("citation_resolver_not_configured")
        try:
            resolved = self._resolver(handle, actor)
        except self._resolver_errors as exc:
            if isinstance(exc, CitationUnresolvable) and getattr(exc, "public", False) is True:
                raise  # e.g. source_not_configured: host configuration, not an oracle
            raise CitationUnresolvable("citation_unresolvable") from exc
        if not isinstance(resolved, Mapping):
            raise CitationUnresolvable("citation_resolver_returned_non_mapping")
        return resolved

    def _current_digest(self, handle: str, actor: Actor) -> str | None:
        """Read-side digest of what the citation resolves to now, or None.

        Fail closed; never say why, so denial is not an oracle.
        """
        try:
            return _digest(self._resolve_strict(handle, actor))
        except CitationUnresolvable:
            return None
        except Exception:  # noqa: BLE001 - resolver bugs must not break or leak a read
            _LOG.warning("citation resolver failed unexpectedly", exc_info=True)
            return None

    def _cite(self, handles: Any, actor: Actor, required: bool,
              refuse_changed: bool = False) -> list[dict[str, str]]:
        if isinstance(handles, str) or not isinstance(handles, Iterable):
            raise ValidationError("invalid_citations")
        unique = list(dict.fromkeys(_handle(item) for item in handles))
        if len(unique) > _MAX_CITATIONS:
            raise ValidationError("too_many_citations")
        if required and not unique:
            raise ValidationError("fact_requires_citation")
        cited = []
        for handle in unique:
            resolved = self._resolve_strict(handle, actor)
            if refuse_changed and _changed_since(resolved):
                raise ValidationError("citation_changed_since: the cited version is no longer "
                                      "current; cite its current_citation")
            cited.append({"handle": handle, "digest": _digest(resolved)})
        return cited

    # ---- writes -------------------------------------------------------

    def _write(self, actor: Actor, case_id: str,
               build: Callable[[sqlite3.Connection, dict[str, Any]], Any], *,
               claim_checked: bool = True, db: sqlite3.Connection | None = None) -> Any:
        """Run ``build(db, state)`` in one IMMEDIATE transaction on a writable case:
        open, chain intact, and not under another holder's active claim (unless
        ``claim_checked`` is False: ``build`` checks the claim itself). Given ``db``,
        it runs inside that open write transaction (one spanning several cases)."""
        if db is None:
            with self._connect(write=True) as db:
                return self._write(actor, case_id, build, claim_checked=claim_checked, db=db)
        state = self._state(db, actor, case_id)
        if not state["chain_ok"]:
            raise CaseIntegrityError("case_event_chain_broken")
        if state["status"] == "closed":
            raise CaseClosed("case_closed")
        if claim_checked:
            self._refuse_claimed(state, _WRITER.get())
        return build(db, state)

    def _mutate(self, actor: Actor, case_id: str, kind: str,
                make: Callable[[dict[str, Any]], dict[str, Any]], *,
                claim_checked: bool = True) -> dict[str, Any]:
        def build(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
            payload = make(state)
            event = self._append(db, case_id, kind, actor_label(actor), payload)
            return {**event, **{key: value for key, value in payload.items()
                                if key.endswith("_id")}}

        return self._write(actor, case_id, build, claim_checked=claim_checked)

    def open(self, actor: Actor, question: str, roots: Sequence[str] = (),
             audience: Iterable[str] | None = None,
             header_relations: Iterable[Iterable[str]] | None = None,
             objective_author_kind: str | None = None) -> str:
        """Open a case. Audience = actor principals ∩ requested set; never ``*``.

        ``header_relations``: per identifier in the question/roots, the source
        relations that hold it (see the module docstring); the opener must be
        allowed on each now, or the case would be unreadable from its first event.

        ``objective_author_kind``: who wrote the question, as the channel that opened
        the case knows it (``OBJECTIVE_AUTHOR_KINDS``): ``human`` only from a human
        session (an actor with no agent or service principal; the
        ``operating.entitlements`` rule), ``agent`` for a question an agent supplied
        (MCP ``plx.case_open``), ``unknown`` otherwise. Not given = unknown, and
        unknown fails closed (``operating.injection_guard.human_objective``: only a
        human-written question can be the source of an action parameter).
        """
        _check_actor(actor)
        if (objective_author_kind is not None
                and objective_author_kind not in OBJECTIVE_AUTHOR_KINDS):
            raise ValidationError("invalid_objective_author_kind")
        if objective_author_kind == "human" and any(
                str(p).strip().lower().startswith(_AGENT_PREFIXES) for p in actor.principals):
            raise ValidationError("human_objective_needs_a_human_session")
        groups = [] if header_relations is None else _header_relations(header_relations)
        question = _text(question, "question")
        roots = _strings(() if roots is None else roots, "roots", _MAX_ROOTS)
        held = set(actor.principals) - {"*"}
        if audience is None:
            requested = None
            members = held
        else:
            if isinstance(audience, str) or not isinstance(audience, Iterable):
                raise ValidationError("invalid_audience")
            requested = {_text(item, "audience_principal", 200) for item in audience}
            if "*" in requested:
                raise ValidationError("wildcard_audience_not_allowed")
            members = held & requested
        if not members:
            raise ValidationError("audience_empty_after_intersection_with_actor_principals")
        if groups:
            if self._header_gate is None:
                raise ValidationError("header_gate_not_configured")
            if not all(map(self._header_allows(actor), map(tuple, groups))):
                raise ValidationError("case_header_not_readable_by_actor")
        case_id = "case-" + uuid4().hex
        payload = {"question": question, "roots": roots, "tenant": actor.tenant,
                   "classification": actor.clearance, "owner": actor_label(actor),
                   "audience": sorted(members),
                   "audience_requested": None if requested is None else sorted(requested)}
        if groups:  # absent on cases without header provenance (unchanged payload)
            payload["header_relations"] = groups
        if objective_author_kind is not None:  # absent = unknown (unchanged payload)
            payload["objective_author_kind"] = objective_author_kind
        with self._connect(write=True) as db:
            self._append(db, case_id, "case.opened", actor_label(actor), payload)
        return case_id

    def finding(self, actor: Actor, case_id: str, statement: str, kind: str, status: str,
                citations: Iterable[str] = (),
                relation_links: Sequence[str] | None = None,
                value_claims: Sequence[Mapping[str, Any]] | None = None,
                workbench_statement: bool = False,
                author_kind: str | None = None, merge: bool = True) -> dict[str, Any]:
        """Record a finding. ``relation_links`` makes it a relationship claim.

        Sessions working one case at once report the same finding more than once: a plain
        finding (no relation links, no value claims) whose key (``finding_key``: case, kind,
        status, statement ignoring case and spacing, cited handles) equals that of a finding
        not superseded, and whose citations resolve to the content that finding cited (each
        handle's digest, compared as a link's repeat check compares them), is not recorded
        again; the receipt is the existing finding's, with ``merged`` true. A re-check on
        changed row content is a new finding: the old one reads ``citation_content_changed``.
        The check runs inside the write transaction, so two writers racing record it once.
        A different status is a different finding (both are kept), and
        ``merge=False`` (a correction that supersedes) always records. It never folds across
        author kinds (a person's repeat of an agent's finding stays a separate human finding,
        which an agent may not supersede) nor Workbench-composed text into caller text.
        ``author_kind``: who wrote the statement (``human`` or ``agent``), as the
        session's channel decides it (``operating.session_identity``); absent is unknown.

        A relationship claim names the links (``link``) it rests on; their
        citations join the finding's, and its status is checked against them:
        ``supported`` needs every link verified, ``contradicted`` at least one
        contradicted link, and either needs each link's rows to resolve now to
        the content that was checked. ``unknown`` is always allowed. The
        statement itself stays the caller's text: it is not checked against the
        rows or values the links cover (``claim.statement_by: caller``).
        """
        _check_actor(actor)
        statement = _text(statement, "statement")
        if author_kind is not None and author_kind not in AUTHOR_KINDS:
            raise ValidationError("invalid_author_kind")
        if kind not in FINDING_KINDS:
            raise ValidationError("invalid_finding_kind")
        if status not in FINDING_STATUSES:
            raise ValidationError("invalid_finding_status")
        state = self._readable(actor, case_id)
        if isinstance(citations, str) or not isinstance(citations, (Iterable, type(None))):
            raise ValidationError("invalid_citations")
        handles = list(() if citations is None else citations)
        claim, links = None, []
        if relation_links is not None:
            claim, links = self._relation_claim(state, relation_links, kind, status)
            handles += [cite["handle"] for link in links for cite in link["citations"]]
        # A resolver may mark a citation whose source moved on since the cited
        # version (live sources: ``changed_since``); it is never support.
        cited = self._cite(handles, actor, required=kind == "fact",
                           refuse_changed=status == "supported")
        values = _value_claims(value_claims, cited, state)
        if links:
            now = {cite["handle"]: cite["digest"] for cite in cited}
            changed = sorted({link["id"] for link in links for cite in link["citations"]
                              if now.get(cite["handle"]) != cite["digest"]})
            if changed and status != "unknown":
                raise ValidationError("relation_link_content_changed_since_checked:"
                                      + ",".join(changed))

        def make(state: dict[str, Any]) -> dict[str, Any]:
            record = {"finding_id": f"F{len(state['findings']) + 1}", "kind": kind,
                      "status": status, "statement": statement, "citations": cited}
            if claim is not None:
                record["claim"] = claim
            if values:
                record["value_claims"] = values
            if workbench_statement is True:
                # Text the Workbench composed from what it read (an attached original's
                # metadata, a mail header), never caller text; set only by tool handlers.
                record["statement_by"] = "workbench"
            if author_kind is not None:
                record["author_kind"] = author_kind
            return record

        key = finding_key(case_id, kind, status, statement, [cite["handle"] for cite in cited])
        plain = merge and claim is None and not values
        workbench = workbench_statement is True

        def build(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
            if plain:
                for found in state["findings"].values():
                    if found["superseded"] is None \
                            and found.get("author_kind") == author_kind \
                            and found["recorded_by"] == actor_label(actor) \
                            and (found.get("statement_by") == "workbench") == workbench \
                            and finding_key(
                            case_id, found["kind"], found["status"], found["statement"],
                            [cite["handle"] for cite in found["citations"]]) == key \
                            and _same_citations(found["citations"], cited):
                        return {"case_id": case_id, "seq": found["seq"],
                                "kind": "finding.recorded", "actor": found["recorded_by"],
                                "at": found["at"], "finding_id": found["id"], "merged": True}
            payload = make(state)
            event = self._append(db, case_id, "finding.recorded", actor_label(actor), payload)
            return {**event, "finding_id": payload["finding_id"], "merged": False}

        receipt = self._write(actor, case_id, build)
        receipt["key"] = key
        receipt["label"] = FACT_LABEL if kind == "fact" else INTERPRETATION_LABEL
        if claim is not None:
            receipt["claim"] = claim
        return receipt

    def read_tail(self, actor: Actor, case_id: str, session_ref: str,
                  view: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """What a session sees of the case's journal when it opens the case: the newest
        ``TAIL_FINDINGS`` findings and the findings recorded since this session last opened
        it (``unread``, at most ``MAX_UNREAD_SHOWN`` shown, ``unread_total`` counted), from
        the reader's own ``get`` view (``view`` when the caller already holds it), so a
        finding the reader cannot read stays withheld. Moves the session's read position to
        the view's newest event (the case log is not written; the position is kept in ``<store>.cursors``); ``resumed`` is true
        when the session had opened the case before."""
        _check_actor(actor)
        if not isinstance(session_ref, str) or not session_ref:
            raise ValidationError("invalid_session_ref")
        view = self.get(actor, case_id) if view is None else view
        last = view["integrity"]["events"]
        items = sorted((item for bucket in view["findings"].values() for item in bucket),
                       key=lambda item: item["seq"])
        brief = [{key: item[key] for key in ("id", "kind", "status", "statement", "seq",
                                             "recorded_by", "at", "content") if key in item}
                 for item in items]
        db = sqlite3.connect(self.path + ".cursors", timeout=30, isolation_level=None)
        try:
            db.execute(_CURSOR_DDL)
            row = db.execute("SELECT case_seq FROM case_cursor WHERE case_id=? AND session_ref=?",
                             (case_id, session_ref)).fetchone()
            seen = row[0] if row else 0
            db.execute("INSERT INTO case_cursor(case_id, session_ref, case_seq) VALUES(?,?,?) "
                       "ON CONFLICT(case_id, session_ref) DO UPDATE SET "
                       "case_seq = MAX(case_seq, excluded.case_seq)", (case_id, session_ref, last))
        finally:
            db.close()
        unread = [item for item in brief if item["seq"] > seen]
        return {"tail": brief[-TAIL_FINDINGS:], "unread": unread[:MAX_UNREAD_SHOWN],
                "unread_total": len(unread), "resumed": row is not None}

    @staticmethod
    def _relation_claim(state: Mapping[str, Any], relation_links: Any, kind: str,
                        status: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The semantic check of a relationship claim against its recorded links."""
        ids = list(dict.fromkeys(_strings(relation_links, "relation_links", 10)))
        if not ids:
            raise ValidationError("relation_links_empty")
        if kind != "fact":
            raise ValidationError("relation_claim_must_be_a_fact")
        known = {item["id"]: item for item in state["links"]}
        missing = [link_id for link_id in ids if link_id not in known]
        if missing:
            raise ValidationError("relation_link_not_found:" + ",".join(missing))
        links = [known[link_id] for link_id in ids]
        if status == "supported":
            weak = [f"{link['id']}={link['outcome']}" for link in links
                    if link["outcome"] != "verified"]
            if weak:
                # A text lead or a contradicted link never supports a relationship.
                raise ValidationError("relation_claim_supported_requires_verified_links:"
                                      + ",".join(weak))
        if status == "contradicted" and not any(link["outcome"] == "contradicted"
                                                for link in links):
            raise ValidationError("relation_claim_contradicted_requires_a_contradicted_link")
        return ({"kind": "relation", "links": ids,
                 "contracts": [link["contract"]["id"] for link in links],
                 "link_outcomes": [link["outcome"] for link in links],
                 "statement_by": "caller"}, links)

    def link(self, actor: Actor, case_id: str, from_citation: str, to_citation: str,
             contract_id: str) -> dict[str, Any]:
        """Check and record a link between two cited rows under a declared contract.

        Both citations are resolved for ``actor`` and the contract is evaluated
        over exactly that content (``operating.case_links``). ``verified`` and
        ``contradicted`` record the link and a Workbench-written fact finding
        (supported / contradicted) citing both rows, in one transaction; ``lead``
        (a text match) and ``stored_by_text_join`` (a stored id the source set by a
        text join) record the link only, never a supported finding, and never
        support a relation claim.
        ``unknown`` (a value absent or not of the contract's type, a key naming
        several rows) and ``no_match`` (texts differ) record nothing and say why.
        Repeating a recorded check on the same content returns it
        (``duplicate: true``).
        """
        _check_actor(actor)
        if self._link_contracts is None:
            raise ValidationError("link_contracts_not_configured")
        from_handle, to_handle = _handle(from_citation), _handle(to_citation)
        if from_handle == to_handle:
            raise ValidationError("link_needs_two_different_citations")
        self._readable(actor, case_id)
        views = {handle: self._resolve_strict(handle, actor)
                 for handle in (from_handle, to_handle)}
        checked = self._link_contracts.check(contract_id, from_handle, views[from_handle],
                                             to_handle, views[to_handle])
        outcome = checked["outcome"]
        contract = {key: checked["contract"][key]
                    for key in ("id", "digest", "match", "strength", "provenance")}
        result = {"outcome": outcome, "contract": checked["contract"],
                  "pairs": checked["pairs"]}
        if outcome not in LINK_OUTCOMES:
            return {**result, "recorded": False, "reason": checked["reason"]}
        cited = [{"handle": handle, "digest": _digest(views[handle])}
                 for handle in (from_handle, to_handle)]
        statement = _link_statement(checked, views[from_handle], views[to_handle])
        label = actor_label(actor)

        def build(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
            for item in state["links"]:
                if item["contract"] == contract \
                        and _same_citations(item["citations"], cited, ordered=True):
                    return {**result, "case_id": case_id, "link_id": item["id"],
                            "finding_id": item.get("finding_id"), "recorded": True,
                            "duplicate": True, "statement": item["statement"]}
            link_id = f"L{len(state['links']) + 1}"
            record = {"link_id": link_id, "contract": contract, "outcome": outcome,
                      "pairs": checked["pairs"], "statement": statement, "citations": cited}
            finding = None
            if outcome in ("verified", "contradicted"):
                finding = {"finding_id": f"F{len(state['findings']) + 1}", "kind": "fact",
                           "status": "supported" if outcome == "verified" else "contradicted",
                           "statement": statement, "citations": cited,
                           "claim": {"kind": "relation", "links": [link_id],
                                     "contracts": [contract["id"]],
                                     "link_outcomes": [outcome],
                                     "statement_by": "workbench"}}
                record["finding_id"] = finding["finding_id"]
            event = self._append(db, case_id, "link.recorded", label, record)
            if finding is not None:
                self._append(db, case_id, "finding.recorded", label, finding)
            return {**result, **event, "link_id": link_id,
                    "finding_id": record.get("finding_id"), "recorded": True,
                    "duplicate": False, "statement": statement}

        return self._write(actor, case_id, build)

    def supersede(self, actor: Actor, case_id: str, finding_id: str, reason: str,
                  replaced_by: str | None = None, *,
                  author_kind: str | None = None) -> dict[str, Any]:
        """Mark a finding superseded (it stays in the record). The acting principal
        must be able to read it now: every citation of the finding (and of
        ``replaced_by``) resolves for ``actor``. A finding it sees only as withheld is
        refused (``finding_not_visible``), never superseded on its behalf. Only a human
        (``author_kind``, from the session's channel) supersedes another author's
        finding (``supersede_refusal``)."""
        _check_actor(actor)
        reason = _text(reason, "reason")
        if author_kind is not None and author_kind not in AUTHOR_KINDS:
            raise ValidationError("invalid_author_kind")
        findings = self._readable(actor, case_id)["findings"]
        for fid, error in ((finding_id, "finding_not_visible"),
                           (replaced_by, "replacement_finding_not_visible")):
            found = findings.get(fid) if fid is not None else None
            if found is not None and any(self._current_digest(cite["handle"], actor) is None
                                         for cite in found["citations"]):
                raise ValidationError(error)
        refusal = supersede_refusal(findings.get(finding_id), actor, author_kind)
        if refusal is not None:
            raise ValidationError(refusal)

        def make(state: dict[str, Any]) -> dict[str, Any]:
            found = state["findings"].get(finding_id)
            if found is None:
                raise ValidationError("finding_not_found")
            if found["superseded"]:
                raise ValidationError("finding_already_superseded")
            if replaced_by is not None and (replaced_by == finding_id or
                                            replaced_by not in state["findings"]):
                raise ValidationError("invalid_replacement_finding")
            return {"finding_id": finding_id, "reason": reason, "replaced_by": replaced_by}

        return self._mutate(actor, case_id, "finding.superseded", make)

    def compute(self, actor: Actor, case_id: str, inputs: Mapping[str, Mapping[str, Any]],
                expression: str, unit: str,
                relations: Sequence[Mapping[str, str]] | None = None) -> dict[str, Any]:
        """Deterministic Decimal calculation with unit checking, recorded with its inputs.

        ``inputs[name]`` is ``{"citation": handle, "field": "dotted.path", "unit": u}``
        (the value is read from the resolved citation) or ``{"literal": v, "unit": u}``.
        Literals must be int/str/Decimal; floats are refused. A cited JSON number
        with a fraction arrives as a float and is read by ``_cited_float``, which
        records its raw text and rule as ``source_number`` on the input.

        When the store has ``unit_contracts`` and one covers the cited column, a
        declared unit must equal the contract's (``unit_contradicts_source_contract``
        otherwise) and may be omitted. Every input records ``unit_basis``:
        ``source_contract`` (with the contract id) or ``caller_declared``.

        An input may declare ``role`` (a contract may pin it, ``role_basis``
        records which); ``relations`` declares how different roles combine
        (``quantity_role``, the combination guard). Roles and relations are recorded,
        with ``result_role`` (the result's label) and ``result_type`` (its quantity type
        by the closed algebra of ``quantity_types`` over the expression tree and the
        recorded roles; None when untyped). A relation whose ``as`` does not fit the form
        it labels is refused (``quantity_label_inconsistent``). The claim gate never
        trusts either record: it recomputes both from source contracts.
        """
        _check_actor(actor)
        if not isinstance(inputs, Mapping) or not inputs or len(inputs) > _MAX_INPUTS:
            raise ValidationError("invalid_inputs")
        declared = parse_unit(unit)
        relations = parse_relations(None if relations is None else list(relations))
        self._readable(actor, case_id)
        recorded: dict[str, Any] = {}
        values: dict[str, tuple[Decimal, Unit]] = {}
        cache: dict[str, Mapping[str, Any]] = {}
        for name, spec in inputs.items():
            if (not isinstance(name, str) or not _NAME.match(name) or keyword.iskeyword(name)
                    or not isinstance(spec, Mapping)):
                raise ValidationError(f"invalid_input_name:{name}")
            if ("citation" in spec) == ("literal" in spec):
                raise ValidationError(f"input_needs_exactly_one_of_citation_or_literal:{name}")
            if "citation" in spec:
                handle = _handle(spec["citation"])
                field = _text(spec.get("field"), "field", 200)
                if handle not in cache:
                    cache[handle] = self._resolve_strict(handle, actor)
                    if _changed_since(cache[handle]):
                        raise ValidationError(f"citation_changed_since:{name}")
                raw = _field(cache[handle], field)
                in_unit, unit_basis = bind_cited_unit(self._unit_contracts, handle,
                                                      cache[handle], field, name,
                                                      spec.get("unit"))
                source_number = None
                if isinstance(raw, float):
                    number, source_number = _cited_float(raw, f"{name}.{field}")
                else:
                    number = _to_decimal(raw, f"{name}.{field}")
                recorded[name] = {"kind": "citation", "field": field,
                                  "citation": {"handle": handle, "digest": _digest(cache[handle])},
                                  "value": _dec_str(number), "unit": format_unit(in_unit),
                                  **unit_basis, **bind_role(self._unit_contracts, handle,
                                                            cache[handle], field, name,
                                                            spec.get("role"))}
                if source_number is not None:
                    recorded[name]["source_number"] = source_number
            else:
                in_unit = parse_unit(spec.get("unit"))
                number = _to_decimal(spec["literal"], name)
                recorded[name] = {"kind": "literal", "value": _dec_str(number),
                                  "unit": format_unit(in_unit), "unit_basis": "caller_declared",
                                  **bind_role(None, None, None, None, name, spec.get("role"))}
            values[name] = (number, in_unit)
        result, result_unit, inexact, used = evaluate(expression, values)
        if set(values) - used:
            raise ExpressionError("unused_input:" + ",".join(sorted(set(values) - used)))
        if result_unit != declared:
            raise UnitMismatch(f"declared_unit_{format_unit(declared)}"
                               f"_but_expression_yields_{format_unit(result_unit)}")
        roled: dict[str, Any] = {}
        if relations or any("role" in spec for spec in recorded.values()):
            seen: list[tuple[int, ast.AST]] = []
            result_role, used_relations = quantity_role(expression, {
                name: spec.get("role", _NEUTRAL if spec["kind"] == "literal" else None)
                for name, spec in recorded.items()}, relations,
                {name: unit for name, (_number, unit) in values.items()},
                {name for name, (number, _unit) in values.items()
                 if recorded[name]["kind"] == "literal" and number < 0}, seen=seen)
            if len(used_relations) != len(relations):
                raise ExpressionError("unused_role_relation:" + ",".join(
                    str(index) for index in range(len(relations)) if index not in used_relations))
            # The result's quantity type comes from the expression tree (the closed algebra
            # of ``quantity_types``), never from a relation's name; each name must fit the
            # form it labels. Types here use the recorded roles (contract or caller); the
            # claim gate recomputes them from source contracts only.
            typed = _qt.type_calculation(expression, {
                name: (_qt.cell(spec["citation"]["handle"], spec["field"])
                       if spec["kind"] == "citation" else ("literal", name), spec.get("role"),
                       spec["value"])
                for name, spec in recorded.items()}, relations, seen,
                result_role if result_role and result_role not in _qt.QUANTITY_TYPES
                and result_role not in {spec.get("role") for spec in recorded.values()}
                else None,
                constants={name: spec["value"] for name, spec in recorded.items()
                           if spec["kind"] == "literal" and "role" not in spec})
            if typed["problem"]:
                raise QuantityRoleMismatch(f"quantity_label_inconsistent: {typed['problem']}")
            roled = {"relations": relations, "result_role": result_role or None,
                     "result_type": typed["type"]}
        expression = expression.strip()

        def make(state: dict[str, Any]) -> dict[str, Any]:
            return {"calc_id": f"C{len(state['calculations']) + 1}", "inputs": recorded,
                    "expression": expression, "result": _dec_str(result),
                    "unit": format_unit(result_unit), "inexact": inexact, **roled}

        receipt = self._mutate(actor, case_id, "calculation.recorded", make)
        return {**receipt, "result": _dec_str(result), "unit": format_unit(result_unit),
                "inexact": inexact, **roled,
                "input_units": {name: {key: spec[key] for key in (
                                    "unit", "unit_basis", "unit_contract", "unit_contract_digest",
                                    "role", "role_basis", "role_contract")
                                    if key in spec}
                                for name, spec in recorded.items()}}

    def gap(self, actor: Actor, case_id: str, missing: str, why: str,
            who_can_answer: str | Sequence[str], state: str,
            citations: Iterable[str] = (),
            value_claims: Sequence[Mapping[str, Any]] | None = None,
            workbench_statement: bool = False,
            about_property: str | Sequence[str] | None = None) -> dict[str, Any]:
        """Record a visible unknown.

        When the text quotes or is derived from cited data, pass those
        ``citations``: every read then withholds the gap's content from a reader
        who cannot resolve all of them, exactly as for findings. ``about_property`` (one
        or a list of schema names: ``<ObjectType>.<property>``, ``<ObjectType>.<link>`` or
        ``<ObjectType>``) records what is missing, as the caller's gap guard checked it
        (``operating.capability_guards.check_gap``); it is stored as a sorted list.
        """
        _check_actor(actor)
        missing = _text(missing, "missing")
        why = _text(why, "why")
        if state not in GAP_STATES:
            raise ValidationError("invalid_gap_state")
        who = [who_can_answer] if isinstance(who_can_answer, str) else who_can_answer
        who = _strings(who, "who_can_answer", 10)
        readable = self._readable(actor, case_id)
        cited = self._cite(() if citations is None else citations, actor, required=False)
        values = _value_claims(value_claims, cited, readable)
        if about_property is not None:
            names = [about_property] if isinstance(about_property, str) else about_property
            about_property = sorted({_text(name, "about_property", 200)
                                     for name in _strings(names, "about_property", 5)})

        def make(current: dict[str, Any]) -> dict[str, Any]:
            return {"gap_id": f"G{len(current['gaps']) + 1}", "missing": missing, "why": why,
                    "who_can_answer": who, "state": state, "citations": cited,
                    **({"property": about_property} if about_property else {}),
                    **({"value_claims": values} if values else {}),
                    **({"statement_by": "workbench"} if workbench_statement is True else {})}

        return self._mutate(actor, case_id, "gap.recorded", make)

    def propose(self, actor: Actor, case_id: str, capability: str, payload: Mapping[str, Any],
                preconditions: Sequence[Any] = (), citations: Iterable[str] = (), *,
                evidence: Mapping[str, Any] | None = None,
                idempotency_key: str | None = None,
                workbench_statement: bool = False,
                expected_effect: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Record a proposed action. Nothing is executed or dispatched from here.

        ``citations`` gate the proposal's capability, payload and preconditions on
        read exactly like a finding's statement. ``evidence`` (``statement``,
        optional ``kind``/``status``, default a supported fact) appends a finding
        with the same citations in the same transaction; the proposal records its
        id as ``evidence_finding_id``. ``idempotency_key`` (never projected on
        read) makes a repeat return the existing ids with ``duplicate: true``
        instead of appending anything (a repeat declaring an expected effect other
        than the stored one is refused; ``expected_effect_recorded`` says whether
        the stored proposal has one). ``expected_effect`` (see
        ``parse_expected_effect``) names what a source row should show once the
        proposal has been carried out; its row must be one of the proposal's
        citations (recorded as ``target_citation``), so a later read-back
        (``verification``) checks the row the proposal is about.
        """
        _check_actor(actor)
        # One spelling, stored exactly as checked (operating.capability_guards.canonical):
        # a name with surrounding space or capitals is refused, never repaired.
        from .capability_guards import canonical
        if canonical(capability) is None:
            raise ValidationError("capability_not_canonical")
        if not isinstance(payload, Mapping):
            raise ValidationError("proposal_payload_must_be_object")
        preconditions = () if preconditions is None else preconditions
        if isinstance(preconditions, (str, Mapping)) or not isinstance(preconditions, Iterable):
            raise ValidationError("invalid_preconditions")
        body = _plain_json(payload)
        conditions = _plain_json(list(preconditions))
        key = None if idempotency_key is None else _text(idempotency_key, "idempotency_key", 200)
        finding_body: dict[str, Any] | None = None
        if evidence is not None:
            if not isinstance(evidence, Mapping):
                raise ValidationError("invalid_evidence")
            finding_body = {"kind": evidence.get("kind", "fact"),
                            "status": evidence.get("status", "supported"),
                            "statement": _text(evidence.get("statement"), "statement")}
            if finding_body["kind"] not in FINDING_KINDS:
                raise ValidationError("invalid_finding_kind")
            if finding_body["status"] not in FINDING_STATUSES:
                raise ValidationError("invalid_finding_status")
            if workbench_statement is True:
                finding_body["statement_by"] = "workbench"  # set by tool code only
        effect = None if expected_effect is None else parse_expected_effect(expected_effect)
        readable = self._readable(actor, case_id)
        cited = self._cite(() if citations is None else citations, actor,
                           required=finding_body is not None and finding_body["kind"] == "fact")
        if finding_body is not None and evidence.get("value_claims") is not None:
            finding_body["value_claims"] = _value_claims(evidence["value_claims"], cited,
                                                         readable)
        if effect is not None:
            effect["target_citation"] = self._effect_target(effect, cited, actor)
        label = actor_label(actor)

        def build(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
            if key is not None:
                # A repeat is recognised only when the caller cites exactly the stored
                # proposal's citations, which _cite has just resolved for this caller;
                # otherwise the key would reveal a proposal the caller cannot read.
                handles = {item["handle"] for item in cited}
                for item in state["proposals"]:
                    if (item.get("idempotency_key") == key and handles
                            and {c["handle"] for c in item.get("citations", ())} == handles):
                        stored = item.get("expected_effect")
                        if (effect is not None and stored is not None
                                and _canon({**stored, "target_citation": None})
                                != _canon({**effect, "target_citation": None})):
                            # Same key and citations, another effect: never keep one
                            # silently while the caller believes the other is recorded.
                            raise ValidationError("idempotency_key_reused_with_other_"
                                                  "expected_effect")
                        receipt = {"case_id": case_id, "seq": item["seq"],
                                   "kind": "proposal.recorded", "actor": item["recorded_by"],
                                   "at": item["at"], "proposal_id": item["id"],
                                   "duplicate": True, "status": PROPOSAL_STATUS,
                                   "expected_effect_recorded": stored is not None}
                        if item.get("evidence_finding_id"):
                            receipt["evidence_finding_id"] = item["evidence_finding_id"]
                        return receipt
            extra: dict[str, Any] = {}
            if finding_body is not None:
                finding_id = f"F{len(state['findings']) + 1}"
                self._append(db, case_id, "finding.recorded", label,
                             {"finding_id": finding_id, **finding_body, "citations": cited})
                extra["evidence_finding_id"] = finding_id
            proposal_id = f"P{len(state['proposals']) + 1}"
            record = {"proposal_id": proposal_id, "capability": capability, "payload": body,
                      "preconditions": conditions, "status": PROPOSAL_STATUS,
                      "citations": cited, **extra}
            if key is not None:
                record["idempotency_key"] = key
            if effect is not None:
                record["expected_effect"] = effect
            event = self._append(db, case_id, "proposal.recorded", label, record)
            return {**event, "proposal_id": proposal_id, **extra, "duplicate": False,
                    "status": PROPOSAL_STATUS, "expected_effect_recorded": effect is not None}

        return self._write(actor, case_id, build)

    def _effect_target(self, effect: Mapping[str, Any], cited: Sequence[Mapping[str, str]],
                       actor: Actor) -> str:
        """The cited handle naming the effect's row (one row, relation and key equal)."""
        for cite in cited:
            view = self._resolve_strict(cite["handle"], actor)
            if (view.get("relation") == effect["relation"] and "row" in view
                    and _canon(view.get("key")) == _canon(effect["key"])):
                return cite["handle"]
        raise ValidationError("expected_effect_target_not_cited")

    def verification(self, actor: Actor, case_id: str, proposal_id: str, outcome: str,
                     reason: str | None, detail: Mapping[str, Any],
                     citations: Iterable[str], *,
                     dispatched_read_back: bool = False) -> dict[str, Any]:
        """Record what a read-back of a proposal's expected effect found.

        The verification carries ``citations`` (the row read back) plus every
        citation of the proposal, so a reader who cannot read the proposal or the
        row sees it withheld. It records an observation only: nothing here
        approves, dispatches or attributes a change.

        ``dispatched_read_back``: the read-back of a business write already sent
        (``ActionPath``'s, for a dispatched intent; no tool passes it). Another
        holder's claim never refuses it: the write is done, and only its observed
        effect would stay unrecorded. Every other verification is refused as any write.
        """
        _check_actor(actor)
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValidationError("proposal_not_found")
        if outcome not in VERIFICATION_OUTCOMES:
            raise ValidationError("invalid_verification_outcome")
        reason = None if reason is None else _text(reason, "reason", 200)
        if not isinstance(detail, Mapping):
            raise ValidationError("verification_detail_must_be_object")
        body = _plain_json(dict(detail))
        state = self._readable(actor, case_id)
        proposal = next((item for item in state["proposals"] if item["id"] == proposal_id),
                        None)
        if proposal is None:
            raise ValidationError("proposal_not_found")
        if not proposal.get("expected_effect"):
            raise ValidationError("proposal_has_no_expected_effect")
        handles = list(() if citations is None else citations)
        if isinstance(citations, str) or not handles:
            raise ValidationError("invalid_citations")
        handles += [cite["handle"] for cite in proposal.get("citations") or ()]
        cited = self._cite(handles, actor, required=True)

        def make(current: dict[str, Any]) -> dict[str, Any]:
            return {"verification_id": f"V{len(current['verifications']) + 1}",
                    "proposal_id": proposal_id, "outcome": outcome, "reason": reason,
                    "detail": body, "citations": cited}

        return self._mutate(actor, case_id, "verification.recorded", make,
                            claim_checked=dispatched_read_back is not True)

    def close(self, actor: Actor, case_id: str, outcome: str) -> dict[str, Any]:
        _check_actor(actor)
        outcome = _text(outcome, "outcome")
        return self._mutate(actor, case_id, "case.closed", lambda state: {"outcome": outcome})

    def claim(self, actor: Actor, case_id: str, holder: Mapping[str, Any], lease_seconds: int,
              handoff: str | None = None, *, author_kind: str | None = None) -> dict[str, Any]:
        """Claim, renew (``lease_seconds`` 1..``MAX_LEASE_SECONDS``) or release (0) the
        case for ``holder`` (``claim_holder``); a release may leave a ``handoff`` note,
        written as ``author_kind``. Refused with ``case_claimed_until:<expires_at>``
        while another holder's claim is active, except a release from the holder's own
        employee in a restarted session; the check and the event are one transaction, so
        two concurrent claimants never both hold the case."""
        _check_actor(actor)
        holder, note = self._claim_args(holder, lease_seconds, handoff, author_kind)
        return self._claim_one(actor, case_id, holder, lease_seconds, note, author_kind)

    def claim_objective(self, actor: Actor, case_ids: Any, holder: Mapping[str, Any],
                        lease_seconds: int, handoff: str | None = None, *,
                        author_kind: str | None = None) -> dict[str, Any]:
        """``claim`` every case of an objective (``objective_cases`` order), all or none:
        ``{"cases": receipts, "refused": []}``, one expiry for all, in one write
        transaction. The first case that refuses rolls it back: every lease, one this
        holder already held included, is exactly as it was and ``ObjectiveRefused``
        names that case. A release (0) releases every case it may, each with the
        ``handoff`` note (``each_objective_case``)."""
        _check_actor(actor)
        holder, note = self._claim_args(holder, lease_seconds, handoff, author_kind)
        if not lease_seconds:
            return each_objective_case(case_ids, lambda case_id: self._claim_one(
                actor, case_id, holder, 0, note, author_kind))
        ordered = objective_cases(case_ids)
        expires_at = (self._moment() + timedelta(seconds=lease_seconds)
                      ).isoformat(timespec="microseconds")
        done: list[dict[str, Any]] = []
        current: str | None = None
        try:
            with self._connect(write=True) as db:
                for current in ordered:
                    done.append(self._claim_one(actor, current, holder, lease_seconds,
                                                None, None, expires_at, db=db))
                current = None   # a commit failure is no case's refusal
        except _CASE_REFUSALS as exc:
            if current is None:
                raise
            raise ObjectiveRefused(current, _refusal(exc)) from exc
        return {"cases": done, "refused": []}

    @staticmethod
    def _claim_args(holder: Any, lease_seconds: Any, handoff: Any,
                    author_kind: Any) -> tuple[dict[str, str], str | None]:
        holder = claim_holder(holder)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) \
                or not 0 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValidationError("invalid_lease_seconds")
        note = None if handoff is None else _text(handoff, "handoff")
        if note is not None and lease_seconds:
            raise ValidationError("handoff_is_a_release_note")
        if author_kind is not None and author_kind not in AUTHOR_KINDS:
            raise ValidationError("invalid_author_kind")
        return holder, note

    def _claim_one(self, actor: Actor, case_id: str, holder: dict[str, str],
                   lease_seconds: int, note: str | None, author_kind: str | None,
                   expires_at: str | None = None,
                   db: sqlite3.Connection | None = None) -> dict[str, Any]:
        """One checked claim event; a claim expires at ``expires_at`` when given (an
        objective's one expiry), written in ``db``'s transaction when given."""

        def build(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
            self._refuse_claimed(state, holder, release=not lease_seconds)
            payload: dict[str, Any] = {"holder": holder}
            if lease_seconds:
                kind = "case.claimed"
                payload.update(lease_seconds=lease_seconds, expires_at=expires_at or (
                    self._moment() + timedelta(seconds=lease_seconds)
                ).isoformat(timespec="microseconds"))
            else:
                kind = "case.released"
                if note is not None:
                    payload["handoff"] = note
                    if author_kind is not None:
                        payload["author_kind"] = author_kind
            event = self._append(db, case_id, kind, actor_label(actor), payload)
            return {**event, "claim": self._claim_view(
                _claim_step(state["claim"], kind, payload, event["at"]))}

        return self._write(actor, case_id, build, claim_checked=False, db=db)

    def break_claim(self, actor: Actor, case_id: str, reason: str,
                    approver: Mapping[str, Any], *, author_kind: str) -> dict[str, Any]:
        """End the active claim on the case for ``approver`` with ``reason``, journalled
        as ``case.claim_broken`` (the broken holder, the reason, ``approver``: email,
        Entra oid, session, and ``author_kind``: who wrote the reason). The caller checks
        that ``approver`` may break a claim (``ActionPath.break_claim``). Refused
        ``no_active_claim`` when nothing is held."""
        _check_actor(actor)
        reason = _text(reason, "reason")
        if not isinstance(approver, Mapping) or set(approver) != {"email", "oid", "session"} \
                or not all(isinstance(value, str) and value for value in approver.values()):
            raise ValidationError("invalid_claim_approver")
        if author_kind not in AUTHOR_KINDS:
            raise ValidationError("invalid_author_kind")
        approver = dict(approver)

        def build(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
            current = self._claim_view(state["claim"])
            if current is None or not current["active"]:
                raise ValidationError("no_active_claim")
            payload = {"holder": current["holder"], "reason": reason, "approver": approver,
                       "author_kind": author_kind}
            event = self._append(db, case_id, "case.claim_broken", actor_label(actor), payload)
            return {**event, "claim": self._claim_view(
                _claim_step(state["claim"], "case.claim_broken", payload, event["at"]))}

        return self._write(actor, case_id, build, claim_checked=False)

    # ---- reads --------------------------------------------------------

    def proposal_evidence(self, actor: Actor, case_id: str,
                          proposal_id: str) -> list[dict[str, Any]]:
        """What each citation of one proposal resolves to now, for ``actor``.

        One item per citation: ``handle``, ``cited_digest``, ``current_digest``
        (None when it no longer resolves for this actor) and ``content`` (only
        when the current content is exactly the cited content, else None). Used
        to check action preconditions against the evidence that was cited.
        """
        _check_actor(actor)
        state = self._readable(actor, case_id)
        proposal = next((item for item in state["proposals"] if item["id"] == proposal_id),
                        None)
        if proposal is None:
            raise ValidationError("proposal_not_found")
        out = []
        for cite in proposal.get("citations") or ():
            try:
                content: Mapping[str, Any] | None = self._resolve_strict(cite["handle"], actor)
            except CitationUnresolvable:
                content = None
            current = None if content is None else _digest(content)
            out.append({"handle": cite["handle"], "cited_digest": cite["digest"],
                        "current_digest": current,
                        "content": (json.loads(json.dumps(content, default=str))
                                    if current == cite["digest"] else None)})
        return out

    @contextmanager
    def outside_write(self, actor: Actor, case_id: str) -> Iterator[None]:
        """Hold the case's write lock for a write on it kept outside this store (an
        intent registered on it) until that write commits inside the block: refused
        ``CaseClaimed`` while another holder's claim is active, as ``_write`` refuses,
        and a claim taken meanwhile waits for it. Take it after the outside store's own
        write lock (``ActionPath`` does; ``open_case`` takes the two in that order)."""
        with self._connect(write=True) as db:
            self._refuse_claimed(self._state(db, _check_actor(actor), case_id), _WRITER.get())
            yield

    def cited_handles(self, actor: Actor, case_id: str) -> list[str]:
        """Every citation handle of the items ``get`` withholds from a reader who cannot
        resolve one of them (findings, calculations, gaps, proposals, verifications), for a
        host check that depends on no reader (``CaseRuntime.hidden_from_every_reader``); never
        served to a reader. ``actor`` must be able to read the case."""
        state = self._readable(_check_actor(actor), case_id)
        cites = [cite for item in state["findings"].values() for cite in item["citations"]]
        cites += [spec["citation"] for calc in state["calculations"]
                  for spec in calc["inputs"].values() if spec["kind"] == "citation"]
        cites += [cite for section in ("gaps", "proposals", "verifications")
                  for item in state[section] for cite in item.get("citations") or ()]
        return sorted({cite["handle"] for cite in cites})

    def verify_chain(self, actor: Actor, case_id: str) -> bool:
        return bool(self._readable(_check_actor(actor), case_id)["chain_ok"])

    def get(self, actor: Actor, case_id: str, *, authors: bool = False) -> dict[str, Any]:
        """Projection visible to ``actor``; cited content is re-checked at read time.
        ``authors``: each readable finding also names the ``author_kind`` its writer
        recorded (absent: unknown)."""
        _check_actor(actor)
        state = self._readable(actor, case_id)
        memo: dict[str, str | None] = {}

        def current(handle: str) -> str | None:
            if handle not in memo:
                memo[handle] = self._current_digest(handle, actor)
            return memo[handle]

        def cite_view(cites: Sequence[Mapping[str, str]]
                      ) -> tuple[list[dict[str, Any]], bool, bool]:
            """(per-citation view, all resolvable, any content changed)."""
            view, complete, changed = [], True, False
            for index, cite in enumerate(cites):
                digest = current(cite["handle"])
                if digest is None:
                    view.append({"index": index, "status": CITATION_UNAVAILABLE})
                    complete = False
                elif digest != cite["digest"]:
                    # Resolvable, but not the content that was cited: report it,
                    # never vouch for the recorded claim against new content.
                    view.append({"handle": cite["handle"], "status": CITATION_CONTENT_CHANGED,
                                 "digest": cite["digest"], "current_digest": digest})
                    changed = True
                else:
                    view.append({"handle": cite["handle"], "status": "resolved",
                                 "digest": cite["digest"]})
            return view, complete, changed

        def bare(view: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            """Citation view of a withheld item: no handle or digest of any citation,
            so a withheld item never tells the reader which rows it concerns."""
            return [{"index": index, "status": cite["status"]}
                    for index, cite in enumerate(view)]

        buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in FINDING_STATUSES}
        buckets["superseded"], buckets["withheld"] = [], []
        buckets[CITATION_CONTENT_CHANGED] = []
        for item in state["findings"].values():
            citations, complete, changed = cite_view(item["citations"])
            base = {"id": item["id"], "kind": item["kind"], "label": item["label"],
                    "citations": citations, "recorded_by": item["recorded_by"],
                    "at": item["at"], "seq": item["seq"]}
            if not complete:
                base["citations"] = bare(citations)
                buckets["withheld"].append({**base, "statement": None, "status": None,
                                            "superseded": item["superseded"] is not None,
                                            "content": WITHHELD})
                continue
            entry = {**base, "statement": item["statement"], "status": item["status"]}
            if item.get("claim"):
                entry["claim"] = item["claim"]
            if item.get("value_claims"):
                entry["value_claims"] = item["value_claims"]
            if item.get("statement_by"):
                entry["statement_by"] = item["statement_by"]
            if authors and item.get("author_kind"):
                entry["author_kind"] = item["author_kind"]
            if item["superseded"]:
                buckets["superseded"].append({**entry, "superseded": item["superseded"]})
            elif changed:
                buckets[CITATION_CONTENT_CHANGED].append(
                    {**entry, "status": None, "recorded_status": item["status"],
                     "content": CITATION_CONTENT_CHANGED})
            else:
                buckets[item["status"]].append(entry)

        calculations = []
        for calc in state["calculations"]:
            cited = [name for name, spec in calc["inputs"].items() if spec["kind"] == "citation"]
            citations, complete, changed = cite_view(
                [calc["inputs"][name]["citation"] for name in cited])
            base = {"id": calc["id"], "recorded_by": calc["recorded_by"], "at": calc["at"],
                    "seq": calc["seq"]}
            if not complete:
                calculations.append({**base, "content": WITHHELD, "result": None,
                                     "citations": bare(citations)})
                continue
            # The recorded result is history and never recomputed; each cited
            # input carries its read-time status next to the recorded digest.
            inputs = {name: dict(spec) for name, spec in calc["inputs"].items()}
            for name, view in zip(cited, citations):
                inputs[name]["citation"] = view
            calculations.append({**base, "expression": calc["expression"],
                                 "result": calc["result"], "unit": calc["unit"],
                                 "inexact": calc["inexact"], "inputs": inputs,
                                 **{key: calc[key] for key in ("relations", "result_role", "result_type")
                                    if key in calc},
                                 "inputs_status": (CITATION_CONTENT_CHANGED if changed
                                                   else "resolved")})

        def gated(cites: Sequence[Mapping[str, str]], base: dict[str, Any],
                  content: dict[str, Any]) -> dict[str, Any]:
            """Gap/proposal view: content only when every citation resolves now."""
            citations, complete, changed = cite_view(cites)
            if not complete:
                return {**base, "citations": bare(citations),
                        **{name: None for name in content}, "content": WITHHELD}
            base = {**base, "citations": citations}
            status = ("uncited" if not cites else
                      CITATION_CONTENT_CHANGED if changed else "resolved")
            return {**base, **content, "citations_status": status}

        gaps = [gated(gap.get("citations") or [],
                      {"id": gap["id"], "recorded_by": gap["recorded_by"], "at": gap["at"],
                       "seq": gap["seq"]},
                      {"missing": gap["missing"], "why": gap["why"],
                       "who_can_answer": gap["who_can_answer"], "state": gap["state"],
                       **({"value_claims": gap["value_claims"]}
                          if gap.get("value_claims") else {}),
                       **({"statement_by": gap["statement_by"]}
                          if gap.get("statement_by") else {}),
                       **({"property": gap["property"]} if gap.get("property") else {})})
                for gap in state["gaps"]]
        proposals = []
        for item in state["proposals"]:
            base = {"id": item["id"], "status": item["status"],
                    "recorded_by": item["recorded_by"], "at": item["at"], "seq": item["seq"]}
            if item.get("evidence_finding_id"):
                base["evidence_finding_id"] = item["evidence_finding_id"]
            # The idempotency key is never projected: it can fingerprint cited values.
            proposals.append(gated(item.get("citations") or [], base,
                                   {"capability": item["capability"], "payload": item["payload"],
                                    "preconditions": item["preconditions"],
                                    "expected_effect": item.get("expected_effect")}))
        verifications = [gated(item["citations"],
                               {"id": item["id"], "proposal_id": item["proposal_id"],
                                "recorded_by": item["recorded_by"], "at": item["at"],
                                "seq": item["seq"]},
                               {"outcome": item["outcome"], "reason": item["reason"],
                                "detail": item["detail"]})
                         for item in state["verifications"]]

        links = [gated(item["citations"],
                       {"id": item["id"], "recorded_by": item["recorded_by"], "at": item["at"],
                        "seq": item["seq"]},
                       {"contract": item["contract"], "outcome": item["outcome"],
                        "pairs": item["pairs"], "statement": item["statement"],
                        "finding_id": item.get("finding_id")})
                 for item in state["links"]]

        return {
            "case_id": state["case_id"], "question": state["question"], "roots": state["roots"],
            "owner": state["owner"], "tenant": state["tenant"],
            "classification": state["classification"], "audience": state["audience"],
            "status": state["status"], "opened_at": state["opened_at"],
            "updated_at": state["updated_at"], "closed_at": state["closed_at"],
            "closed_by": state["closed_by"], "outcome": state["outcome"],
            "findings": buckets, "calculations": calculations,
            "gaps": gaps, "proposals": proposals, "verifications": verifications,
            "timeline": state["timeline"],
            "integrity": {"events": state["last_seq"], "chain_ok": state["chain_ok"]},
            # Present only on a case ever claimed (as links), so views of every other
            # case are unchanged; handoff is None until a release leaves a note.
            **({"claim": self._claim_view(state["claim"]), "handoff": state["handoff"]}
               if state["claim"] is not None else {}),
            # Present only on cases that have links, so readers written before
            # links existed see unchanged views of every other case.
            **({"links": links} if links else {}),
            # Present only when the opening channel named the question's author.
            **({"objective_author_kind": state["objective_author_kind"]}
               if state.get("objective_author_kind") else {}),
        }

    def list(self, actor: Actor, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest-first summaries of cases this actor may see."""
        _check_actor(actor)
        if status is not None and status not in CASE_STATUSES:
            raise ValidationError("invalid_status")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIST:
            raise ValidationError("invalid_limit")
        out: list[dict[str, Any]] = []
        cursor = 2 ** 62
        header_allows = self._header_allows(actor)
        with self._connect() as db:
            while len(out) < limit:
                rows = db.execute(_LIST_SQL, (cursor, 200)).fetchall()
                if not rows:
                    break
                for row in rows:
                    cursor = row["seq"]
                    opened = json.loads(row["payload_json"])
                    closed = bool(row["closed"])
                    if status and (status == "closed") != closed:
                        continue
                    if not _visible(opened, actor, header_allows):
                        continue
                    out.append({"case_id": row["case_id"], "question": opened["question"],
                                "owner": opened["owner"],
                                "status": "closed" if closed else "open",
                                "opened_at": row["opened_at"], "updated_at": row["updated_at"],
                                "events": row["events"]})
                    if len(out) == limit:
                        break
            # Claims of the listed cases only (never of one the actor may not see), in
            # one query; a case never claimed has no claim key.
            claims: dict[str, dict[str, Any]] = {}
            if out:
                for event in db.execute(_CLAIMS_SQL.format(",".join("?" * len(out))),
                                        [item["case_id"] for item in out]):
                    claims[event["case_id"]] = _claim_step(
                        claims.get(event["case_id"]), event["kind"],
                        json.loads(event["payload_json"]), event["at"])
            for item in out:
                if item["case_id"] in claims:
                    item["claim"] = self._claim_view(claims[item["case_id"]])
        return out

    # ---- agent sessions (``operating.session_replay`` shapes and withholds them) ----

    def record_session(self, actor: Actor, case_id: str,
                       session: Mapping[str, Any]) -> dict[str, Any]:
        """Keep one agent session on the case (``SESSION_KIND``): ``session`` with each
        handle of its ``cited`` resolved for ``actor`` and digested, as a finding's are, and
        its ``gated`` handles (what else it read) kept for ``session_records``."""
        _check_actor(actor)
        self._readable(actor, case_id)
        handles = sorted({_handle(item) for item in session.get("cited") or ()})
        gated = sorted({_handle(item) for item in session.get("gated") or ()} - set(handles))
        if len(handles) > _MAX_LIST or len(gated) > 4 * _MAX_LIST:
            raise ValidationError("too_many_session_citations")
        cited = [{"handle": handle, "digest": _digest(self._resolve_strict(handle, actor))}
                 for handle in handles]
        payload = {**{key: value for key, value in session.items() if key != "cited"},
                   "gated": gated}

        def make(state: dict[str, Any]) -> dict[str, Any]:
            count = sum(event["kind"] == SESSION_KIND for event in state["timeline"])
            return {**payload, "session_id": f"S{count + 1}", "citations": cited}

        return self._mutate(actor, case_id, SESSION_KIND, make)

    def session_records(self, actor: Actor, case_id: str
                        ) -> tuple[bool, list[dict[str, Any]]]:
        """(chain intact, each kept session as recorded with ``seq``, ``at``,
        ``recorded_by``, ``current``: each citation's digest for ``actor`` now, None where
        it does not resolve, ``gated_readable``: whether every ``gated`` handle resolves
        for ``actor`` now, and ``by_reader``: whether ``actor`` recorded it). Never served
        to a reader as is."""
        _check_actor(actor)
        with self._connect() as db:
            state = self._state(db, actor, case_id)
            rows = db.execute("SELECT case_seq, actor, at, payload_json FROM case_event "
                              "WHERE case_id=? AND kind=? ORDER BY case_seq",
                              (case_id, SESSION_KIND)).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            out.append({**payload, "seq": row["case_seq"], "at": row["at"],
                        "recorded_by": row["actor"],
                        "by_reader": row["actor"] == actor_label(actor),
                        "current": {cite["handle"]: self._current_digest(cite["handle"], actor)
                                    for cite in payload["citations"]},
                        "gated_readable": all(self._current_digest(handle, actor) is not None
                                              for handle in payload.get("gated") or ())})
        return state["chain_ok"], out


def _same_citations(recorded: Sequence[Mapping[str, str]], cited: Sequence[Mapping[str, str]],
                    ordered: bool = False) -> bool:
    """Whether a recorded write cited the same content as ``cited``: the same handles,
    each with the same digest (in the same order where order carries meaning, as a
    link's from and to). The one repeat rule for links and plain findings."""
    pairs = [[(c["handle"], c["digest"]) for c in side] for side in (recorded, cited)]
    return pairs[0] == pairs[1] if ordered else sorted(pairs[0]) == sorted(pairs[1])


def finding_key(case_id: str, kind: str, status: str, statement: str,
                handles: Iterable[str] = ()) -> str:
    """Deterministic key of a plain finding: the same case, kind, status, statement
    (ignoring case and whitespace) and cited handle set give the same key in any session
    at any time. Status is part of it: a session's different verdict on the same
    statement is a different finding."""
    return _sha(_canon([case_id, kind, status, " ".join(statement.split()).casefold(),
                        sorted(set(handles))]))


def supersede_refusal(finding: Mapping[str, Any] | None, actor: Actor,
                      author_kind: str | None) -> str | None:
    """Why ``actor`` writing as ``author_kind`` may not supersede ``finding`` (a case
    view or folded state entry), else None. A human may correct any finding it reads;
    anyone else only its own: the same recording actor and the same author kind (an
    agent never supersedes what a person, or an author nobody recorded, wrote). An
    approval route for an agent's correction of another author's finding does not
    exist yet, so that is refused."""
    if finding is None or author_kind == "human":
        return None
    if (finding.get("recorded_by") != actor_label(actor)
            or finding.get("author_kind") != author_kind):
        return "supersede_needs_human_author"
    return None


def _link_statement(checked: Mapping[str, Any], source: Mapping[str, Any],
                    target: Mapping[str, Any]) -> str:
    """The Workbench-written statement of one checked link (never caller text)."""
    contract, outcome = checked["contract"], checked["outcome"]
    sides = []
    for pair in checked["pairs"]:
        left = f"{pair['from_field']}={_canon(pair.get('from_value'))}"
        right = f"{pair['to_field']}={_canon(pair.get('to_value'))}"
        sides.append((left, right))
    head = (f"Workbench link check {contract['id']}: {contract['from']} row "
            f"{_canon(source.get('key'))} ")
    tail = f" {contract['to']} row {_canon(target.get('key'))}"
    if outcome == "verified":
        text = (head + " and ".join(f"{left} equals" for left, _ in sides) + tail + " " +
                " and ".join(right for _, right in sides) +
                f" ({contract['match']}): the cited rows are linked.")
    elif outcome == "stored_by_text_join":
        text = (head + " and ".join(f"{left} equals" for left, _ in sides) + tail + " " +
                " and ".join(right for _, right in sides) + ": " +
                contract.get("provenance_note", "the source set this stored id by a text "
                             "join") + "; not a verified link.")
    elif outcome == "lead":
        text = (head + " and ".join(f"text {left} matches" for left, _ in sides) + tail +
                " " + " and ".join(right for _, right in sides) +
                ": a lead from matching text, not a verified link.")
    else:
        text = (head + " and ".join(f"{left} vs" for left, _ in sides) + tail + " " +
                " and ".join(right for _, right in sides) +
                f": the values differ, so these rows are not linked under {contract['id']}.")
    return text[:_MAX_TEXT]


_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)\Z")  # ASCII decimal, no leading zeros: one spelling per row


def _value_claims(raw: Any, cited: Sequence[Mapping[str, str]],
                  state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Structured value claims of a finding or gap (``operating.claim_checks``): shape
    only; each cites one of the item's own citations or a recorded calculation. The
    claims are checked against the cited rows on read, never trusted."""
    if raw is None:
        return []
    from .claim_checks import parse_value_claims  # claim_checks imports this module
    return parse_value_claims(raw, [cite["handle"] for cite in cited],
                              [calc["id"] for calc in state["calculations"]])


def _changed_since(resolved: Mapping[str, Any]) -> bool:
    """Whether a resolver reports that the cited version is no longer current."""
    return resolved.get("changed_since") is True


def _field(resolved: Mapping[str, Any], path: str) -> Any:
    """Follow a dotted path through nested mappings (and list indexes) of a resolved citation.

    A list index has exactly one accepted spelling (``rows.1``, never ``rows.01``
    or non-ASCII digits), so checks keyed on the path, such as source unit
    contracts, see the same path that is read.
    """
    current: Any = resolved
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif (isinstance(current, (list, tuple)) and _INDEX.match(part)
              and int(part) < len(current)):
            current = current[int(part)]
        else:
            raise ValidationError(f"field_not_found:{path}")
    if current is None:
        raise ValidationError(f"field_is_null:{path}")
    return current
