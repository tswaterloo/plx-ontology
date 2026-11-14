"""Deterministic landed-cost contract (overseas packaging case, milestone M1-B).

per-unit landed cost = (goods + freight + duty/tariff + other) / quantity, in a
target currency. Decimal only (the Portal engine port, ``landed_cost_portal``, computes in
doubles exactly as the Portal does and hands out exact Decimals); units go through the case unit
algebra (``operating.cases.evaluate``) so ``USD/EA * EA = USD`` is checked, not assumed.

Rules this module enforces:
* Floats and bools in literals are refused. A cited JSON float is read with the
  case store's recorded rule (``_cited_float``).
* A cost is either a total (``USD``) or per quantity unit (``USD/EA``); any other
  unit is refused. A ``currency`` given next to a unit must agree with it.
* Mixed currencies need an explicit FX input ``{from, to, rate|citation, unit
  "TO/FROM", as_of}``. Missing FX makes that cost unknown with a gap; nothing is
  inverted or guessed.
* Any unknown (explicit, unresolvable citation, missing FX, stale quote) makes
  ``total`` and ``per_unit`` None. ``known_subtotal`` is reported only as
  partial, and only when every known part shares the target currency.
* An FX rate must be positive, dated no later than ``as_of`` and at most
  ``MAX_FX_AGE_DAYS`` older (else unknown / stale, like quotes). A goods cost of
  zero or less is unknown (an unpriced line); a negative other cost is unknown
  (``negative_cost_requires_explicit_credit``) unless its input carries
  ``credit: true`` and a ``credit_reason`` (recorded; then it is netted openly).
* A quote whose ``valid_until`` is before ``as_of`` is ``stale`` and unknown
  unless the caller passes ``accept_stale`` naming it: a mapping
  ``{input_name | fx_index | "fx[i]": reason}`` (recorded). A plain string is
  accepted only when exactly one stale item exists; with several it is refused
  (``accept_stale_string_ambiguous``), and an entry naming no stale item is refused
  (``accept_stale_names_no_stale_item``).
* An FX entry that no cost used is reported under ``unused_fx`` and never under
  ``stale``; it cannot affect a computed result.
* An input is a citation, a literal, an unknown (with who can answer) or ``derived``: a
  value a named rule (a ported Portal function) computed from cited records, recorded with
  its sources and rule.
* The record lists inputs, each arithmetic step as text, and a sha256 over the
  canonical inputs + steps + outcome, so equal inputs give an equal digest.

Cost treatment (Portal staging ``5724575``; 9):
PLX's current Portal decision is to expense freight and duty to Sage G/L (ERP-D-015, Approved
2026-09-10, ``docs/projects/business-central-erp-modernization/decision-log.md``;
``portal/src/lib/procurement/landed-gl.ts``); Vince's step 18 asks to reopen it. So every
record here carries ``cost_treatment``: an analytic estimate, never item cost.

The Portal's landed-cost / quote engine is ported in ``operating.landed_cost_portal`` (the
frozen compatibility port, re-exported here): the one engine every Workbench landed-cost and
quote-cost view uses. ``portal_quote_landed`` reproduces
``quoteProjectLanded`` from supplied facts; ``quote_landed`` adds the Workbench layer (typed
unknowns, units, currency, citations); ``assess_freight_quote`` and ``po_line_landed`` turn
forwarder quotes and purchase-order freight/duty into cited inputs (or typed unknowns) for
``compute_landed_cost``. Golden tests run the real Portal TypeScript (Node, no network, a fake
read-only database) and compare every number bit for bit.

``plan_run`` (``operating.landed_cost_plan``, re-exported here) is the engine of the
``plx.investigate`` plan ``ontology/plans/landed_cost.json`` (evidence kind ``compute``): the
same functions over a purchase order's cited projected values,
including the typed fields of the forwarder quotes that name it (``assess_freight_quote``).
Each gap of its answer names who can answer it (``who_can_answer``); ``draft_request`` supplies
only the wording of the draft requests (a freight quote request, an upstream ask) that the one
gap follow-up path (``ontology_tools_investigate.record_follow_ups``) records; nothing is sent.

No network and no writes. Citation resolution is caller-supplied:
``resolver(handle, actor) -> Mapping`` (like ``CaseStore``'s); any failure or a
non-mapping is an unresolvable citation, which fails closed to unknown.
"""

from __future__ import annotations

import math
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Mapping, Sequence

from ontology.core import ValidationError
from ontology.units import ISO_4217  # one list, there
from operating.landed_cost_base import COST_TREATMENT, LandedCostError
from operating.source_units import SourceUnitContracts
from operating.cases import (
    ExpressionError, UnitMismatch, Unit, _MAX_EXPR, _canon, _cited_float, _dec_str, _digest,
    _field, _sha, _to_decimal, bind_cited_unit, evaluate, format_unit, parse_unit)

CONTRACT = "plx.landed_cost.v1"
MAX_FX_AGE_DAYS = 7  # a rate older than this before as_of is stale (accept_stale to use it)
CATEGORIES = ("goods", "freight", "duty", "other")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_BUILTIN: Any = object()
Resolver = Callable[[str, Any], Mapping[str, Any]]


def _date(value: Any, what: str) -> date:
    if not isinstance(value, str):
        raise LandedCostError(f"date_must_be_iso_string:{what}")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise LandedCostError(f"invalid_date:{what}") from exc


class _Reader:
    def __init__(self, resolver: Resolver | None, actor: Any,
                 unit_contracts: Any = None) -> None:
        self.resolver, self.actor = resolver, actor
        self.unit_contracts = unit_contracts
        self.cache: dict[str, Mapping[str, Any] | None] = {}

    def resolve(self, handle: str) -> Mapping[str, Any] | None:
        if handle not in self.cache:
            resolved: Any = None
            if self.resolver is not None:
                try:
                    resolved = self.resolver(handle, self.actor)
                except Exception:  # noqa: BLE001 - any resolver failure fails closed
                    resolved = None
            self.cache[handle] = resolved if isinstance(resolved, Mapping) else None
        return self.cache[handle]

    @staticmethod
    def _check_currency(name: str, spec: Mapping[str, Any], unit: Unit) -> None:
        if "currency" in spec and spec["currency"] is not None:
            cur = spec["currency"]
            if not isinstance(cur, str) or cur not in ISO_4217 or \
                    not any(n == cur for n, _ in unit):
                raise UnitMismatch(f"currency_contradicts_unit:{name}")

    def _derived(self, name: str, spec: Mapping[str, Any]
                 ) -> tuple[dict[str, Any], Decimal | None, Unit, str | None]:
        """A value computed by a named rule (a ported Portal function) from cited records:
        ``{"derived": {"value": "<decimal string>", "from": [handles], "rule": str},
        "unit": ...}``. Every source handle is resolved through the caller's resolver (as the
        actor); an unresolvable one makes the input unknown. The rule names the code."""
        body = spec["derived"]
        if not isinstance(body, Mapping):
            raise LandedCostError(f"derived_must_be_mapping:{name}")
        handles = [h for h in body.get("from") or () if h is not None]
        rule = body.get("rule")
        if not handles or not all(isinstance(h, str) and h.strip() for h in handles):
            raise LandedCostError(f"derived_needs_cited_sources:{name}")
        if not isinstance(rule, str) or not rule.strip():
            raise LandedCostError(f"derived_needs_rule:{name}")
        if not isinstance(body.get("value"), str):
            raise LandedCostError(f"derived_value_must_be_decimal_string:{name}")
        unit = parse_unit(spec.get("unit"))
        self._check_currency(name, spec, unit)
        number = _to_decimal(body["value"], name)
        handles = list(dict.fromkeys(handles))
        record = {"kind": "derived", "unit": format_unit(unit),
                  "unit_basis": "caller_declared", "value": _dec_str(number),
                  "derived_from": handles, "rule": rule.strip()}
        if "valid_until" in spec:
            record["valid_until"] = _date(spec["valid_until"], f"{name}.valid_until").isoformat()
        # every source must resolve for this actor (existence and ACL), else fail closed
        digests = {}
        for handle in handles:
            resolved = self.resolve(handle)
            if resolved is None:
                record["unresolved_source"] = handle
                return record, None, unit, "derived_source_unresolvable"
            digests[handle] = _digest(resolved)
        record["derived_from_digests"] = digests
        return record, number, unit, None

    def read(self, name: str, spec: Mapping[str, Any]
             ) -> tuple[dict[str, Any], Decimal | None, Unit, str | None]:
        """-> (record, value or None, unit, unknown_reason or None)."""
        if not isinstance(spec, Mapping):
            raise LandedCostError(f"input_must_be_mapping:{name}")
        kinds = [k for k in ("citation", "literal", "unknown", "derived") if k in spec]
        if len(kinds) != 1:
            raise LandedCostError(
                f"input_needs_exactly_one_of_citation_literal_unknown_derived:{name}")
        kind = kinds[0]
        if kind == "unknown":
            reason = spec["unknown"]
            who = spec.get("who_can_answer")
            if not isinstance(reason, str) or not reason.strip():
                raise LandedCostError(f"unknown_needs_reason:{name}")
            if not isinstance(who, str) or not who.strip():
                raise LandedCostError(f"unknown_needs_who_can_answer:{name}")
            return ({"kind": "unknown", "reason": reason.strip(), "who_can_answer": who.strip()},
                    None, (), reason.strip())
        if kind == "derived":
            return self._derived(name, spec)
        if kind == "literal" or spec.get("unit") is not None:
            unit: Unit = parse_unit(spec.get("unit"))
        else:
            unit = ()  # citation without a declared unit: only a source contract can fix it
        record: dict[str, Any] = {"kind": kind}
        if kind == "literal":
            self._check_currency(name, spec, unit)
            record.update({"unit": format_unit(unit), "unit_basis": "caller_declared"})
            number = _to_decimal(spec["literal"], name)
            record["value"] = _dec_str(number)
            if "valid_until" in spec:
                record["valid_until"] = _date(spec["valid_until"], f"{name}.valid_until").isoformat()
            return record, number, unit, None
        handle = spec["citation"]
        field = spec.get("field")
        if not isinstance(handle, str) or not handle.strip() or \
                not isinstance(field, str) or not field.strip():
            raise LandedCostError(f"citation_needs_handle_and_field:{name}")
        record["citation"] = {"handle": handle, "field": field}
        resolved = self.resolve(handle)
        if resolved is None:
            if spec.get("unit") is not None:
                self._check_currency(name, spec, unit)
            record.update({"unit": format_unit(unit), "unit_basis": "caller_declared"})
            return record, None, unit, "citation_unresolvable"
        record["citation"]["digest"] = _digest(resolved)
        # The same binding rule as CaseStore.compute (one shared helper).
        unit, basis = bind_cited_unit(self.unit_contracts, handle, resolved, field, name,
                                      spec.get("unit"))
        record.update(basis)
        record["unit"] = format_unit(unit)
        self._check_currency(name, spec, unit)
        try:
            raw = _field(resolved, field)
        except ValidationError:
            return record, None, unit, "cited_field_missing_or_null"
        if isinstance(raw, float):
            number, record["source_number"] = _cited_float(raw, name)
        else:
            number = _to_decimal(raw, name)
        record["value"] = _dec_str(number)
        if "valid_until" in spec:
            record["valid_until"] = _date(spec["valid_until"], f"{name}.valid_until").isoformat()
        elif spec.get("valid_until_field"):
            try:
                record["valid_until"] = _date(_field(resolved, spec["valid_until_field"]),
                                              f"{name}.valid_until").isoformat()
            except ValidationError:
                return record, None, unit, "cited_valid_until_missing_or_invalid"
        return record, number, unit, None


CENT = Decimal("0.01")
SUB_CENT = Decimal("0.0001")
# the longest sum expression handed to the evaluator at once (its own limit)
MAX_SUM_EXPR = _MAX_EXPR


def money_text(value: Decimal, unit: Unit | str) -> str:
    """How an amount reads in a step or verdict: money rounded half-up, the value itself kept.

    An amount in one currency (``CAD``) shows at most 2 decimal places; a price per a
    non-currency unit (``CAD/kg``) at most 2 when it is 1 or more, else at most 4 (a bulk
    price below a cent stays readable). Anything else (a quantity, an exchange rate
    ``CAD/USD``, a duty rate) is shown exactly, as is an amount with no more places than
    that (``2500``, ``275.5``). The computed ``value`` of every entry keeps its full
    precision: only the text is rounded."""
    try:
        terms = parse_unit(unit) if isinstance(unit, str) else tuple(unit)
    except (ExpressionError, TypeError, ValueError):
        terms = ()   # an unreadable unit is no money unit: shown exactly
    money = [p for n, p in terms if n in ISO_4217]
    if not isinstance(value, Decimal) or not value.is_finite() or money != [1] or any(
            p > 0 for n, p in terms if n not in ISO_4217):
        return _dec_str(value) if isinstance(value, Decimal) else str(value)
    per_unit = any(p < 0 for _n, p in terms)
    places = SUB_CENT if per_unit and abs(value) < 1 else CENT
    text = _dec_str(value)   # trailing zeros dropped: 10025.000 is 10025
    if len(text.partition(".")[2]) <= -places.as_tuple().exponent:
        return text
    return format(value.quantize(places, rounding=ROUND_HALF_UP), "f")


def _calc(expr: str, values: Mapping[str, tuple[Decimal, Unit]], steps: list[str],
          label: str) -> tuple[Decimal, Unit]:
    result, unit, inexact, _ = evaluate(expr, values)
    bound = ", ".join(f"{k}={money_text(v, u)} {format_unit(u)}"
                      for k, (v, u) in sorted(values.items()))
    steps.append(f"{label}: {expr} [{bound}] = {money_text(result, unit)} {format_unit(unit)}"
                 + (" (inexact, rounded half-even at 50 digits)" if inexact else ""))
    return result, unit


def _stale_label(key: Any) -> str:
    if isinstance(key, bool):
        raise LandedCostError("accept_stale_key_invalid")
    if isinstance(key, int):
        return f"fx[{key}]"
    if isinstance(key, str) and key.strip():
        return key.strip()
    raise LandedCostError("accept_stale_key_invalid")


def _sum_calc(values: Mapping[str, tuple[Decimal, Unit]], steps: list[str],
              label: str) -> tuple[Decimal, Unit]:
    """``_calc`` of the sum of ``values`` (their order kept). A sum whose expression is longer
    than the evaluator takes (``operating.cases`` 500 characters: about 50 purchase order
    lines or 60 inputs) is added in parts that fit, each part a step of its own, then the
    parts: the same exact Decimal sum, never refused for its length. A sum that fits is one
    step, as before (so its steps and digest are unchanged)."""
    names = list(values)
    if len(" + ".join(names)) <= MAX_SUM_EXPR:
        return _calc(" + ".join(names), values, steps, label)
    groups: list[list[str]] = [[]]
    for name in names:
        if groups[-1] and len(" + ".join([*groups[-1], name])) > MAX_SUM_EXPR:
            groups.append([])
        groups[-1].append(name)
    parts: dict[str, tuple[Decimal, Unit]] = {}
    for i, group in enumerate(groups, start=1):
        parts[f"part_{i}"] = _calc(" + ".join(group), {n: values[n] for n in group}, steps,
                                   f"{label}, part {i} of {len(groups)} "
                                   f"({group[0]} .. {group[-1]})")
    return _sum_calc(parts, steps, label)


def compute_landed_cost(
        costs: Sequence[Mapping[str, Any]], quantity: Mapping[str, Any], target_currency: str,
        *, as_of: str, fx: Sequence[Mapping[str, Any]] = (),
        accept_stale: str | Mapping[Any, str] | None = None,
        resolver: Resolver | None = None, actor: Any = None,
        unit_contracts: Any = _BUILTIN) -> dict[str, Any]:
    """Compute per-unit landed cost; see ``_compute`` for the inputs.

    ``accept_stale`` names what it accepts: ``{input_name | fx_index: reason}``. A plain
    string reason is accepted only when exactly one stale item exists.
    """
    accept: dict[str, str] = {}
    sole_reason: str | None = None
    if isinstance(accept_stale, str):
        if not accept_stale.strip():
            raise LandedCostError("accept_stale_needs_a_reason")
        sole_reason = accept_stale.strip()
    elif isinstance(accept_stale, Mapping):
        for key, reason in accept_stale.items():
            if not isinstance(reason, str) or not reason.strip():
                raise LandedCostError("accept_stale_needs_a_reason")
            label = _stale_label(key)
            if label in accept:
                raise LandedCostError(f"accept_stale_duplicate_key:{label}")
            accept[label] = reason.strip()
        if not accept:
            raise LandedCostError("accept_stale_needs_a_reason")
    elif accept_stale is not None:
        raise LandedCostError("accept_stale_needs_a_reason")
    kwargs = dict(as_of=as_of, fx=fx, resolver=resolver, actor=actor,
                  unit_contracts=unit_contracts)
    if sole_reason is not None:
        probe = _compute(costs, quantity, target_currency, accept={}, **kwargs)
        labels = [item["input"] for item in probe["stale"]]
        if len(labels) > 1:
            raise LandedCostError(
                f"accept_stale_string_ambiguous:{len(labels)}_stale_items:{','.join(labels)}")
        if not labels:
            raise LandedCostError("accept_stale_names_no_stale_item")
        accept = {labels[0]: sole_reason}
    result = _compute(costs, quantity, target_currency, accept=accept, **kwargs)
    named = {item["input"] for item in result["stale"]}
    for label in accept:
        if label not in named:
            raise LandedCostError(f"accept_stale_names_no_stale_item:{label}")
    return result


def _compute(
        costs: Sequence[Mapping[str, Any]], quantity: Mapping[str, Any], target_currency: str,
        *, as_of: str, fx: Sequence[Mapping[str, Any]], accept: Mapping[str, str],
        resolver: Resolver | None, actor: Any, unit_contracts: Any) -> dict[str, Any]:
    """Compute per-unit landed cost as a reproducible record.

    ``costs``: each ``{name, category in goods|freight|duty|other, ...spec}`` where
    spec is ``{citation, field, unit, currency?}``, ``{literal, unit, currency?}``
    or ``{unknown, who_can_answer}``; optional ``valid_until`` (ISO date) or
    ``valid_until_field`` (path in the citation).
    ``quantity``: ``{literal|citation+field, unit}`` with a single-term unit (EA).
    ``unit_contracts``: a ``SourceUnitContracts`` (default builtin; None disables):
    a cited column with a contract fixes the unit (declaring it is optional, a
    different declared unit is refused ``unit_contradicts_source_contract``), and
    each input records ``unit_basis`` source_contract|caller_declared.
    ``fx``: each ``{from, to, rate|citation+field, unit "TO/FROM", as_of}``.
    """
    if not isinstance(target_currency, str) or not target_currency in ISO_4217:
        raise LandedCostError("target_currency_must_be_iso_4217_code")
    as_of_date = _date(as_of, "as_of")
    if not costs:
        raise LandedCostError("costs_required")
    if unit_contracts is _BUILTIN:
        unit_contracts = SourceUnitContracts.builtin()
    reader = _Reader(resolver, actor, unit_contracts)
    steps: list[str] = []
    unknowns: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    # quantity
    qrec, qnum, qunit, qunknown = reader.read("quantity", quantity)
    if qunknown is not None or qnum is None:
        raise LandedCostError("quantity_must_be_known")
    if len(qunit) != 1 or qunit[0][1] != 1 or qunit[0][0] in ISO_4217:
        raise UnitMismatch(f"quantity_unit_must_be_one_non_currency_unit:{format_unit(qunit)}")
    if qnum <= 0:
        raise LandedCostError("quantity_must_be_positive")
    qname = qunit[0][0]

    # fx table
    rates: dict[tuple[str, str], dict[str, Any]] = {}
    fx_used: set[str] = set()
    fx_records: list[dict[str, Any]] = []
    for i, entry in enumerate(fx):
        stale_fx: dict[str, Any] | None = None
        frm, to = entry.get("from"), entry.get("to")
        if not (isinstance(frm, str) and isinstance(to, str) and frm in ISO_4217
                and to in ISO_4217) or frm == to:
            raise LandedCostError(f"fx_needs_distinct_from_and_to_currencies:{i}")
        fx_as_of = _date(entry.get("as_of"), f"fx[{i}].as_of")
        spec = {k: v for k, v in entry.items() if k in ("citation", "field", "unit", "currency")}
        if "rate" in entry:
            spec["literal"] = entry["rate"]
        rec, num, unit, unk = reader.read(f"fx[{i}]", spec)
        if unit != parse_unit(f"{to}/{frm}") and unk is None:
            raise UnitMismatch(f"fx_unit_must_be_{to}/{frm}:{format_unit(unit)}")
        rec.update({"from": frm, "to": to, "as_of": fx_as_of.isoformat()})
        if unk is None and num is not None and num <= 0:
            unk = "fx_rate_not_positive"
        elif unk is None and fx_as_of > as_of_date:
            unk = f"fx_dated_{fx_as_of.isoformat()}_after_as_of_{as_of_date.isoformat()}"
        elif unk is None and (as_of_date - fx_as_of).days > MAX_FX_AGE_DAYS:
            # Staleness is decided where a cost uses the rate: an unused entry must not
            # leave a stale item (or need an acceptance) that affects nothing.
            stale_fx = {"input": f"fx[{i}]", "fx_as_of": fx_as_of.isoformat(), "as_of": as_of,
                        "max_age_days": MAX_FX_AGE_DAYS}
        if unk is not None:
            rec["unknown_reason"] = unk
        fx_records.append(rec)
        if (frm, to) in rates:
            raise LandedCostError(f"duplicate_fx:{frm}->{to}")
        rates[(frm, to)] = {"value": num, "unit": unit, "unknown": unk, "stale": stale_fx,
                            "rec": rec, "label": f"fx[{i}]", "decided": False}

    cost_records: list[dict[str, Any]] = []
    known: list[tuple[str, Decimal]] = []  # (name, amount in target)
    partial_ok = True
    seen: set[str] = set()
    for spec in costs:
        name, category = spec.get("name"), spec.get("category")
        if not isinstance(name, str) or not _NAME.match(name) or name in seen:
            raise LandedCostError(f"cost_needs_unique_name:{name}")
        seen.add(name)
        if category not in CATEGORIES:
            raise LandedCostError(f"cost_category_must_be_one_of_{'|'.join(CATEGORIES)}:{name}")
        rec, num, unit, unk = reader.read(name, {k: v for k, v in spec.items()
                                                 if k not in ("name", "category")})
        rec.update({"name": name, "category": category})
        cost_records.append(rec)
        if unk is None and "valid_until" in rec and \
                date.fromisoformat(rec["valid_until"]) < as_of_date:
            item = {"input": name, "valid_until": rec["valid_until"], "as_of": as_of}
            if name in accept:
                rec["stale_accepted"] = accept[name]
                stale.append({**item, "accepted_with_reason": accept[name]})
                steps.append(f"{name}: stale quote (valid_until {rec['valid_until']} < "
                             f"{as_of}) accepted: {accept[name]}")
            else:
                rec["stale"] = True
                stale.append({**item, "accepted_with_reason": None})
                unk = f"stale_quote_valid_until_{rec['valid_until']}_before_{as_of}"
        if unk is None and category == "goods" and num is not None and num <= 0:
            # A goods price of zero or less is an unpriced line (a PO sent at $0.000 before
            # the vendor priced it), never a free good.
            unk = "goods_cost_not_positive"
        if "credit" in spec and spec["credit"] is not None:
            reason = spec.get("credit_reason")
            if spec["credit"] is not True or not isinstance(reason, str) or not reason.strip():
                raise LandedCostError(f"credit_needs_true_and_credit_reason:{name}")
            if num is not None and num >= 0:
                raise LandedCostError(f"credit_requires_a_negative_amount:{name}")
        if unk is None and num is not None and num < 0:
            if spec.get("credit") is True:
                rec["negative"] = True  # an explicit credit, reason recorded, netted openly
                rec["credit"] = {"reason": spec["credit_reason"].strip()}
            else:
                unk = "negative_cost_requires_explicit_credit"
        if unk is not None:
            unknowns.append({"input": name, "category": category, "reason": unk,
                             "who_can_answer": rec.get("who_can_answer")})
            steps.append(f"{name}: UNKNOWN ({unk})")
            continue
        # normalise to a total in its own currency
        currencies = [n for n, p in unit if n in ISO_4217]
        if len(currencies) != 1:
            raise UnitMismatch(f"cost_unit_needs_exactly_one_currency:{name}:{format_unit(unit)}")
        cur = currencies[0]
        if unit == ((cur, 1),):
            amount, amount_unit = num, unit
            steps.append(f"{name}: {money_text(num, unit)} {cur} (total)")
        elif unit == parse_unit(f"{cur}/{qname}"):
            # internal operand names: a cost named like one never overwrites it (BIZ-001)
            amount, amount_unit = _calc("cost * qty", {"cost": (num, unit),
                                                       "qty": (qnum, qunit)},
                                        steps, f"{name} total")
        else:
            raise UnitMismatch(f"cost_unit_must_be_{cur}_or_{cur}/{qname}:{name}:{format_unit(unit)}")
        if cur != target_currency:
            rate = rates.get((cur, target_currency))
            if rate is not None and rate["stale"] is not None and not rate["decided"]:
                rate["decided"] = True
                label, item = rate["label"], rate["stale"]
                if label in accept:
                    rate["rec"]["stale_accepted"] = accept[label]
                    stale.append({**item, "accepted_with_reason": accept[label]})
                    steps.append(f"{label}: stale rate (as_of {item['fx_as_of']}, older than "
                                 f"{MAX_FX_AGE_DAYS} days before {as_of}) accepted: "
                                 f"{accept[label]}")
                else:
                    rate["rec"]["stale"] = True
                    stale.append({**item, "accepted_with_reason": None})
                    rate["unknown"] = f"stale_fx_rate_as_of_{item['fx_as_of']}"
                    rate["rec"]["unknown_reason"] = rate["unknown"]
            if rate is not None:
                fx_used.add(rate["label"])
            if rate is None or rate["unknown"] is not None or rate["value"] is None:
                why = "fx_missing" if rate is None else f"fx_unknown:{rate['unknown']}"
                gap = {"missing": f"fx_{cur}_to_{target_currency}", "reason": why, "input": name}
                gaps.append(gap)
                unknowns.append({"input": name, "category": category,
                                 "reason": f"{why}:{cur}->{target_currency}",
                                 "who_can_answer": None})
                steps.append(f"{name}: UNKNOWN ({why} {cur}->{target_currency})")
                continue
            amount, amount_unit = _calc("amount * fx", {"amount": (amount, amount_unit),
                                                         "fx": (rate["value"], rate["unit"])},
                                        steps, f"{name} in {target_currency} "
                                               f"(fx {cur}->{target_currency})")
        if amount_unit != parse_unit(target_currency):
            raise UnitMismatch(f"cost_not_in_target_currency:{name}:{format_unit(amount_unit)}")
        known.append((name, amount))

    result: dict[str, Any] = {"contract": CONTRACT, "target_currency": target_currency,
                              "as_of": as_of_date.isoformat(),
                              "quantity": {**qrec, "unit": format_unit(qunit)}}
    tunit = parse_unit(target_currency)
    sub_unit = tunit
    known_subtotal = None
    if known:
        values = {n: (a, tunit) for n, a in known}
        total, _ = _sum_calc(values, steps, "known subtotal" if unknowns else "total")
        known_subtotal = {"value": _dec_str(total), "unit": format_unit(sub_unit),
                          "label": "partial: excludes unknown inputs"}
    if unknowns:
        result.update({"status": "unknown", "total": None, "per_unit": None,
                       "known_subtotal": known_subtotal})
    else:
        assert known_subtotal is not None
        total = Decimal(known_subtotal["value"])
        per_unit, punit = _calc("total / qty", {"total": (total, tunit), "qty": (qnum, qunit)},
                                steps, "per-unit landed cost")
        result.update({"status": "computed",
                       "total": {"value": _dec_str(total), "unit": target_currency},
                       "per_unit": {"value": _dec_str(per_unit), "unit": format_unit(punit)},
                       "known_subtotal": None})
    unused_fx = [{"input": r["label"], "from": f, "to": t}
                 for (f, t), r in rates.items() if r["label"] not in fx_used]
    result.update({"unknowns": unknowns, "gaps": gaps, "stale": stale, "unused_fx": unused_fx,
                   "accept_stale": dict(sorted(accept.items())) if accept else None,
                   "inputs": cost_records, "fx": fx_records, "steps": steps,
                   "cost_treatment": dict(COST_TREATMENT)})
    result["digest"] = _sha(_canon({k: v for k, v in result.items()}))
    result["digest_scope"] = "sha256 over canonical inputs, fx, quantity, steps, outcome, as_of"
    return result


# --------------------------------------------------------------------------- Portal engine port
#
# The Portal's landed-cost / quote engine is ``operating.landed_cost_portal`` (the frozen
# compatibility port, golden-tested against the Portal's TypeScript). The names this module and
# its callers use are re-exported here.
from operating.landed_cost_portal import (  # noqa: E402,F401  (re-exported)
    _active_cards,
    allocate_po_freight_per_unit,
    amortise_setup_hours,
    apply_column_unit_cost_fallback,
    assembly_labour_per_unit,
    build_landed_expense_lines,
    compounding_crew_hours_per_unit,
    compounding_labour_per_unit,
    compute_quote_totals,
    customer_supplied_requirements,
    derive_ready_to_quote,
    derive_unit_price,
    derive_uptime_overhead,
    draft_cost_rows,
    estimate_unit_cost,
    fallback_matrix_unit_cost,
    fill_mass_kg,
    _finite,
    first_positive_line_std_cost,
    _gt0,
    js_number,
    js_string,
    landed_component_cost,
    line_total,
    _num,
    offer_line,
    pick_run_volume_tier,
    pick_supplier_card_tier_price,
    portal_decimal,
    PORTAL_QUOTE_DUTY_RATE,
    portal_quote_landed,
    portal_quote_landed_component,
    PORTAL_QUOTE_SOURCE,
    PORTAL_REVISION,
    PORTAL_SHORT,
    PORTAL_SOURCES,
    _pos,
    _positive,
    price_column_at_margin,
    price_margin_tiers,
    primary_routing,
    quote_facts,
    quote_labour_from_routing,
    quote_raw_cost_per_kg,
    QUOTE_RELATIONS,
    RATE_DEFAULTS,
    _raw_cost_card,
    _raw_cost_cards,
    raw_cost_per_kg,
    resolve_draft_cost,
    resolve_expected_units_override,
    resolve_margin_ladder,
    resolve_offer_margin_pct,
    resolve_posted_landed_amounts,
    resolve_quote_formulation,
    resolve_quote_hours,
    resolve_setup_standard,
    resolve_unit_price,
    rollup_bom_total,
    SUPPLY_RELATIONS,
    _truthy,
    _valid_margin,
    volume_column_labour,
    WORKBOOK_SETUP_SEED,
)


# ---- the Workbench layer: typed unknowns, units, currency, citations ---------------------------

def _unknown(code: str, subject: str, detail: str, who: str, evidence: Sequence[Any] = (),
             portal_used: Any = "n/a") -> dict[str, Any]:
    return {"code": code, "subject": subject, "detail": detail, "who_can_answer": who,
            "portal_used": portal_used,
            "evidence": [e for e in dict.fromkeys(_flatten(evidence)) if e]}


def _flatten(items: Any) -> list[Any]:
    out: list[Any] = []
    for item in items if isinstance(items, (list, tuple)) else [items]:
        if isinstance(item, (list, tuple)):
            out += _flatten(item)
        else:
            out.append(item)
    return out


def _money(value: Any, unit: str) -> dict[str, Any] | None:
    number = portal_decimal(value)
    return {"value": _dec_str(number), "unit": unit} if number is not None else None


def quote_landed(facts: Mapping[str, Any], *,
                 quote_currency: str | None, cost_currency: Mapping[str, Any] | None,
                 tariff_evidence: Mapping[str, Mapping[str, Any]] | None = None,
                 asked_volumes: Sequence[Any] | None = None) -> dict[str, Any]:
    """The Portal quote engine's result for these facts plus what it does not establish.

    ``cost_currency``: ``{"currency": ISO, "_source": handle}`` declaring the currency of
    Product.unitCost and RateSettings rates (the Portal stores none; a raw material without a
    positive unitCost is priced from its preferred card's systemCost, CAD per kg by raw-cost.ts).
    ``quote_currency``: the
    Quote's currency. Card currencies come from ``SupplierProduct.currency``. ``tariff_evidence``:
    ``{productId: {"hs_code", "duty_rate" (decimal string), "_source"}}``; without it the
    Portal's flat 6.5% duty is an unverified tariff treatment.

    Status ``computed`` only when no typed unknown is open; then ``workbench`` carries the Portal
    figures as Decimal with units. Otherwise ``workbench`` per-unit values are None and the
    unknowns say what each depends on; the ``portal`` reproduction is always returned, labelled.
    """
    if quote_currency is not None and quote_currency not in ISO_4217:
        raise LandedCostError("quote_currency_must_be_iso_4217_code")
    portal = portal_quote_landed(facts, asked_volumes=asked_volumes)
    q = portal.pop("_facts")
    evidence = portal.pop("_evidence")
    used = portal.pop("_labour")
    unknowns: list[dict[str, Any]] = []
    assumptions: list[dict[str, Any]] = []
    cited: list[Any] = []
    tariff_evidence = tariff_evidence or {}

    def cite(record: Any, path: str) -> Any:
        handle = record.get("_source") if isinstance(record, Mapping) else None
        if not handle:
            unknowns.append(_unknown("INPUT_NOT_CITED", path,
                                     "An engine input carries no source citation; it cannot be "
                                     "traced to a record.", "Workbench operator"))
        else:
            cited.append(handle)
        return handle

    project = facts.get("project") or {}
    cite(project, "project")
    rates_row = facts.get("rate_settings")
    if rates_row:
        rates_cite = cite(rates_row, "rate_settings")
        fields = {"rawMaterialsAdjustmentRate": RATE_DEFAULTS["rawMaterialsAdjustmentPct"],
                  "assemblyQuotingLaborRate": RATE_DEFAULTS["assemblyRate"],
                  "batchingQuotingLaborRate": RATE_DEFAULTS["batchingRate"]}
        for key, default in fields.items():
            if not _finite(rates_row.get(key)):
                unknowns.append(_unknown("RATE_SETTING_DEFAULTED", f"RateSettings.{key}",
                                         "The RateSettings row has no numeric value; the Portal "
                                         "prices with its code default.", "Finance",
                                         [rates_cite], portal_used=default))
            elif rates_row.get(key) < 0:
                # BIZ-004: a negative rate takes cost away; never a computed figure
                unknowns.append(_unknown("RATE_SETTING_INVALID", f"RateSettings.{key}",
                                         "The RateSettings rate is negative.", "Finance",
                                         [rates_cite], portal_used=rates_row.get(key)))
        for k, v in sorted(rates_row.items(), key=lambda kv: str(kv[0])):
            if isinstance(k, str) and k.startswith("overheadPer") and _finite(v) and v < 0:
                unknowns.append(_unknown("RATE_SETTING_INVALID", f"RateSettings.{k}",
                                         "The overhead rate is negative.", "Finance",
                                         [rates_cite], portal_used=v))
        if not any(isinstance(k, str) and k.startswith("overheadPer") and _finite(v)
                   for k, v in rates_row.items()):
            unknowns.append(_unknown("RATE_SETTING_DEFAULTED", "RateSettings.overheadPer*",
                                     "No overhead rate; the Portal uses its code default.",
                                     "Finance", [rates_cite],
                                     portal_used=RATE_DEFAULTS["overheadRate"]))
        if not _valid_margin(rates_row.get("marginPercent")):
            unknowns.append(_unknown("RATE_SETTING_DEFAULTED", "RateSettings.marginPercent",
                                     "No valid target margin; the Portal uses its code default.",
                                     "Finance", [rates_cite],
                                     portal_used=RATE_DEFAULTS["marginPct"]))
    else:
        rates_cite = None
        unknowns.append(_unknown("RATE_SETTINGS_DEFAULTED", "RateSettings",
                                 "No RateSettings row is cited: the Portal prices with its code "
                                 "defaults (raw-material adjustment 10%, batching $32/h, assembly "
                                 "$20.90/h, overhead $31.30/h, margin 30%), not governed rates.",
                                 "Finance", [PORTAL_SOURCES["rate_defaults"]],
                                 portal_used="code defaults"))
    cur = (cost_currency or {}).get("currency")
    if cur not in ISO_4217 or not (cost_currency or {}).get("_source"):
        cur = None
        unknowns.append(_unknown("COST_CURRENCY_UNDECLARED", "Product.unitCost / RateSettings",
                                 "The Portal stores no currency for Product.unitCost or the "
                                 "RateSettings rates; nothing cited states it.", "Finance",
                                 [rates_cite], portal_used="no currency"))
    else:
        cited.append(cost_currency["_source"])
    if quote_currency is None:
        unknowns.append(_unknown("QUOTE_CURRENCY_UNKNOWN", "quote", "No quote currency is "
                                 "cited; a cost cannot be stated in it.", "Sales"))
    elif cur is not None and cur != quote_currency:
        unknowns.append(_unknown("FX_NOT_APPLIED", "quote",
                                 f"Costs are in {cur} and the quote is in {quote_currency}; the "
                                 "Portal converts nothing (RateSettings.fxRateCadUsd is unused).",
                                 "Finance", [cost_currency.get("_source")],
                                 portal_used="no conversion"))
    for name in q["unread"]:
        unknowns.append(_unknown("RECORDS_NOT_READ", name,
                                 f"{QUOTE_RELATIONS[name]} was not read; the Portal's own query "
                                 "result is unknown (reproduced as its failed read).",
                                 "Workbench operator"))
    if any(line["supplySource"] == "PLX" for line in q["componentLines"]):
        # the line cost evidence's own reads (BIZ-010): a failed one is unavailable
        # evidence, never "no PO history" or "no stock row"
        for name in ("purchase_order_lines", "inventory"):
            if facts.get(name) is None:
                unknowns.append(_unknown("RECORDS_NOT_READ", name,
                                         f"{name} was not read; the line cost evidence "
                                         "(last PO price, stock on hand) is unavailable, "
                                         "not absent.", "Workbench operator"))
    if q["formulationPin"]:
        cite(q["formulationPin"], "formula pin")
    formulation = q["formulation"]
    if q["resolutionSource"] in ("none", "pin_unresolved"):
        unknowns.append(_unknown("FORMULA_NOT_PRICED", "formulation",
                                 f"Formula resolution is '{q['resolutionSource']}': the Portal "
                                 "prices no bulk.", "Product Development", portal_used=0))
    customer_raws = {m["key"] for m in q["customerSupplied"] if m["kind"] == "raw"}
    if formulation:
        cite(formulation, "formulation")
        # BIZ-004: every inclusion is a number from 0 to 100 and they make the whole
        # formula, else the bulk cost leaves part of it out (or counts it twice)
        shares = [ing.get("percentage") for ing in formulation.get("ingredients") or []]
        if shares and all(_finite(p) and 0 <= p <= 100 for p in shares):
            total = sum(Decimal(str(p)) for p in shares)
            if abs(total - 100) > Decimal("0.5"):
                unknowns.append(_unknown("FORMULA_COMPOSITION_INCOMPLETE", "formulation",
                                         f"The inclusions sum to {total}%, not 100%; the "
                                         "bulk is priced on what is listed.",
                                         "Product Development",
                                         [formulation.get("_source")],
                                         portal_used=float(total)))
        for i, ing in enumerate(formulation.get("ingredients") or []):
            product = ing.get("product") or {}
            subject = product.get("name") or f"ingredient {i}"
            cite(product, f"formulation.ingredients[{i}].product")
            pct = ing.get("percentage")
            if not (_finite(pct) and 0 <= pct <= 100):
                unknowns.append(_unknown("INCLUSION_PERCENT_INVALID", subject,
                                         f"The ingredient's inclusion is {pct!r}, not a "
                                         "percentage from 0 to 100; the Portal prices it at "
                                         "0.", "Product Development",
                                         [ing.get("_source"), formulation.get("_source")],
                                         portal_used=0))
            if ing.get("productId") in customer_raws:
                continue  # priced at $0 (G9); the supply basis is cited below
            card = None if _pos(product.get("unitCost")) is not None else \
                _raw_cost_card(_raw_cost_cards(product))
            if card is not None:
                cite(card, f"{subject}.supplierProducts (raw cost)")
                if cur is not None and cur != "CAD":
                    unknowns.append(_unknown(
                        "FX_NOT_APPLIED", subject, f"The raw is priced from its preferred card's "
                        f"systemCost, CAD per kg, among costs in {cur}; the Portal converts "
                        "nothing.", "Finance", [card.get("_source"), PORTAL_SOURCES["raw_cost"]],
                        portal_used="no conversion"))
            elif _pos(product.get("unitCost")) is None:
                unknowns.append(_unknown("RAW_MATERIAL_COST_MISSING", subject,
                                         "Product.unitCost is missing or not positive and no "
                                         "preferred active card has a systemCost; the Portal "
                                         "prices it at 0.", "PROC",
                                         [product.get("_source")], portal_used=0))
            uom = product.get("uom")
            if not (isinstance(uom, str) and uom.strip().lower() in ("kg", "kgs", "kilogram")):
                unknowns.append(_unknown(
                    "RAW_MATERIAL_COST_UNIT_UNVERIFIED", product.get("name") or f"ingredient {i}",
                    "The Portal treats Product.unitCost as per kg with no unit conversion; the "
                    f"product's unit of measure is {uom!r}.", "PROC",
                    [product.get("_source")], portal_used="per kg"))
    if q["fillWeightG"] is None or not q["fillWeightG"] > 0:
        unknowns.append(_unknown("FILL_WEIGHT_MISSING", "bom.fillWeightG",
                                 "The ASSEMBLY BOM has no fill weight; the Portal prices the bulk "
                                 "at 0 (census blocker).", "Product Development",
                                 [(facts.get("bom") or {}).get("_source")], portal_used=0))
    if not (_finite(q["specificGravity"]) and q["specificGravity"] > 0):
        unknowns.append(_unknown("SPECIFIC_GRAVITY_DEFAULTED", "formulation.specificGravity",
                                 "No specific gravity; the Portal uses 1.00.", "R&D",
                                 [(formulation or {}).get("_source")], portal_used=1))
    bom = q["bom"]
    if q["bomLink"]:
        cite(q["bomLink"], "assembly BOM link")
    if not bom or not q["componentLines"]:
        unknowns.append(_unknown("PACKAGING_BOM_MISSING", "bom", "No ASSEMBLY BOM lines; the "
                                 "Portal prices packaging at 0.", "Product Development",
                                 [(bom or {}).get("_source")], portal_used=0))
    else:
        cite(bom, "bom")
    raw_lines = [line for line in (bom or {}).get("lines") or [] if line.get("deletedAt") is None]
    for raw, line in zip(raw_lines, q["componentLines"]):
        subject = f"packaging line {line['lineName']}"
        cite(raw, f"bom.lines[{line['lineKey']}]")
        if not _truthy(raw.get("quantity")):
            unknowns.append(_unknown("PACKAGING_QTY_DEFAULTED", subject, "BOM line quantity is "
                                     "0 or missing; the Portal uses 1.", "Product Development",
                                     [raw.get("_source")], portal_used=1))
        elif not (_finite(raw.get("quantity")) and raw.get("quantity") > 0):
            # BIZ-004: a negative (or non-numeric) quantity takes cost away
            unknowns.append(_unknown("PACKAGING_QTY_INVALID", subject, "BOM line quantity "
                                     f"is {raw.get('quantity')!r}, not a positive number.",
                                     "Product Development", [raw.get("_source")],
                                     portal_used=raw.get("quantity")))
        if line["supplySource"] != "PLX":
            continue  # priced at $0 (G9); the supply basis is cited below
        if not line["linked"]:
            unknowns.append(_unknown("PACKAGING_LINE_UNSOURCED", subject,
                                     "The line is priced from estimatedUnitCost / cost, without "
                                     "a supplier card, duty, uplift or freight.", "PROC",
                                     [raw.get("_source")], portal_used=line["pricedUnitCost"]))
            continue
        product = raw.get("product") or {}
        cards = _active_cards(product)
        card = cards[0] if cards else None
        if card is None:
            unknowns.append(_unknown("SUPPLIER_CARD_MISSING", subject, "No active supplier card; "
                                     "the Portal lands line.cost (or 0) with duty and uplift.",
                                     "PROC", [raw.get("_source")],
                                     portal_used=line["landed"]["tierPrice"]))
        else:
            cite(card, f"{subject}.supplierProducts[0]")
            if not (_finite(card.get("unitCost")) and card.get("unitCost") > 0):
                unknowns.append(_unknown("SUPPLIER_PRICE_MISSING", subject, "The supplier card "
                                         "price is missing or not positive.", "PROC",
                                         [card.get("_source")], portal_used=0))
            if card.get("deliveryCost") is None or not _num(card.get("deliveryCost")) > 0:
                unknowns.append(_unknown("FREIGHT_PER_COMPONENT_MISSING", subject,
                                         "SupplierProduct.deliveryCost is missing or zero; the "
                                         "Portal adds no freight.", FORWARDER,
                                         [card.get("_source")], portal_used=0))
            card_cur = card.get("currency")
            if card_cur not in ISO_4217:
                unknowns.append(_unknown("CARD_CURRENCY_UNKNOWN", subject, "The supplier card "
                                         "carries no currency.", "PROC",
                                         [card.get("_source")], portal_used="no currency"))
            elif quote_currency is not None and card_cur != quote_currency:
                unknowns.append(_unknown("FX_NOT_APPLIED", subject,
                                         f"The card price is in {card_cur} and the quote in "
                                         f"{quote_currency}; the Portal's 1.38 uplift is a "
                                         "workbook factor, not a cited exchange rate.", "Finance",
                                         [card.get("_source")], portal_used="no conversion"))
            per = card.get("uomPerSellingUnit")
            if per is not None and not (_finite(per) and float(per) == 1.0):
                unknowns.append(_unknown("SELLING_UNIT_NOT_COMPONENT", subject,
                                         f"The card price is per selling unit of {per!r} "
                                         "components; the Portal uses it per component.",
                                         "PROC", [card.get("_source")],
                                         portal_used="per component"))
            if not _truthy(card.get("incoterms")) and \
                    not _truthy((card.get("supplier") or {}).get("incoterms")):
                unknowns.append(_unknown("INCOTERMS_MISSING", subject, "No Incoterms on the card "
                                         "or supplier; who pays freight and duty is unknown.",
                                         "PROC", [card.get("_source")]))
            if not _truthy(card.get("countryOfOrigin")):
                unknowns.append(_unknown("ORIGIN_MISSING", subject, "No country of origin; duty "
                                         "treatment cannot be determined.", "PROC",
                                         [card.get("_source")]))
        tariff = tariff_evidence.get(raw.get("productId")) if raw.get("productId") else None
        if tariff and tariff.get("_source") and tariff.get("duty_rate") is not None:
            cited.append(tariff["_source"])
            if Decimal(str(tariff["duty_rate"])) != Decimal("0.065"):
                unknowns.append(_unknown("PORTAL_DUTY_RATE_DIFFERS", subject,
                                         f"The cited tariff treatment (HS {tariff.get('hs_code')}, "
                                         f"rate {tariff['duty_rate']}) differs from the Portal's "
                                         "flat 0.065.", "FIN",
                                         [tariff["_source"]], portal_used=0.065))
        else:
            unknowns.append(_unknown("TARIFF_TREATMENT_UNVERIFIED", subject,
                                     "The Portal applies a flat 6.5% duty (workbook default) "
                                     "whatever the HS code; no cited classification and rate "
                                     "confirm it" + ("" if _truthy(product.get("hsCode")) else
                                                     " and Product.hsCode is missing") + ".",
                                     "FIN",
                                     [product.get("_source"), (card or {}).get("_source")],
                                     portal_used=0.065))
    labour = portal["labour"]
    key = q["project"].get("projectKey")
    routing, drafts = used["routing"], used["drafts"] or []
    for process in ("assembly", "compounding"):
        if labour[f"{process}Source"] == "missing":
            unknowns.append(_unknown("LABOUR_HOURS_MISSING", f"{process} hours",
                                     f"No {process} hours (no draft tier row or primary routing "
                                     "is cited); the Portal prices them at 0.", "Operations",
                                     portal_used=0))
    if portal["overhead"]["source"] == "missing":
        unknowns.append(_unknown("OVERHEAD_BASIS_MISSING", "overhead", "No assembly hours to "
                                 "absorb overhead over; the Portal prices it at 0.", "Finance",
                                 portal_used=0))
    if routing is not None:
        cite(routing, "fg_routing")
    for row in drafts:
        cite(row, "draft_cost_override")
    def basis_of(tier: Any, assembly: str, compounding: str) -> list[Mapping[str, Any]]:
        """The records that priced the hours (routing or the tier's draft row per process)."""
        rows = []
        for process, source in (("assembly", assembly), ("compounding", compounding)):
            row = routing if source == "routing" else \
                resolve_draft_cost(drafts, key, process, tier) if source == "draft_tier" else None
            rows += [row] if row is not None and row not in rows else []
        return rows

    basis_rows = basis_of(labour["pricedTierCode"], labour["assemblySource"],
                          labour["compoundingSource"])
    basis = [row["_source"] for row in basis_rows if row.get("source") != "seed"
             and row.get("_source")]
    for column in portal["volumeMatrix"]:
        col = column["labour"]
        crew = col["compoundingBasis"] == "crew_per_charge"
        basis_rows += [row for row in basis_of(
            col["tierCode"], col["assemblySource"],
            "draft_tier" if crew else col["compoundingSource"]) if row not in basis_rows]
    seeded = [row for row in basis_rows if row.get("source") == "seed"]
    if seeded:
        unknowns.append(_unknown("LABOUR_FROM_SEED_DEFAULTS", "labour", "Priced hours come "
                                 "from template-seeded rows (seed defaults), not an engineered "
                                 "routing or crew standard.", "Operations",
                                 [row.get("_source") for row in seeded]))

    def standard_used(standard: Mapping[str, Any], subject: str) -> None:
        for row in standard["matched"]:
            if row is not WORKBOOK_SETUP_SEED:
                cite(row, "setup_standard")
        if standard["source"] == "seed":
            unknowns.append(_unknown("SETUP_STANDARD_DEFAULTED", subject, "The minutes are the "
                                     "workbook wedge seed, not a measured setup standard.",
                                     "Operations", [PORTAL_SOURCES["setup_standard"]],
                                     portal_used=standard["setupMinutes"]
                                     if subject.startswith(("setup", "routing"))
                                     else standard["changeoverMinutes"]))

    plans = facts.get("vessel_plans") or {}
    # routed hours carry the routing's setup minutes when it has a crew and a run quantity
    routing_crew = _gt0((routing or {}).get("totalOperators"))
    routing_setup_used = routing_crew and _gt0(q["runQty"]) and \
        "routing" in (labour["assemblySource"], labour["compoundingSource"])
    for column in portal["volumeMatrix"]:
        units = js_string(column["askedUnits"])
        at = f" at {units} units"
        col = column["labour"]
        for process in ("assembly", "compounding"):
            if col[f"{process}HoursPerUnit"] is None and \
                    labour[f"{process}HoursPerUnit"] is not None:
                unknowns.append(_unknown("LABOUR_HOURS_MISSING", f"{process} hours{at}",
                                         f"No {process} hours for this asked volume (its "
                                         "run-volume tier has no draft row or routing); the "
                                         "Portal prices them at 0 in this column.", "Operations",
                                         portal_used=0))
        if column["overhead"]["source"] == "missing" and portal["overhead"]["source"] != "missing":
            unknowns.append(_unknown("OVERHEAD_BASIS_MISSING", "overhead" + at,
                                     "No assembly hours for this asked volume; the Portal prices "
                                     "its overhead at 0.", "Finance", portal_used=0))
        priced = col["assemblyHoursPerUnit"] is not None
        if column["vesselCharges"] is not None:
            cite(plans[units], "vessel plan" + at)
        elif priced or col["compoundingHoursPerUnit"] is not None:
            unknowns.append(_unknown("CHARGE_COUNT_UNDERIVED", "vessel charges" + at,
                                     "No vessel plan is cited (the compounding standard and the "
                                     "vessel derivation are not ported); the Portal amortises "
                                     "setup and changeover over one charge and prices compounding "
                                     "per unit when it has no vessel pick.",
                                     "R&D (compounding standard)",
                                     [PORTAL_SOURCES["volume_column"]], portal_used=1))
        routing_setup_used = routing_setup_used or (priced and col["setupSource"] == "routing") \
            or (routing_crew and col["compoundingBasis"] == "per_unit_fallback"
                and col["compoundingSource"] == "routing")
        if priced and col["setupSource"].startswith("setup-standard:"):
            standard_used(used["setup_standard"], "setup minutes" + at)
        if priced and (column["vesselCharges"] or 1) > 1:
            standard_used(used["setup_standard"], "changeover minutes" + at)
        # BIZ-004: the supplier card tier a column priced from is a cited, positive price
        # (the card's base price is checked above; a tier is its own row)
        cards = {line["lineKey"]: line.get("card") for line in q["componentLines"]}
        for comp in column["componentLines"]:
            if comp.get("tierMinQty") is None:
                continue
            tiers = (cards.get(comp["lineKey"]) or {}).get("tiers") or []
            tier = next((t for t in tiers if t["minQty"] == comp["tierMinQty"]), None)
            subject = f"packaging line {comp['lineName']}{at}"
            if tier is None or not tier.get("_source"):
                unknowns.append(_unknown("INPUT_NOT_CITED", subject,
                                         "The supplier card tier this volume is priced from "
                                         "carries no source citation.", "Workbench operator"))
            else:
                cited.append(tier["_source"])
            price = (tier or {}).get("unitCost")
            if not (_finite(price) and price > 0):
                unknowns.append(_unknown("SUPPLIER_TIER_PRICE_INVALID", subject,
                                         f"The supplier card tier price is {price!r}, not a "
                                         "positive number.", "PROC",
                                         [(tier or {}).get("_source")], portal_used=price))
    fill = used["routing_fill"]
    if fill and "standardSetupMinutes" in fill["fields"] and routing_setup_used:
        standard_used(fill["standard"], "routing setup minutes")
    for material in q["customerSupplied"]:
        subject = f"customer-supplied {material['kind']} {material['name']}"
        basis_rows = q["supplyBasis"][(material["kind"], material["key"])]
        for row in basis_rows:
            cite(row, subject)
        if material["supplySource"] == "CUSTOMER_PENDING":
            unknowns.append(_unknown("CUSTOMER_SUPPLY_PENDING", subject,
                                     "A customer-path sourcing request is still open: the Portal "
                                     "prices the material at $0 before the customer component is "
                                     "approved, so who supplies it is not settled.",
                                     "Product Development (sourcing)",
                                     [row.get("_source") for row in basis_rows], portal_used=0))
    if q["customerSupplied"]:
        assumptions.append({"code": "CUSTOMER_SUPPLIED_AT_ZERO", "detail": "Materials the "
                            "customer supplies (an active customer-supplied item row, or an "
                            "open customer-path request) are priced at $0 with no fee (G-D4); "
                            "customerSupplied lists the quantity the customer must send",
                            "source": PORTAL_SOURCES["customer_supplied"]})
    for row in facts.get("purchase_order_lines") or []:
        cite(row, "purchase_order_lines")
    for row in facts.get("inventory") or []:
        cite(row, "inventory")
    assumptions += [
        {"code": "PORTAL_LANDED_UPLIFT", "detail": "Each card-backed component is landed as "
         "(tier + round4(tier × 0.065)) × 1.38 + deliveryCost, the Portal's hard defaults "
         "(RateSettings tariffRate / landedMultiplier have no effect). The 1.38 uplift is "
         "accepted as current Portal behaviour (a workbook factor, not evidence of any cost); "
         "the 0.065 duty is not: each line carries TARIFF_TREATMENT_UNVERIFIED until a cited "
         "classification confirms it",
         "source": PORTAL_QUOTE_SOURCE},
        {"code": "LABOUR_HOURS_DERIVED", "detail": "Hours are derived as the Portal derives "
         "them: the priced tier's draft rows (labourPerUnit, headcount ÷ BPM, crew × shift ÷ "
         "yield), else the primary routing with its setup amortised, setup minutes filled from "
         "the setup standards; each asked volume's vessel charge count is an input (the "
         "compounding standard and vessel derivation are not ported)",
         "source": PORTAL_SOURCES["quote_hours"]}]
    unit = f"{cur}/EA" if cur else "EA^-1 (currency undeclared)"
    status = "computed" if not unknowns and quote_currency else "unknown"
    work = None
    if status == "computed":
        work = {"unit_cost": _money(portal["estimate"]["totalCostPerUnit"], unit),
                "sell_price": _money(portal["estimate"]["sellPricePerUnit"], unit),
                "columns": [{"asked_units": _money(c["askedUnits"], "EA"),
                             "unit_cost": _money(c["unitCost"], unit),
                             "margin_tiers": [{"margin_pct": _money(t["marginPct"], "%"),
                                               "sell_price": _money(t["sellPricePerUnit"], unit)}
                                              for t in c["marginTiers"]]}
                            for c in portal["volumeMatrix"]]}
    result = {"contract": "plx.portal_landed_quote.v1", "status": status,
              "portal_revision": PORTAL_REVISION, "portal_sources": dict(PORTAL_SOURCES),
              "label": "Portal quote engine reproduction (isolated recomputation of the Portal's "
                       "arithmetic from cited facts), not Portal output and not item cost",
              "currency": {"cost": cur, "quote": quote_currency},
              "portal": portal, "workbench": work,
              "labour": {"is_placeholder": not basis, "basis": list(dict.fromkeys(basis))},
              "unknowns": unknowns, "assumptions": assumptions,
              "inputs_cited": sorted(set(_flatten(cited)) - {None}),
              "line_evidence_sources": {pid: v.get("_source") for part in evidence.values()
                                        for pid, v in part.items()},
              "cost_treatment": dict(COST_TREATMENT)}
    result["digest"] = _sha(_canon(result))
    return result


# ---- freight quotes (LOG-10) and PO landed inputs (packaging case) -----------------------------

FREIGHT_COMPONENTS = ("origin_charges", "main_carriage", "destination_charges",
                      "customs_brokerage", "duties_and_taxes", "surcharges_contingencies")
TOTAL_FREIGHT = ("origin_charges", "main_carriage", "destination_charges")


def assess_freight_quote(quote: Mapping[str, Any], *, as_of: str,
                         basis_count: Any = None) -> dict[str, Any]:
    """Whether a cited forwarder quote can stand as the freight of a landed cost.

    ``quote``: ``{_source, amount (decimal string: the price per basis, so a stated total goes
    with basis "shipment"), currency, basis ("shipment" or a basis unit such as "40HQ", "CBM",
    "KG", "PALLET"), includes [...], excludes [...], valid_until, label?}``; components are
    ``FREIGHT_COMPONENTS``. The scope comes from the cited text, never from a file name: a
    label saying "all-in" does not add components. A quote is usable as the total freight only
    when it includes origin, main carriage and destination charges, is valid on ``as_of`` and,
    when priced per basis unit, the count of that unit is cited (``basis_count``: ``{count (a
    positive int or Decimal), _source}``). Duties and taxes are never freight: excluded ones
    stay a separate duty input.
    """
    unknowns: list[dict[str, Any]] = []
    source = quote.get("_source")
    if not source:
        raise LandedCostError("freight_quote_needs_source")
    try:
        amount = Decimal(str(quote.get("amount")))
    except (ArithmeticError, ValueError):
        raise LandedCostError("freight_quote_amount_invalid") from None
    if not amount.is_finite() or amount <= 0:
        raise LandedCostError("freight_quote_amount_invalid")
    currency = quote.get("currency")
    if currency not in ISO_4217:
        raise LandedCostError("freight_quote_currency_must_be_iso_4217")
    includes = set(quote.get("includes") or ())
    excludes = set(quote.get("excludes") or ())
    bad = (includes | excludes) - set(FREIGHT_COMPONENTS)
    if bad or includes & excludes:
        raise LandedCostError("freight_quote_scope_invalid:" + ",".join(sorted(bad or
                                                                             includes & excludes)))
    missing = [c for c in TOTAL_FREIGHT if c not in includes]
    if missing:
        unknowns.append(_unknown("FREIGHT_SCOPE_PARTIAL", "freight quote",
                                 "The quote covers " + (", ".join(sorted(includes)) or "nothing "
                                 "stated") + "; missing " + ", ".join(missing) + ". It is not the "
                                 "total freight.", FORWARDER, [source]))
    label = quote.get("label") or ""
    if "all-in" in label.lower().replace("all in", "all-in") and (missing or excludes):
        unknowns.append(_unknown("FREIGHT_LABEL_CONTRADICTS_SCOPE", "freight quote",
                                 f"The name says 'all-in' ({label}) but the quote text excludes "
                                 + (", ".join(sorted(excludes)) or ", ".join(missing))
                                 + "; the cited scope wins.", FORWARDER,
                                 [source]))
    if "surcharges_contingencies" in excludes:
        unknowns.append(_unknown("FREIGHT_EXCLUDES_SURCHARGES", "freight quote",
                                 "The quote excludes listed surcharges / contingencies; the "
                                 "freight charged can exceed it.", FORWARDER,
                                 [source]))
    duty_excluded = "duties_and_taxes" in excludes or "duties_and_taxes" not in includes
    valid_until = quote.get("valid_until")
    if not valid_until:
        unknowns.append(_unknown("FREIGHT_QUOTE_VALIDITY_UNKNOWN", "freight quote",
                                 "No validity date is cited.", FORWARDER,
                                 [source]))
    elif _date(valid_until, "valid_until") < _date(as_of, "as_of"):
        unknowns.append(_unknown("FREIGHT_QUOTE_EXPIRED", "freight quote",
                                 f"Valid until {valid_until}, before {as_of}: re-quote needed.",
                                 FORWARDER, [source]))
    basis = quote.get("basis") or "shipment"
    count = None
    if basis != "shipment":
        given = basis_count.get("count") if isinstance(basis_count, Mapping) else None
        if isinstance(given, (int, Decimal)) and not isinstance(given, bool) and \
                basis_count.get("_source") and Decimal(given).is_finite() and given > 0:
            count = Decimal(given)
        else:
            unknowns.append(_unknown("FREIGHT_BASIS_COUNT_UNKNOWN", "freight quote",
                                     f"Priced per {basis}; how many are needed is not cited.",
                                     FORWARDER, [source]))
    total = amount * count if count is not None else (amount if basis == "shipment" else None)
    usable = not unknowns
    return {"source": source, "amount": _dec_str(amount),
            "unit": f"{currency}/{basis}" if basis != "shipment" else currency,
            "includes": sorted(includes), "excludes": sorted(excludes),
            "usable_as_total_freight": usable,
            "total_freight": {"value": _dec_str(total), "unit": currency,
                              "basis_count_source": (basis_count or {}).get("_source")}
            if usable and total is not None else None,
            "duties_and_taxes_separate": duty_excluded, "unknowns": unknowns}


def freight_cost_input(assessment: Mapping[str, Any]) -> dict[str, Any]:
    """A ``compute_landed_cost`` freight input from an assessed quote: derived and cited when
    usable, else unknown with the first open reason."""
    if assessment["usable_as_total_freight"]:
        return {"name": "freight", "category": "freight",
                "unit": assessment["total_freight"]["unit"],
                "derived": {"value": assessment["total_freight"]["value"],
                            "from": [assessment["source"],
                                     assessment["total_freight"]["basis_count_source"]],
                            "rule": "cited forwarder quote covering origin, main carriage and "
                                    "destination, valid on as_of"}}
    first = assessment["unknowns"][0]
    return {"name": "freight", "category": "freight",
            "unknown": f"{first['code']}: {first['detail']}",
            "who_can_answer": first["who_can_answer"]}


def _actual_line_duty(actual: Mapping[str, Any], line: Mapping[str, Any]) -> Decimal | None:
    try:
        rate = Decimal(str(actual["rate"]))
    except (ArithmeticError, KeyError):
        return None
    price, qty = js_number(line.get("unitPrice")), js_number(line.get("quantity"))
    if price is None or qty is None or not math.isfinite(price) or not math.isfinite(qty) \
            or not rate.is_finite() or rate < 0 or price < 0 or qty <= 0:
        return None
    return rate * Decimal(repr(price)) * Decimal(repr(qty))


def po_line_landed(po: Mapping[str, Any], lines: Sequence[Mapping[str, Any]], line_id: Any,
                   *, tariff: Mapping[str, Any] | None = None,
                   receipt: Mapping[str, Any] | None = None,
                   receipts_read: int = 0,
                   incoterm: Mapping[str, Any] | None = None,
                   actual_duty: Mapping[str, Any] | None = None,
                   actual_freight: Mapping[str, Any] | None = None,
                   receipt_withheld: Sequence[str] = (),
                   header_withheld: Mapping[str, Sequence[str]] | None = None
                   ) -> dict[str, Any]:
    """Freight, duty and receipt-actual inputs for one purchase-order line.

    ``po``: ``{_source, shippingCost, dutiesCost}`` (header estimates, ERP-D-015: PO header =
    estimate, receipt = actual); ``lines``: every line of the same PO, ``[{id, _source,
    unitPrice, quantity, quantityReceived}]``; ``tariff``: an optional cited classification
    ``{hs_code, duty_rate, _source}`` for the line's product.

    Freight is derived from the Portal's ``allocatePoFreightPerUnit`` only when EVERY line of
    the PO is fully received: the Portal divides the header freight by RECEIVED value, so with
    unreceived siblings a received line would carry the whole shipment's freight. An absent or
    zero header shipping cost is an unknown, not a free shipment. Duty stays a typed unknown
    (TARIFF_TREATMENT_UNVERIFIED) unless a cited classification and rate support it, as in
    ``quote_landed``; even then a header duty estimate is used only on a single-line PO (the
    Portal has no per-line duty allocation). The receipt actual, which the Portal posts over the
    PO estimate (``resolvePostedLandedAmounts``), is a typed unknown (``receipt_actual``) unless
    the caller read the PO's receipt: ``receipt`` ``{_source, freightCost, dutiesCost}`` (the one
    receipt carrying the actuals, in the PO's currency, which the caller checks). Then
    ``receipt_actual`` is None and an actual above zero replaces the estimate: freight is the
    Portal's allocation of the posted freight, and duty is the posted actual on a single-line PO
    (what was paid needs no tariff classification; a multi-line PO still has no per-line duty
    allocation). ``receipts_read``: how many receipts of the purchase order the caller read
    when ``receipt`` is None; at least one means the receipt was read and records no freight or
    duty actual (RECEIPT_ACTUAL_NOT_RECORDED), not that no receipt was read. The Portal's own
    readings stay in ``portal_allocation`` / ``posted_if_received`` for comparison.
    ``incoterm`` (``operating.hs_tariff.resolve_incoterm``): a cost the term
    leaves with the seller is a cited zero, not an unknown; a cost the buyer bears, or no
    stated term, changes nothing. The term, its rule status (provisional) and the tariff's
    ``provisional_rules`` come back in ``provisional_rules``.
    ``actual_duty`` (``operating.duty_actual.actual_duty_for_po_line``): the PO's effective duty
    rate from recorded broker/duty charges. It applies after a receipt actual and before any
    tariff rate or header estimate; the line's duty is the rate times the line's goods value
    (pro rata on a multi-line PO), labelled actual and, when the account also holds brokerage,
    an upper bound.
    ``actual_freight`` (``operating.duty_actual.actual_freight_for_po_line``): the PO's
    effective freight rate from recorded inbound freight charges. It applies after a receipt
    actual and before the header estimate; the line's freight is the rate times the line's
    goods value (``basis`` actual_recorded), so it needs no received-value allocation.
    ``receipt_withheld``: the markings the reader lacks on the receipt's freight and duty
    actuals. With no usable receipt, the receipt actual is then RECEIPT_ACTUAL_WITHHELD (who:
    FIN), never 'the receipt records none': the reader cannot see whether it does.
    ``header_withheld``: per part (``freight``, ``duty``), the markings the reader lacks on the
    header's estimate. A header estimate it is not shown is then PO_FREIGHT_ESTIMATE_WITHHELD /
    PO_DUTY_ESTIMATE_WITHHELD (who: FIN), never 'the header records none'.
    """
    header_withheld = header_withheld or {}
    shipping = js_number(po.get("shippingCost"))
    duty = js_number(po.get("dutiesCost"))
    receipt_freight, receipt_duty = ((receipt or {}).get(k) for k in ("freightCost", "dutiesCost"))
    posted = resolve_posted_landed_amounts(receipt_freight, receipt_duty, shipping, duty)
    # resolvePostedLandedAmounts: an actual above zero beats the estimate
    freight_actual, duty_actual = _positive(receipt_freight), _positive(receipt_duty)
    view = [{"id": x.get("id"), "unitPrice": js_number(x.get("unitPrice")),
             "quantityReceived": js_number(x.get("quantityReceived"))} for x in lines]
    target = next((x for x in lines if x.get("id") == line_id), None)
    if target is None:
        raise LandedCostError("po_line_not_in_lines")
    if freight_actual:
        shipping = posted["freight"]
    portal = allocate_po_freight_per_unit(shipping, view, line_id)
    sources = [receipt["_source"] if freight_actual else po.get("_source")] + \
        [x.get("_source") for x in lines]

    def fully_received(line: Mapping[str, Any]) -> bool:
        received, ordered = js_number(line.get("quantityReceived")), js_number(line.get("quantity"))
        return received is not None and ordered is not None and ordered > 0 and received == ordered

    freight: dict[str, Any]
    share = None if freight_actual or not actual_freight else \
        _actual_line_duty(actual_freight, target)
    if share is not None:
        freight = {"derived": {
            "value": _dec_str(share), "per_quantity_unit": False,
            "from": [actual_freight["_source"], target.get("_source")],
            "basis": "actual_recorded",
            "rule": f"actual recorded freight: PO effective rate {actual_freight['rate']} "
                    "(inbound freight charges linked to this PO by a recorded reference) times "
                    "this line's goods value" + ("; whole purchase order" if len(lines) == 1
                                                 else "; pro rata by goods value")}}
    elif shipping is None and header_withheld.get("freight"):
        freight = {"unknown": "PO_FREIGHT_ESTIMATE_WITHHELD: the purchase order header's "
                              "shipping estimate carries the "
                              f"{', '.join(sorted(header_withheld['freight']))} marking, which "
                              "this reader does not hold; a reader holding it gets it",
                   "who_can_answer": "FIN"}
    elif shipping is None or not math.isfinite(shipping) or shipping <= 0:
        freight = {"unknown": "PO_FREIGHT_NOT_RECORDED: the purchase order header has no "
                              "shipping cost; the Portal would read that as zero freight",
                   "who_can_answer": FORWARDER}
    elif portal["freightPerUnit"] is None:
        freight = {"unknown": f"PORTAL_FREIGHT_UNALLOCATED: {portal['reason']} (the Portal "
                              "allocates header freight by received line value)",
                   "who_can_answer": "WH"}
    elif not fully_received(target):
        freight = {"unknown": "PO_LINE_PARTLY_RECEIVED: the Portal's freight is per received "
                              "unit and this line is not fully received",
                   "who_can_answer": "WH"}
    elif not all(fully_received(x) for x in lines):
        freight = {"unknown": "PO_SIBLING_LINES_NOT_RECEIVED: other lines of this purchase order "
                              "are not fully received, so the Portal's received-value allocation "
                              "would put their share of the header freight on this line",
                   "who_can_answer": "WH"}
    else:
        freight = {"derived": {"value": _dec_str(portal_decimal(portal["freightPerUnit"])),
                               "per_quantity_unit": True, "from": sources,
                               "rule": PORTAL_SOURCES["po_freight_allocation"]
                               + " allocatePoFreightPerUnit over a fully received PO; "
                               + ("the receipt's actual freight, posted over the PO estimate ("
                                  + PORTAL_SOURCES["landed_gl"] + " resolvePostedLandedAmounts)"
                                  if freight_actual else
                                  "the header shipping cost is an estimate (ERP-D-015)")}}
    rate = None
    if tariff and tariff.get("_source") and tariff.get("hs_code") and \
            tariff.get("duty_rate") is not None:
        try:
            rate = Decimal(str(tariff["duty_rate"]))
        except ArithmeticError:
            rate = None
    duty_input: dict[str, Any]
    if duty_actual and len(lines) != 1:
        duty_input = {"unknown": "DUTY_ALLOCATION_RULE_ABSENT: the receipt records the actual "
                                 "duty of the whole purchase order and the Portal has no per-line "
                                 "duty allocation for a multi-line purchase order",
                      "who_can_answer": "FIN"}
    elif duty_actual:
        duty_input = {"derived": {"value": _dec_str(portal_decimal(posted["duty"])),
                                  "per_quantity_unit": False, "from": [receipt["_source"]],
                                  "rule": "single-line purchase order: the receipt's actual "
                                          "duty, posted over the PO estimate ("
                                          + PORTAL_SOURCES["landed_gl"]
                                          + " resolvePostedLandedAmounts)"}}
    elif actual_duty and _actual_line_duty(actual_duty, target) is not None:
        duty_input = {"derived": {
            "value": _dec_str(_actual_line_duty(actual_duty, target)),
            "per_quantity_unit": False,
            "from": [actual_duty["_source"], target.get("_source")],
            "basis": "actual_recorded",
            "rule": f"actual recorded duty: PO effective rate {actual_duty['rate']} (broker/duty "
                    "charges linked to this PO by a recorded reference) times this line's goods "
                    "value" + ("; an upper bound, the G/L account also holds the broker's fee"
                               if actual_duty.get("includes_brokerage") else "")
                    + ("; whole purchase order" if len(lines) == 1
                       else "; pro rata by goods value, no per-line duty is recorded")}}
    elif rate is not None and rate.is_finite() and rate == 0:
        duty_input = {"derived": {"value": "0", "per_quantity_unit": False,
                                  "from": [tariff["_source"]],
                                  "rule": f"cited duty rate 0 (HS {tariff['hs_code']}): the duty "
                                          "is 0, whatever the header estimate says"}}
    elif rate is None or not rate.is_finite():
        duty_input = {"unknown": "TARIFF_TREATMENT_UNVERIFIED: no cited HS classification and "
                                 "duty rate for this item"
                                 + (f" (the header duty {_dec_str(portal_decimal(duty))} is an "
                                    "estimate, not a classification)"
                                    if duty is not None and math.isfinite(duty) and duty > 0
                                    else "; the header records no duty"),
                      "who_can_answer": "FIN"}
    elif duty is None and header_withheld.get("duty"):
        duty_input = {"unknown": "PO_DUTY_ESTIMATE_WITHHELD: the purchase order header's duty "
                                 "estimate carries the "
                                 f"{', '.join(sorted(header_withheld['duty']))} marking, which "
                                 "this reader does not hold; a reader holding it gets it",
                      "who_can_answer": "FIN"}
    elif duty is None or not math.isfinite(duty) or duty <= 0:
        duty_input = {"unknown": "PO_DUTY_NOT_RECORDED: a classification is cited but the "
                                 "purchase order header records no duty amount",
                      "who_can_answer": "FIN"}
    elif len(lines) != 1:
        duty_input = {"unknown": "DUTY_ALLOCATION_RULE_ABSENT: the Portal has no per-line duty "
                                 "allocation for a multi-line purchase order",
                      "who_can_answer": "FIN"}
    else:
        duty_input = {"derived": {"value": _dec_str(portal_decimal(duty)),
                                  "per_quantity_unit": False,
                                  "from": [po.get("_source"), tariff["_source"]],
                                  "rule": f"single-line purchase order: header duty estimate "
                                          f"(ERP-D-015) with cited HS {tariff['hs_code']} rate "
                                          f"{rate}"}}
    provisional: list[dict[str, Any]] = list((tariff or {}).get("provisional_rules") or [])
    seller_bears = {k for k, v in ((incoterm or {}).get("buyer_bears") or {}).items() if not v}
    if incoterm and incoterm.get("bearing"):
        provisional.append({"id": f"incoterm {incoterm['incoterm']}",
                            "status": incoterm["status"]})
    recorded = {"freight": bool(freight_actual) or "basis" in freight.get("derived", {}),
                "duty": bool(duty_actual) or "basis" in duty_input.get("derived", {})}
    for name, slot in (("freight", freight), ("duty", duty_input)):
        if name in seller_bears and recorded[name]:
            provisional.append({"id": f"incoterm {incoterm['incoterm']} not applied to {name}",
                                "status": "a recorded actual wins over the provisional rule"})
        elif name in seller_bears:
            slot.clear()
            slot["derived"] = {"value": "0", "per_quantity_unit": False,
                               "from": [incoterm["source"]],
                               "rule": f"{incoterm['incoterm']} ({incoterm['set_by']}): the "
                                       f"seller bears {name}, so it adds nothing to the "
                                       f"buyer's landed cost; provisional rule, "
                                       f"{incoterm['status']}; nothing is recorded for it"}
    # A read receipt that records no actual is moot once freight and duty both rest on
    # recorded charges (what was paid); an unread receipt is not: it might record an actual,
    # which comes first.
    paid_both = receipts_read > 0 and not receipt_withheld and all(
        "basis" in slot.get("derived", {}) for slot in (freight, duty_input))
    unread = None if (receipt is not None and not receipt_withheld) or paid_both else {
        "unknown": f"RECEIPT_ACTUAL_WITHHELD: the receipt's freight and duty actuals carry the "
                   f"{', '.join(sorted(receipt_withheld))} marking, which this reader does not "
                   "hold, so whether a receipt records one is not shown; a reader holding it "
                   "gets them",
        "who_can_answer": "FIN"} if receipt_withheld else {
        "unknown": f"RECEIPT_ACTUAL_NOT_RECORDED: {receipts_read} receipt(s) of this purchase "
                   "order are read and none records a freight or duty actual; the Portal "
                   "posts one over the PO estimates only when a receipt records it "
                   "(resolvePostedLandedAmounts)",
        "who_can_answer": "WH"} if receipts_read > 0 else {
        "unknown": "RECEIPT_ACTUAL_NOT_READ: the Portal posts receipt freight and duty "
                   "actuals over the PO estimates (resolvePostedLandedAmounts); no "
                   "verified receipt of this purchase order is read",
        "who_can_answer": "WH"}
    return {"portal_allocation": portal, "freight": freight, "duty": duty_input,
            "receipt_actual": unread,
            "posted_if_received": posted, "incoterm_bearing": incoterm,
            "provisional_rules": provisional,
            "source": PORTAL_SOURCES["po_freight_allocation"]}


# ---- forwarder quote text (the typed forwarder_quote fields of a plan, LOG-10) --------------
#
# A projected ForwarderQuote carries its scope, basis, inclusions and exclusions as untrusted
# machine-read text (ontology/schema/document_fields.json). These read them into the
# vocabulary ``assess_freight_quote`` takes; a term not listed here never widens a quote's scope
# (an unread inclusion is ignored, an unread exclusion leaves the scope unknown).

# Who can answer, as Portal Department codes (operating/answerers.py; seed-rules.ts
# ROUTING_DEPARTMENTS): Procurement asks the freight forwarder, Finance the customs broker,
# the Warehouse receives.
FORWARDER = "PROC"
_O, _M, _D = "origin_charges", "main_carriage", "destination_charges"
FREIGHT_SCOPE_TERMS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("origin", "origin charge", "origin charges", "origin thc", "origin handling",
                     "pickup", "pick up", "export clearance", "export customs",
                     "export customs clearance", "export declaration", "exw charges"), (_O,)),
    **dict.fromkeys(("main carriage", "freight", "ocean freight", "sea freight", "air freight",
                     "airfreight", "ocean", "sea", "air", "international freight",
                     "basic ocean freight", "fcl freight", "lcl freight"), (_M,)),
    **dict.fromkeys(("destination", "destination charge", "destination charges",
                     "destination thc", "destination handling", "delivery", "door delivery",
                     "final delivery", "drayage", "trucking"), (_D,)),
    **dict.fromkeys(("customs", "customs brokerage", "brokerage", "customs clearance",
                     "import clearance", "import customs clearance", "isf", "ams"),
                    ("customs_brokerage",)),
    **dict.fromkeys(("duty", "duties", "tax", "taxes", "vat", "gst", "import duty",
                     "import duties", "tariff", "tariffs", "section 301", "mpf", "hmf"),
                    ("duties_and_taxes",)),
    **dict.fromkeys(("surcharge", "surcharges", "contingency", "contingencies",
                     "listed surcharges", "peak season surcharge", "pss", "baf", "bunker", "gri"),
                    ("surcharges_contingencies",)),
    "door to door": (_O, _M, _D), "port to port": (_M,),
    # a name, not a scope: "all-in" adds no component (assess_freight_quote)
    "all in": (), "all inclusive": (),
}
_SCOPE_SPLIT = re.compile(r"[,;/+&()\n]|\band\b|\bplus\b")
_CONTAINER = re.compile(r"\b(20|40|45)\s*(?:'|’|ft\b|foot\b|feet\b)?\s*"
                        r"(gp|dv|dc|st|std|hq|hc|high\s*cube)?\b")


def freight_scope_terms(text: str | None) -> tuple[set[str], int]:
    """(the freight components one cited scope line names, how many of its terms are not
    in ``FREIGHT_SCOPE_TERMS``)."""
    found: set[str] = set()
    unread = 0
    for part in _SCOPE_SPLIT.split((text or "").lower()):
        term = " ".join(re.sub(r"[^a-z0-9 ]", " ", part.replace("-", " ")).split())
        if not term:
            continue
        if term in FREIGHT_SCOPE_TERMS:
            found.update(FREIGHT_SCOPE_TERMS[term])
        else:
            unread += 1
    return found, unread


def _basis_text(text: str | None) -> str:
    return " ".join((text or "").lower().replace("-", " ").split())


def freight_basis(text: str | None) -> str | None:
    """The basis a cited rate line prices: ``shipment``, a container code (``20GP``,
    ``40GP``, ``40HQ``, ``45HQ``), ``CBM``, ``KG`` or ``PALLET``; None when none or several
    are read."""
    low = _basis_text(text)
    found = set()
    if re.search(r"\b(?:shipment|lump sum|consignment)\b", low):
        found.add("shipment")
    for size, kind in _CONTAINER.findall(low):
        if kind or re.search(size + r"\s*(?:'|’|ft|foot|feet)", low):
            found.add(size + ("HQ" if kind.startswith("h") else "GP"))
    if re.search(r"\b(?:cbm|m3)\b", low):
        found.add("CBM")
    if re.search(r"\b(?:kg|kgs|chargeable weight)\b", low):
        found.add("KG")
    if re.search(r"\b(?:pallets?|plts?)\b", low):
        found.add("PALLET")
    return found.pop() if len(found) == 1 else None


# A basis line that is itself a count of its unit ("1 x 40HQ", "40HQ x 2", "12.5 CBM",
# "4 pallets"); anything more on the line (a minimum, a second figure) is not read as a count.
_NUMBER = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_TIMES = r"\s*(?:x|\u00d7|\*)\s*"
_UNIT_WORDS = {"CBM": r"(?:cbm|m3)", "KG": r"kgs?", "PALLET": r"(?:pallets?|plts?)"}
_CONTAINER_WORD = (r"(?:20|40|45)\s*(?:'|\u2019|ft|foot|feet)?\s*"
                   r"(?:gp|dv|dc|st|std|hq|hc|high\s*cube)?(?:\s*containers?)?")


def basis_text_count(text: str | None, basis: str | None) -> Decimal | None:
    """The count of ``basis`` units a basis line states as its whole text (``N x <unit>``,
    ``[per] <unit> x N``, or ``N <unit>`` for a unit other than a container), else None."""
    if basis is None or basis == "shipment":
        return None
    container = basis[:2] in ("20", "40", "45")
    word = _CONTAINER_WORD if container else _UNIT_WORDS[basis]
    forms = [_NUMBER + _TIMES + word, r"(?:per\s+)?" + word + _TIMES + _NUMBER]
    if not container:
        forms.append(_NUMBER + r"\s*" + word)
    low = " ".join((text or "").lower().split())   # a hyphen stays: "-2 x 40HQ" is no count
    for form in forms:
        m = re.fullmatch(form, low)
        if m:
            return Decimal(m.group(1).replace(",", ""))
    return None


_CONTAINER_CODE = re.compile(r"\b(?:20|40|45)\s*(?:(?:'|\u2019|ft|foot|feet)\s*(?:gp|dv|dc|st|std|hq|"
                             r"hc|high\s*cube)?|(?:gp|dv|dc|st|std|hq|hc|high\s*cube))(?![a-z0-9])")
_LIMIT_TERMS = re.compile(r"\b(?:min|mins|minimum|minima|max|maximum|at least|up to|or more|"
                          r"less than|over|above|below)\b")


def basis_terms_unread(text: str | None, basis: str | None) -> bool:
    """A per-unit basis line that states more than its unit and an optional count of it: a
    minimum or maximum clause, or a second figure ("per kg, min 100 kg", "USD 280 per CBM").
    Such a term is never applied, so no rate times count of that line is total freight."""
    if basis is None or basis == "shipment" or basis_text_count(text, basis) is not None:
        return False
    low = _basis_text(text)
    if _LIMIT_TERMS.search(low):
        return True
    rest = _CONTAINER_CODE.sub(" ", low) if basis[:2] in ("20", "40", "45") else low
    return bool(re.search(r"\d", re.sub(r"\bm3\b", " ", rest)))


_COMPONENT_NAMES = {"origin_charges": "origin charges", "main_carriage": "main carriage",
                    "destination_charges": "destination charges",
                    "customs_brokerage": "customs brokerage",
                    "duties_and_taxes": "duties and taxes",
                    "surcharges_contingencies": "surcharges and contingencies"}


def _components(names: Sequence[str] | set[str]) -> str:
    return ", ".join(_COMPONENT_NAMES[c] for c in FREIGHT_COMPONENTS if c in names)


# The gap a quote's typed unknown is reported with: (state, who can answer, detail template).
# Templates name components, codes and parsed dates only: never the quote's own text.
_QUOTE_GAPS: dict[str, tuple[str, str, str]] = {
    "FREIGHT_QUOTE_AMOUNT_UNREAD": ("unknown", FORWARDER, "no total or rate with an ISO 4217 "
                                    "currency is read from the quote for this reader"),
    "FREIGHT_BASIS_UNREAD": ("unknown", FORWARDER, "the quote states a rate but no rate basis the "
                             "Workbench reads (per shipment, container type, CBM, kg or pallet), "
                             "so the rate is not a total"),
    "BASIS_NOT_TOTAL": ("unknown", FORWARDER, "the quote states an amount under a total label "
                        "but prices {priced}; with no cited count of one whole container or "
                        "pallet, and no cited rate times count equal to it, it is not the total "
                        "freight"),
    "FREIGHT_RATE_COUNT_CONFLICT": ("unknown", FORWARDER, "the quote's cited rate times its cited "
                                    "count of {basis} is not its stated amount (see the "
                                    "conflict); neither is chosen, so it is not the total "
                                    "freight"),
    "FREIGHT_BASIS_COUNTS_CONFLICT": ("unknown", FORWARDER, "the quote cites {counts} different "
                                      "counts of {basis} (see the conflict); none is chosen"),
    "FREIGHT_BASIS_TERMS_UNREAD": ("unknown", FORWARDER, "the quote's basis line ({entry}) states "
                                   "a minimum, a maximum or a second figure besides its {basis} "
                                   "unit, which the Workbench does not apply, so neither its "
                                   "stated amount nor a rate times a count is the total freight"),
    "FREIGHT_EXCLUSIONS_UNREAD": ("unknown", FORWARDER, "{unread} exclusion term(s) of the quote "
                                  "name no known freight component, so its scope is not known"),
    "FREIGHT_SCOPE_CONTRADICTS": ("unknown", FORWARDER, "the quote's inclusions and exclusions "
                                  "name the same freight component"),
    "FREIGHT_SCOPE_PARTIAL": ("unknown", FORWARDER, "the quote's cited scope covers {covered}; "
                              "it does not cover {missing}, so it is not the total freight"),
    "FREIGHT_LABEL_CONTRADICTS_SCOPE": ("unknown", FORWARDER, "the quote calls itself all-in but "
                                        "its cited scope leaves out {left}; the cited scope wins"),
    "FREIGHT_EXCLUDES_SURCHARGES": ("unknown", FORWARDER, "the quote excludes surcharges and "
                                    "contingencies, so the freight charged can exceed it"),
    "FREIGHT_QUOTE_VALIDITY_UNKNOWN": ("unknown", FORWARDER, "no validity date is read from the "
                                       "quote"),
    "FREIGHT_QUOTE_EXPIRED": ("stale", FORWARDER, "valid until {valid_until}, before {as_of}: a "
                              "re-quote is needed"),
    "FREIGHT_BASIS_COUNT_UNKNOWN": ("unknown", FORWARDER, "the rate is per {basis}; how many "
                                    "are needed is not cited on the quote in that unit"),
    "PACK_SPEC_NOT_SERVED": ("not_ingested", "Portal team (Workbench reporting API)",
                             "the {basis} count depends on case dimensions and gross weight, "
                             "which the Portal keeps in app_product_pack_spec; the reporting API "
                             "does not serve it. Upstream ask: serve public.app_product_pack_spec "
                             "(case length, width, height and gross weight)"),
}


# ---- plx.investigate engine: ``operating.landed_cost_plan`` ----------------------
# Its names are looked up there on first use: the plan engine imports this module, so a
# module-level import back would be a cycle.
_PLAN_NAMES = frozenset({
    "ASSUMPTION_KINDS",
    "BC_GROSS_PRICE",
    "BC_NET_ROLES",
    "CONFIRMATION_ROLES",
    "CONTAINER_UNITS",
    "DUTY_RULE_PENDING",
    "ESTIMATE_BASIS",
    "ESTIMATE_LABEL",
    "ESTIMATE_QUOTE_TOLERANCE",
    "FM_AGREEMENT_TOLERANCE",
    "FREIGHT_LABELS",
    "HISTORY_LEVEL_TEXT",
    "HISTORY_METHOD_TEXT",
    "IN_QUOTE",
    "MAX_STEP_CHARS",
    "PART_BASES",
    "PLAN_ENGINES",
    "PLAN_ROLES",
    "QUOTE_ROLES",
    "REAL_INPUT_ROLES",
    "RECEIVED_ESTIMATE_LABEL",
    "SUPPLIER_LINK_ARGS",
    "SUPPLIER_ROW_ROLES",
    "TIER_LINK_ARG",
    "TIER_ROLES",
    "_ACTUALS_SUBJECT",
    "_ASSUMPTION_KEYS",
    "_ASSUMPTION_TEXT",
    "_BASIS_COUNT_UNITS",
    "_COST_ROLES",
    "_COUNT_ROLES",
    "_GAP_CODE",
    "_GAP_STATES",
    "_HISTORY_KEYS",
    "_HISTORY_PERIOD",
    "_LINE_ROLES",
    "_OPTIONAL_ROLES",
    "_QUOTE_COUNT_CODES",
    "_QUOTE_COUNT_SEARCHED",
    "_QUOTE_REQUEST_CODES",
    "_QUOTE_SEARCHED",
    "_TARIFF_PERCENT",
    "_TARIFF_UNITS",
    "_UPSTREAM_ASKS",
    "_WHOLE_BASES",
    "_actuals_note",
    "_assumption_text",
    "_blank",
    "_chosen_tier",
    "_closed",
    "_code_of",
    "_corrected_input",
    "_entry_decimal",
    "_entry_text",
    "_equal_at",
    "_evidence_edge",
    "_fm_agreement",
    "_fm_tiers",
    "_fm_unit",
    "_history_defaults",
    "_history_estimate",
    "_history_label",
    "_history_sample_gap",
    "_history_too_small",
    "_iso_date",
    "_part_steps",
    "_pre_receipt_estimate",
    "_quote_counts",
    "_quote_record",
    "_rate_setting",
    "_recorded_actuals",
    "_row_inputs",
    "_supplier_rows",
    "correction_sources",
    "draft_request",
    "duty_assumption",
    "person_correction",
    "plan_check",
    "plan_run",
    "prorate",
    "rate_setting_tariff",
    "supplier_tariff_rate",
})


def __getattr__(name: str) -> Any:
    if name in _PLAN_NAMES:
        from operating import landed_cost_plan
        return getattr(landed_cost_plan, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
