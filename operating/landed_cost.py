"""Deterministic landed-cost contract (overseas packaging case).

per-unit landed cost = (goods + freight + duty/tariff + other) / quantity, in a
target currency. Decimal only (the Portal engine port below computes in doubles exactly as
the Portal does and hands out exact Decimals); units go through the case unit algebra
(``operating.cases.evaluate``) so ``USD/EA * EA = USD`` is checked, not assumed.

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

Cost treatment (Portal staging ``5724575``; docs/reference/portal-code-map.md sections 4.1-4.3, 9):
PLX's current Portal decision is to expense freight and duty to Sage G/L (ERP-D-015, Approved
2026-09-10, ``docs/projects/business-central-erp-modernization/decision-log.md``;
``portal/src/lib/procurement/landed-gl.ts``); Vince's step 18 asks to reopen it. So every
record here carries ``cost_treatment``: an analytic estimate, never item cost.

The Portal's landed-cost / quote engine is ported below ("Portal engine port"): the one
engine every Workbench landed-cost and quote-cost view uses. ``portal_quote_landed`` reproduces
``quoteProjectLanded`` from supplied facts; ``quote_landed`` adds the Workbench layer (typed
unknowns, units, currency, citations); ``assess_freight_quote`` and ``po_line_landed`` turn
forwarder quotes and purchase-order freight/duty into cited inputs (or typed unknowns) for
``compute_landed_cost``. Golden tests run the real Portal TypeScript (Node, no network, a fake
read-only database) and compare every number bit for bit.

``plan_run`` is the engine of the ``plx.investigate`` plan ``ontology/plans/landed_cost.json``
(evidence kind ``compute``): the same functions over a purchase order's cited projected values,
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
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Mapping, Sequence

from ontology.core import ValidationError
from ontology.units import ISO_4217, currency_code, js_trim, unit_token  # one list, there
from operating.answerers import UNDECLARED
from operating.source_units import SourceUnitContracts
from operating.cases import (
    ExpressionError, UnitMismatch, Unit, _canon, _cited_float, _dec_str, _digest,
    _field, _sha, _to_decimal, bind_cited_unit, evaluate, format_unit, parse_unit)

CONTRACT = "plx.landed_cost.v1"
# Current Portal decision (ERP-D-015): freight and duty are expensed, so a landed cost computed
# here is an analytic estimate and never the item's cost.
COST_TREATMENT = {
    "kind": "analytic_estimate_not_item_cost",
    "current_portal_decision": "expense (ERP-D-015, Approved 2026-09-10): freight and duty post "
                               "to Sage G/L (packaging freight 41130, packaging duty 41131)",
    "open_question": "Vince's step 18 asks to reopen ERP-D-015 (capitalise freight and duty "
                     "into item cost via BC Item Charges); until then this is not item cost",
    "source": "docs/projects/business-central-erp-modernization/decision-log.md@5724575#L25; "
              "portal/src/lib/procurement/landed-gl.ts@5724575#L1-L104",
}
MAX_FX_AGE_DAYS = 7  # a rate older than this before as_of is stale (accept_stale to use it)
CATEGORIES = ("goods", "freight", "duty", "other")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_BUILTIN: Any = object()
Resolver = Callable[[str, Any], Mapping[str, Any]]


class LandedCostError(ValidationError):
    pass


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


def _calc(expr: str, values: Mapping[str, tuple[Decimal, Unit]], steps: list[str],
          label: str) -> tuple[Decimal, Unit]:
    result, unit, inexact, _ = evaluate(expr, values)
    bound = ", ".join(f"{k}={_dec_str(v)} {format_unit(u)}" for k, (v, u) in sorted(values.items()))
    steps.append(f"{label}: {expr} [{bound}] = {_dec_str(result)} {format_unit(unit)}"
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
            steps.append(f"{name}: {_dec_str(num)} {cur} (total)")
        elif unit == parse_unit(f"{cur}/{qname}"):
            amount, amount_unit = _calc(f"{name} * qty", {name: (num, unit), "qty": (qnum, qunit)},
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
            amount, amount_unit = _calc(f"{name}_{cur} * fx_{cur}_{target_currency}",
                                        {f"{name}_{cur}": (amount, amount_unit),
                                         f"fx_{cur}_{target_currency}": (rate["value"], rate["unit"])},
                                        steps, f"{name} in {target_currency}")
        known.append((name, amount))

    result: dict[str, Any] = {"contract": CONTRACT, "target_currency": target_currency,
                              "as_of": as_of_date.isoformat(),
                              "quantity": {**qrec, "unit": format_unit(qunit)}}
    tunit = parse_unit(target_currency)
    sub_unit = tunit
    known_subtotal = None
    if known:
        values = {n: (a, tunit) for n, a in known}
        total, _ = _calc(" + ".join(n for n, _ in known), values, steps,
                         "known subtotal" if unknowns else "total")
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
# The Portal's landed-cost / quote engine (ERP-D-015 context), ported line by line from
# petralabx/plx-customer-portal staging @ 45673ee and re-pinned at d4d1bb2 (see below). Since the bc6e8de reading (TASK-2486) only
# 77f1ee6 (TASK-2465, G9) moved the cited files: project-landed-quote.ts, raw-cost.ts,
# formulation-resolution.ts and ready-to-quote.ts price what the customer supplies at $0
# through one G3 supply-source read (lib/customer-materials/supply-source*.ts, ported as
# ``resolve_supply_sources``); a failed read stops the quote, never PLX pricing. Since 514e1e5
# (TASK-2611, G-D4) the matrix unit-cost fallback skips a column with customer-supplied
# material (``fallback_matrix_unit_cost``); 65a0e8b only added an audit constant to
# supply-source-shared.ts. Re-read at 4243499 (2026-10-14): 4dc5cdf (TASK-2809) moved the
# engine's reads into quote-inputs.ts ``loadQuoteInputs`` (one setup-standards read,
# ``loadSetupStandardRows``; a failed read still resolves to the workbook seed and is now
# also named in ``readErrors``) and its pricing, unchanged, into ``priceQuoteInputs``;
# 4f8c905 (TASK-2810) added a dry-run cost preview, a POST the Workbench never calls (GET
# only); 66cd56c (TASK-1509) added ``pickRunVolumeTierForQtyExplicit`` for the L9 run-size
# view and left ``pickRunVolumeTierForQty`` as is. Overhead stays RateSettings $/hour
# (``overheadBasis.source = "rate_settings"``): ERP-D-022's line-hour rate is Proposed, not
# code. Both harnesses (operating/tests/portal_engine) re-run on 4243499 print every golden
# value unchanged (only the read list of one scenario lost a raw query: b59b007, TASK-2649,
# projects/read.ts ``loadCustomerLite`` now asks customers/portal-label.ts ``loadCustomerLabel``,
# portal customer records first, a display label that prices nothing), so nothing ported
# moved; the line citations below stay at 45673ee. Re-read at 1f2d42d (2026-10-14), only
# writes changed: 9108064 (TASK-2850, CT-B) makes quote-inputs.ts ``loadPrimaryRoutingSafe``
# always read with ``persist: false`` and the recalc script skip frozen FGs on --apply;
# 255ec2a (TASK-2863, RV2a) makes fg-routing-store.ts fill blank setup minutes in memory on
# every read (``withSetupFilled``, the old ``persist: false`` branch, which ``primary_routing``
# already ports); b236bef (TASK-2859, CT-H) stops draft-cost-persist.ts ``persistRow`` writing
# ``overheadPerUnit`` and non-compounding ``yieldUnits``, while ``loadDraftCostStore`` still
# reads the stored values and the engine still prices from an assembly row's stored
# ``yieldUnits`` (``resolveExpectedUnitsOverride``, project-landed-quote.ts@1f2d42d:401,2092;
# here ``_DRAFT_FIELDS``, ``resolve_expected_units_override``). Such a value can predate
# b236bef and no longer changes from the cost sheet. No ported arithmetic changed.
# Re-read at 40237ed (2026-10-15): 0ae127a (TASK-2851, RV2b) adds draft-cost-persist.ts
# ``loadCostSheetFreeze`` and makes ``assertCostSheetWritable`` refuse a frozen FG
# (``findFrozenFgItem``) with 409 ``FG_FROZEN`` before its other checks, so a frozen FG's tier
# rows no longer change from the cost sheet or the scratch cost write; the reads
# (``toDraftRow``, ``loadDraftCostStore``) are unchanged, and both harnesses print the same
# output with that file at 1f2d42d or at 40237ed.
# Re-read at d4d1bb2 (2026-10-15); the citations below and both
# goldens now pin d4d1bb2. 35cfb18 (TASK-2811) changed the one ported behaviour: the census
# labour detail of an unrouted quote now says it is priced at $0 and reported, not "the 1-hour
# placeholder" (``derive_ready_to_quote``); the arithmetic was already $0 in both. 0f9fbee
# (TASK-2948) has quote-inputs.ts read the setup standards once and hand the rows to the routing
# fill (``getPrimaryRouting(..., {persist: false, setupStandardRows})``): the same in-memory
# fill, a failed read still the workbook seed, as ``primary_routing`` does. eddbfe3 (TASK-2914)
# stops a routing *save* filling blank setup minutes and lets a manual save keep its own
# throughput (fg-routing.ts ``deriveFgRouting``), and 0a62186 (TASK-2864, RV2a) makes a primary
# routing save also write a one-band route plan and refuse a frozen FG with 409 ``FG_FROZEN``:
# writes the Workbench never makes; no read changed. 371b4cc (TASK-2865) appends rate-settings
# audit redaction to rate-masters.ts (``RATE_MASTER_DEFAULTS`` and ``overheadRateFromSettings``
# unchanged); the Workbench serves no app_audit_log. d4d1bb2 (TASK-2862, CT-C) adds a
# report-only cost fingerprint to the snapshot row that ``landedQuoteOf`` strips. ERP-D-022:
# b8f6e22 (TASK-2853) adds the line-hour overhead store ``LineOverheadRate`` and the flag
# ``User.canManageOverheadRates``, and 371b4cc its store and sizing code, but the engine does
# not read the store yet (overhead-rate-store.ts: "Not read by the quote engine yet (CT-G)";
# quote-inputs.ts ``overheadBasis.source`` is still "rate_settings"), so neither is ported.
# Both harnesses print every other golden value unchanged at d4d1bb2.
# ``readErrors`` is not a separate output here: a relation whose read failed arrives as None,
# is priced with the Portal fallback and is named as a RECORDS_NOT_READ unknown
# (``QUOTE_RELATIONS``), so the quote is never shown as verified while one is open.
# Re-read at d53137e (2026-10-16): no file cited here changed.
# 335dc8a (TASK-2878, CT-E2) has recalc-theoretical-fg.ts ``--apply`` also re-stamp a draft
# whose costs did not move but whose fingerprint is missing or stale, and 8c68104 (TASK-2918)
# adds the report-only TheoreticalFG freshness columns that Recalc and Activate write: writes
# the Workbench never makes, from the fingerprint ``landedQuoteOf`` strips.
#
# Arithmetic is IEEE double precision exactly as the Portal computes it (Python floats are
# the same doubles; Math.round is reproduced with ties toward +infinity), because the goal is to
# reproduce Portal numbers bit for bit. Every number leaves through ``portal_decimal`` (the
# shortest round-trip decimal, the digits JavaScript prints) with its unit and currency stated.
# The Portal substitutes 0 or a default for missing inputs (missing hours, no supplier card,
# no fill weight, no freight); the Workbench layer (``quote_landed``) lists each substitution as
# a typed unknown and states no Workbench cost while one is open: the Portal figure is shown as
# "what the Portal engine computes from these facts", never as a verified landed cost.
#
# The engine reads records, not resolved values: the formula pin and FG pin resolution, the
# ASSEMBLY BOM selection, the primary routing (setup minutes filled from the setup standards),
# the draft tier rows and the labour hours (LOG-08) are ported. Inputs, not derived: each asked
# volume's vessel charge count (the compounding standard and vessel derivation), the quote run
# quantity, and the census / pack spec, which have no read-only export upstream (the matrix and
# workbook GETs write a theoretical-FG snapshot, so they are never read sources). The Q2-C volume
# advisories are not ported. The WMS pallet builder and skid math are ported in
# operating/pallet_build.py (domestic parcel/LTL handling units and freight class; they price no
# ocean or landed freight).

PORTAL_REVISION = "d4d1bb247708052ddacf78ed4f47bcba0a27c375"
PORTAL_SHORT = "d4d1bb2"
_L = "portal/src/lib/"


def _psrc(path: str, lines: str) -> str:
    return f"{path}@{PORTAL_SHORT}#L{lines}"


_PLQ = _L + "costing/project-landed-quote.ts"
_QIN = _L + "costing/quote-inputs.ts"
PORTAL_SOURCES = {
    "landed_component": _psrc(_L + "costing/landed-cost.ts", "39-71"),
    "fill_mass": _psrc(_L + "costing/landed-cost.ts", "73-81"),
    "rate_defaults": _psrc(_L + "costing/rate-masters.ts", "34-57"),
    "overhead_rate": _psrc(_L + "costing/rate-masters.ts", "71-80"),
    "suggestion_volumes": _psrc(_L + "costing/rate-masters.ts", "133-156"),
    "estimate_unit_cost": _psrc(_L + "costing/cost-estimate.ts", "111-198"),
    "margin_ladder": _psrc(_L + "costing/margin-ladder.ts", "24-182"),
    "uptime_overhead": _psrc(_L + "costing/uptime-overhead.ts", "50-121"),
    "workbook_wedge": _psrc(_L + "costing/setup-standards.ts", "19-23"),
    "run_volume_tier": _psrc(_L + "costing/volume-tiers.ts", "77-86")
    + "; " + _psrc(_L + "costing/template-cost-matrix.ts", "44-49"),
    "parse_money": _psrc(_PLQ, "339-343"),
    "margin_and_overhead": _psrc(_PLQ, "361-424"),
    "gather_quote_facts": _psrc(_PLQ, "651-888"),
    "census_from_facts": _psrc(_PLQ, "899-927"),
    "card_tier": _psrc(_PLQ, "1304-1328"),
    "po_freight_allocation": _psrc(_PLQ, "1340-1366"),
    "line_cost_evidence": _psrc(_PLQ, "1392-1521"),
    "line_provenance": _psrc(_PLQ, "1529-1602"),
    "volume_column": _psrc(_PLQ, "1713-1987") + "; " + _psrc(_PLQ, "1030-1132"),
    "matrix_landed_caller": _psrc(_PLQ, "1881-1884"),
    "engine": _psrc(_PLQ, "2092-2327") + "; " + _psrc(_QIN, "111-164")
    + "; " + _psrc(_PLQ, "2371-2404"),
    "raw_cost": _psrc(_L + "costing/raw-cost.ts", "1-59"),
    "formulation_resolution": _psrc(_L + "costing/formulation-resolution.ts", "1-181")
    + "; " + _psrc(_L + "project-development/overview-shared.ts", "61-92"),
    "bom_selection": _psrc(_PLQ, "527-649"),
    "routing_labour": _psrc(_L + "costing/fg-routing.ts", "89-192"),
    "template_labour": _psrc(_L + "costing/template-cost-matrix.ts", "84-101"),
    "setup_amortisation": _psrc(_L + "costing/setup-standards.ts", "208-271"),
    "setup_standard": _psrc(_L + "costing/setup-standards.ts", "19-169")
    + "; " + _psrc(_L + "costing/setup-standards-store.ts", "98-119")
    + "; " + _psrc(_L + "inventory/line-tank-keys.ts", "34-45"),
    "routing_setup_fill": _psrc(_L + "costing/fg-routing-store.ts", "73-298")
    + "; " + _psrc(_QIN, "90-104"),
    "draft_costs": _psrc(_L + "costing/draft-cost-overrides.ts", "92-117")
    + "; " + _psrc(_L + "costing/draft-cost-overrides.ts", "420-443")
    + "; " + _psrc(_L + "assembly-bom/draft-cost-persist.ts", "113-175")
    + "; " + _psrc(_L + "assembly-bom/draft-cost-persist.ts", "353-374")
    + "; " + _psrc(_L + "costing/volume-tiers.ts", "26-69"),
    "quote_hours": _psrc(_PLQ, "951-1259") + "; " + _psrc(_PLQ, "408-424"),
    "setup_standard_load": _psrc(_QIN, "111-164")
    + "; " + _psrc(_L + "costing/setup-standards-store.ts", "98-111"),
    "bom_total": _psrc(_L + "costing/bom-total.ts", "14-26"),
    "ready_to_quote": _psrc(_L + "costing/ready-to-quote.ts", "64-497"),
    "quote_totals": _psrc(_L + "quote/totals.ts", "8-57"),
    "matrix_fallback": _psrc(_L + "quote/matrix-fallback.ts", "15-95"),
    "matrix_offer_margin": _psrc(_L + "quote/matrix.ts", "149-183"),
    "matrix_route_fallback": _psrc("portal/src/app/api/mrp/quotes/[id]/matrix/route.ts",
                                   "134-162"),
    "offer_writer": _psrc(_L + "quote/service.ts", "389-480"),
    "landed_gl": _psrc(_L + "procurement/landed-gl.ts", "1-104"),
    "customer_supplied": _psrc(_PLQ, "184-267"),
    "supply_source": _psrc(_L + "customer-materials/supply-source-shared.ts", "14-82")
    + "; " + _psrc(_L + "customer-materials/supply-source.ts", "46-233")
    + "; " + _psrc(_L + "customers/anchor.ts", "42-43")
    + "; " + _psrc(_L + "customers/anchor.ts", "99-118")
    + "; " + _psrc(_L + "sourcing/raw-product.ts", "20-48")
    + "; " + _psrc(_L + "sourcing/status.ts", "74-99")
    + "; " + _psrc(_L + "projects/read.ts", "120-152"),
    "erp_d_015": "docs/projects/business-central-erp-modernization/decision-log.md@"
                 + PORTAL_SHORT + "#L25",
}

# portal/src/lib/costing/rate-masters.ts RATE_MASTER_DEFAULTS
RATE_DEFAULTS = {"rawMaterialsAdjustmentPct": 10.0, "batchingRate": 32.0, "assemblyRate": 20.9,
                 "overheadRate": 31.3, "marginPct": 30, "marginTiersPct": (25, 30, 35, 40),
                 "quoteSuggestionVolumes": (5000, 10000, 25000, 50000), "fxRateCadUsd": 1.0,
                 "tariffRate": 0.065, "landedMultiplier": 1.38}
PORTAL_QUOTE_DUTY_RATE = RATE_DEFAULTS["tariffRate"]
PORTAL_QUOTE_MULTIPLIER = RATE_DEFAULTS["landedMultiplier"]
PORTAL_QUOTE_SOURCE = (PORTAL_SOURCES["landed_component"] + " (landedComponentCost); callers "
                       "project-landed-quote.ts:784 and :1700, quote/matrix.ts:111, "
                       "quote/workbook.ts:208, costing/quote-advisories.ts:407 pass only "
                       "tierPrice and freight")
WORKBOOK_UPTIME_HOURS = 6.5  # setup-standards.ts WORKBOOK_WEDGE_HOURS.uptime
# template-cost-matrix.ts RUN_VOLUME_TIERS (code, maxQty), sorted by sortOrder
RUN_VOLUME_TIERS = (("5k", 9999), ("10k", 24999), ("25k", 49999), ("50k", None))
DRIFT_THRESHOLD_PCT = 10  # ready-to-quote.ts DEFAULT_CARD_PO_DRIFT_THRESHOLD_PCT
STOCK_FILL_LABEL = ("Margin if filled from stock — won't-replace-stock exception, display only; "
                    "the priced margin stays canonical")
LANDED_GL = {"rmFreight": "41102", "packFreight": "41130", "rmDuty": "41101",
             "packDuty": "41131", "distDuty": "41680"}
_EPSILON = 2.220446049250313e-16  # Number.EPSILON
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---- JavaScript number semantics -------------------------------------------------------------

def _is_num(value: Any) -> bool:
    """``typeof value === "number"`` (NaN and infinities included, bools excluded)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    """``Number.isFinite(value)``."""
    return _is_num(value) and math.isfinite(value)


def _num(value: Any) -> float:
    """``num()`` of landed-cost.ts / cost-estimate.ts: a finite number above zero, else 0."""
    return float(value) if _finite(value) and value > 0 else 0.0


def _truthy(value: Any) -> bool:
    """JavaScript truthiness for the values a Prisma row holds."""
    if value is None or value is False:
        return False
    if _is_num(value):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


def _nc(*values: Any) -> Any:
    """``a ?? b ?? c``: the first value that is not null/undefined."""
    for value in values:
        if value is not None:
            return value
    return None


def _js_math_round(value: float) -> float:
    """``Math.round``: nearest integer, ties toward +infinity, exactly as in IEEE doubles."""
    if not math.isfinite(value):
        return value
    floor = math.floor(value)
    return float(floor + 1 if value - floor >= 0.5 else floor)


def _round(value: float, scale: float) -> float:
    """``Math.round(n * scale) / scale`` (round2 = 100, round4 = 1e4, round6 = 1e6)."""
    return _js_math_round(value * scale) / scale


def quote_round2(value: float) -> float:
    """quote/totals.ts ``round2``: ``Math.round((n + Number.EPSILON) * 100) / 100``."""
    return _js_math_round((value + _EPSILON) * 100) / 100


def js_number(value: Any) -> float | None:
    """``Number(value)`` for the row values the engine meets; None stands for NaN."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if _is_num(value):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        # StringToNumber: trimmed; "" is 0; decimal literal, "Infinity" with a sign, or an
        # unsigned 0x/0o/0b integer. Anything else ("1_000", "inf", "nan") is NaN.
        text = value.strip()
        if text == "":
            return 0.0
        if text in ("Infinity", "+Infinity", "-Infinity"):
            return -math.inf if text[0] == "-" else math.inf
        radix = {"0x": 16, "0o": 8, "0b": 2}.get(text[:2].lower())
        if radix:
            try:
                return float(int(text[2:], radix)) if "_" not in text and text[2:] else None
            except ValueError:
                return None
        if not _JS_DECIMAL.match(text):
            return None
        return float(text)
    return None


_JS_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _to_num(value: Any) -> float | None:
    """project-landed-quote.ts ``toNum``: ``Number(value)`` when finite, else null."""
    number = js_number(value)
    return number if number is not None and math.isfinite(number) else None


def js_string(value: Any) -> str:
    """JavaScript ``String(number)`` (Number::toString), for Portal text such as census details."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits, exponent = Decimal(repr(abs(value))).normalize().as_tuple()[1:]
    s = "".join(map(str, digits))
    k = len(s)
    n = k + exponent  # decimal point position, as in ECMA-262 Number::toString
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s
    else:
        e = n - 1
        mantissa = s[0] + ("." + s[1:] if k > 1 else "")
        body = f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
    return sign + body


def portal_decimal(value: Any) -> Decimal | None:
    """The exact decimal a Portal (JavaScript) number prints as; None for null/NaN/infinite."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float) and math.isfinite(value):
        return Decimal(repr(value))
    return None


def _iso(value: Any) -> str | None:
    """``toIso``: ``new Date(value).toISOString()`` (a time without an offset is read as UTC,
    the harness runs with TZ=UTC); None when the value is not a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _epoch_ms(iso: str | None) -> int | None:
    """``Date.parse`` of an ISO string, in whole milliseconds."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(milliseconds=1)


# ---- pure Portal functions, one per Portal function ------------------------------------------

def landed_component_cost(tier_price: Any, freight: Any = 0, *, duty: Any = None,
                          duty_rate: Any = None, multiplier: Any = None) -> dict[str, Any]:
    """landed-cost.ts ``landedComponentCost``:
    ``duty = round4(tier × rate)``; ``landed = round4((tier + duty) × multiplier + freight)``.
    Every Portal caller passes only ``tierPrice`` and ``freight``, so rate 0.065 and
    multiplier 1.38 always apply (RateSettings tariffRate/landedMultiplier have no effect)."""
    tier = _num(tier_price)
    rate = float(duty_rate) if _finite(duty_rate) and duty_rate >= 0 else PORTAL_QUOTE_DUTY_RATE
    duty_value = float(duty) if _finite(duty) and duty >= 0 else _round(tier * rate, 1e4)
    mult = float(multiplier) if _finite(multiplier) and multiplier > 0 else PORTAL_QUOTE_MULTIPLIER
    freight_value = _num(freight)
    landed = _round((tier + duty_value) * mult + freight_value, 1e4)
    return {"tierPrice": _round(tier, 1e4), "duty": _round(duty_value, 1e4), "dutyRate": rate,
            "multiplier": mult, "freight": _round(freight_value, 1e4), "landedUnitCost": landed}


def portal_quote_landed_component(tier_price: Any, freight: Any = 0) -> dict[str, Any]:
    """The quote engine's per-component call ``landedComponentCost({tierPrice, freight})``,
    labelled: a Portal quote estimate, never item cost (ERP-D-015). No FX conversion: the tier
    price and freight are used in whatever currency the supplier card holds."""
    return {**landed_component_cost(tier_price, freight), "currency_conversion": None,
            "rate_settings_applied": False,
            "label": "Portal quote landed-cost estimate (mirrored), not item cost",
            "cost_treatment": dict(COST_TREATMENT), "source": PORTAL_QUOTE_SOURCE}


def fill_mass_kg(fill_weight_g: Any, specific_gravity: Any = None) -> float:
    """landed-cost.ts ``fillMassKg``: (g / 1000) × SG, SG defaulting to 1."""
    grams = _num(fill_weight_g)
    sg = float(specific_gravity) if _finite(specific_gravity) and specific_gravity > 0 else 1.0
    return (grams / 1000) * sg


def estimate_unit_cost(inp: Mapping[str, Any]) -> dict[str, Any]:
    """cost-estimate.ts ``estimateUnitCost`` (same operation order, same rounding)."""
    fill_kg = _num(inp.get("fillWeightG")) / 1000
    adj = _num(inp.get("rawMaterialsAdjustmentPct"))
    raw_per_kg = 0.0
    for row in inp.get("rawMaterials") or []:
        raw_per_kg = raw_per_kg + (_num(row.get("inclusionPct")) / 100) * _num(row.get("costPerKg"))
    bulk_per_kg = raw_per_kg * (1 + adj / 100)
    bulk = bulk_per_kg * fill_kg
    packaging = 0.0
    for row in inp.get("packaging") or []:
        packaging = packaging + _num(row.get("qtyPerUnit")) * _num(row.get("unitCost"))
    asm_hours = _num(inp.get("assemblyHoursPerUnit"))
    cmp_hours = _num(inp.get("compoundingHoursPerUnit"))
    production_hours = asm_hours + cmp_hours
    asm_labour = asm_hours * _num(inp.get("assemblyRate"))
    cmp_labour = cmp_hours * _num(inp.get("batchingRate"))
    oh_hours = (_num(inp.get("overheadHoursPerUnit"))
                if inp.get("overheadHoursPerUnit") is not None else asm_hours)
    overhead = oh_hours * _num(inp.get("overheadRate"))
    total = bulk + packaging + cmp_labour + asm_labour + overhead
    margin = _num(inp.get("marginPct"))
    sell = total / (1 - margin / 100) if margin < 100 else total
    r2 = lambda v: _round(v, 100.0)  # noqa: E731
    lines = [{"key": "bulk", "label": "Raw materials (bulk)", "amountPerUnit": r2(bulk)},
             {"key": "packaging", "label": "Packaging components", "amountPerUnit": r2(packaging)},
             {"key": "compounding", "label": "Compounding labour", "amountPerUnit": r2(cmp_labour)},
             {"key": "assembly", "label": "Assembly labour", "amountPerUnit": r2(asm_labour)},
             {"key": "overhead", "label": "Overhead", "amountPerUnit": r2(overhead)}]
    return {"bulkCostPerKg": r2(bulk_per_kg), "bulkCostPerUnit": r2(bulk),
            "packagingCostPerUnit": r2(packaging), "compoundingLabourPerUnit": r2(cmp_labour),
            "assemblyLabourPerUnit": r2(asm_labour), "overheadPerUnit": r2(overhead),
            "productionHoursPerUnit": _round(production_hours, 1e6),
            "totalCostPerUnit": r2(total), "marginPct": margin, "sellPricePerUnit": r2(sell),
            "marginPerUnit": r2(sell - total), "lines": lines}


def pick_run_volume_tier(qty: Any) -> str | None:
    """volume-tiers.ts ``pickRunVolumeTierForQty``."""
    if qty is None or not _finite(qty) or qty <= 0:
        return None
    for code, max_qty in RUN_VOLUME_TIERS:
        if qty <= (max_qty if max_qty is not None else math.inf):
            return code
    return RUN_VOLUME_TIERS[-1][0]


def _valid_margin(value: Any) -> bool:
    return _finite(value) and 0 <= value < 100


def margin_to_markup_pct(margin_pct: Any) -> float:
    if not _valid_margin(margin_pct):
        return math.nan
    m = margin_pct / 100
    return (m / (1 - m)) * 100


def price_at_margin_pct(cost: Any, margin_pct: Any) -> float:
    """margin-ladder.ts ``priceAtMarginPct``: cost ÷ (1 − m)."""
    base = float(cost) if _finite(cost) and cost > 0 else 0.0
    if not _valid_margin(margin_pct):
        return base
    return base / (1 - margin_pct / 100)


def _margin_label(margin_pct: float) -> str:
    markup = margin_to_markup_pct(margin_pct)
    shown = js_string(margin_pct if float(margin_pct).is_integer() else _round(margin_pct, 10.0))
    return f"{shown}% margin = {js_string(_round(markup, 10.0))}% markup"


def normalize_margin_tiers(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    ordered = sorted(_round(n, 100.0) for n in value if _valid_margin(n))
    clean = [n for i, n in enumerate(ordered) if i == 0 or abs(n - ordered[i - 1]) > 0.01]
    return clean or None


def resolve_margin_ladder(settings: Mapping[str, Any] | None,
                          quote_override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """margin-ladder.ts ``resolveMarginLadder`` (OD-13)."""
    settings = settings or {}
    override = quote_override or {}
    system = normalize_margin_tiers(settings.get("marginTiersPct"))
    over = normalize_margin_tiers(override.get("tiersPct"))
    tiers = over or system or list(RATE_DEFAULTS["marginTiersPct"])
    tiers_source = "quote_override" if over else ("rate_settings" if system else "fallback")
    default = settings.get("marginPercent")
    default = default if _valid_margin(default) else RATE_DEFAULTS["marginPct"]
    target_override = override.get("targetPct")
    target = target_override if _valid_margin(target_override) else default
    return {"tiersPct": tiers, "tiersSource": tiers_source, "defaultTierPct": default,
            "targetPct": target,
            "targetSource": "quote_override" if _valid_margin(target_override) else "system_default",
            "tiers": [{"marginPct": m, "markupPct": margin_to_markup_pct(m),
                       "label": _margin_label(m), "isDefault": abs(m - default) <= 0.01,
                       "isTarget": abs(m - target) <= 0.01} for m in tiers]}


def price_margin_tiers(cost: Any, ladder: Mapping[str, Any]) -> list[dict[str, Any]]:
    """margin-ladder.ts ``priceMarginTiers``: every tier off one frozen cost."""
    return [{**tier, "sellPricePerUnit": _round(price_at_margin_pct(cost, tier["marginPct"]), 100.0)}
            for tier in ladder["tiers"]]


def overhead_rate_from_settings(rates: Mapping[str, Any] | None) -> float:
    """rate-masters.ts ``overheadRateFromSettings``: the first ``overheadPer*`` number."""
    for key, value in (rates or {}).items():
        if isinstance(key, str) and key.startswith("overheadPer") and _finite(value):
            return value
    return RATE_DEFAULTS["overheadRate"]


def derive_uptime_overhead(hourly_rate: Any, assembly_hours_per_unit: Any,
                           assembly_hours_source: Any = None, expected_units_per_shift: Any = None,
                           uptime_hours: Any = None) -> dict[str, Any]:
    """uptime-overhead.ts ``deriveUptimeOverhead`` (OD-14: assembly-only absorption)."""
    pos = lambda v: v if _finite(v) and v > 0 else None  # noqa: E731
    rate = hourly_rate if _finite(hourly_rate) and hourly_rate >= 0 else 0
    reasons: list[str] = []
    base = {"hourlyRate": rate, "hoursSource": _nc(assembly_hours_source), "appliesTo": "assembly"}
    expected = pos(expected_units_per_shift)
    if expected is not None:
        uptime = pos(uptime_hours)
        if uptime is None:
            uptime = WORKBOOK_UPTIME_HOURS
            reasons.append("uptime-defaulted-workbook-wedge")
        hours = uptime / expected
        return {**base, "uptimeHours": uptime, "expectedUnitsPerShift": expected,
                "hoursPerUnit": hours, "perUnit": rate * hours,
                "source": "expected_units_override", "reasons": reasons}
    hours = pos(assembly_hours_per_unit)
    if hours is not None:
        return {**base, "uptimeHours": None, "expectedUnitsPerShift": None, "hoursPerUnit": hours,
                "perUnit": rate * hours, "source": "assembly_hours", "reasons": reasons}
    reasons.append("overhead-basis-missing:no-assembly-hours")
    return {**base, "uptimeHours": None, "expectedUnitsPerShift": None, "hoursPerUnit": None,
            "perUnit": None, "source": "missing", "reasons": reasons}


def quote_suggestion_volumes(rates: Mapping[str, Any] | None) -> list[float]:
    value = (rates or {}).get("quoteSuggestionVolumes")
    if isinstance(value, (list, tuple)):
        seen: list[float] = []
        for n in value:
            if _finite(n) and n > 0:
                rounded = _js_math_round(float(n))
                if rounded not in seen:
                    seen.append(rounded)
        if seen:
            return sorted(seen)
    return list(RATE_DEFAULTS["quoteSuggestionVolumes"])


def pick_supplier_card_tier_price(card: Mapping[str, Any], component_qty: Any,
                                  run_volume_tier_code: str | None) -> dict[str, Any]:
    """project-landed-quote.ts ``pickSupplierCardTierPrice``: highest minQty ≤ demand."""
    matched = None
    if component_qty is not None and component_qty > 0:
        for tier in card.get("tiers") or []:
            if tier["minQty"] <= component_qty and (matched is None
                                                    or tier["minQty"] > matched["minQty"]):
                matched = tier
    return {"source": "supplier_card_tier",
            "unitCost": matched["unitCost"] if matched else card.get("unitCost"),
            "tierMinQty": matched["minQty"] if matched else None,
            "runVolumeTierCode": run_volume_tier_code, "qtyBasis": component_qty,
            "asOf": _iso(_nc(matched.get("effectiveFrom"), matched.get("updatedAt")))
            if matched else _iso(card.get("updatedAt"))}


def allocate_po_freight_per_unit(shipping_cost: Any, po_lines: Sequence[Mapping[str, Any]],
                                 line_id: Any) -> dict[str, Any]:
    """project-landed-quote.ts ``allocatePoFreightPerUnit``: header shippingCost pro-rata by
    RECEIVED line value, per received unit. Portal reads an absent or zero shippingCost as a
    genuine zero; the Workbench layer (``po_line_landed``) does not."""
    target = next((line for line in po_lines if line.get("id") == line_id), None)
    if target is None:
        return {"freightPerUnit": None, "reason": "po-line-not-found"}
    if not (_is_num(target.get("quantityReceived")) and target["quantityReceived"] > 0):
        return {"freightPerUnit": None, "reason": "po-line-nothing-received"}
    shipping = float(shipping_cost) if _finite(shipping_cost) and shipping_cost > 0 else 0.0
    if shipping == 0:
        return {"freightPerUnit": 0.0, "reason": None}

    def value(line: Mapping[str, Any]) -> float:
        return max(0.0, js_number(line.get("unitPrice")) or 0.0) * \
            max(0.0, js_number(line.get("quantityReceived")) or 0.0)

    total = 0.0
    for line in po_lines:
        total = total + value(line)
    if not total > 0:
        return {"freightPerUnit": None, "reason": "po-freight-unallocatable-zero-line-value"}
    share = shipping * (value(target) / total)
    return {"freightPerUnit": _round(share / target["quantityReceived"], 1e4), "reason": None}


def rollup_bom_total(lines: Sequence[Mapping[str, Any]]) -> float:
    """bom-total.ts ``rollupBomTotal``."""
    total = 0.0
    for line in lines:
        qty = line.get("quantity") if _finite(line.get("quantity")) else 0
        est = line.get("estimatedUnitCost")
        unit = est if est is not None and _finite(est) else (
            line.get("cost") if _finite(line.get("cost")) else 0)
        total = total + qty * unit
    return _round(total, 1e4)


def parse_money(value: Any) -> float | None:
    """project-landed-quote.ts ``parseMoney``: digits and dots only, finite and above zero."""
    if not _truthy(value):
        return None
    number = js_number("".join(ch for ch in js_string(value) if ch.isdigit() or ch == "."))
    return number if number is not None and math.isfinite(number) and number > 0 else None


# ---- ready-to-quote census (ready-to-quote.ts) ------------------------------------------------

def _to_time(iso: Any) -> int | None:
    return _epoch_ms(iso) if isinstance(iso, str) and iso else None


def derive_cost_provenance_advisories(lines: Sequence[Mapping[str, Any]],
                                      drift_threshold_pct: Any = None) -> list[dict[str, Any]]:
    threshold = drift_threshold_pct if drift_threshold_pct is not None and \
        drift_threshold_pct > 0 else DRIFT_THRESHOLD_PCT
    out: list[dict[str, Any]] = []
    for line in lines:
        card, po = line.get("cardUnitCost"), line.get("lastPoUnitPrice")
        if card is not None and po is not None and po > 0 and _finite(card) and _finite(po):
            drift = _js_math_round(((card - po) / po) * 1000) / 10
            if abs(drift) > threshold:
                out.append({"kind": "card_po_drift", "severity": "warning",
                            "lineName": line["lineName"], "cardUnitCost": card,
                            "lastPoUnitPrice": po, "driftPct": drift,
                            "detail": f"{line['lineName']}: supplier card ${js_string(card)} vs "
                                      f"last-PO ${js_string(po)} — "
                                      f"{'+' if drift > 0 else ''}{js_string(drift)}% drift "
                                      f"(threshold {js_string(threshold)}%). Card stays "
                                      "canonical; re-verify the card."})
        card_at, received_at = _to_time(line.get("cardUpdatedAt")), \
            _to_time(line.get("lastPoReceivedAt"))
        if card_at is not None and received_at is not None and card_at < received_at:
            out.append({"kind": "stale_card", "severity": "advisory", "lineName": line["lineName"],
                        "cardUpdatedAt": line.get("cardUpdatedAt"),
                        "lastPoReceivedAt": line.get("lastPoReceivedAt"),
                        "detail": f"{line['lineName']}: supplier card last updated "
                                  f"{line.get('cardUpdatedAt')} — older than the newest PO "
                                  f"receipt {line.get('lastPoReceivedAt')}. Card stays "
                                  "canonical; confirm it still reflects the vendor's price."})
    return out


def derive_margin_if_filled_from_stock(sell: Any, priced_total: Any,
                                       lines: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if sell is None or not sell > 0 or priced_total is None or not _finite(priced_total):
        return None
    delta = 0.0
    from_stock = at_priced = 0
    for line in lines:
        stock_cost, on_hand, required = (line.get("stockUnitCost"),
                                         line.get("stockQuantityOnHand"), line.get("requiredQty"))
        if stock_cost is not None and on_hand is not None and required is not None and \
                required > 0 and on_hand >= required:
            from_stock += 1
            delta = delta + line["qtyPerUnit"] * (stock_cost - line["pricedUnitCost"])
        else:
            at_priced += 1
    if from_stock == 0:
        return None
    adjusted = _round(priced_total + delta, 100.0)
    margin = lambda cost: _js_math_round(((sell - cost) / sell) * 1000) / 10  # noqa: E731
    return {"label": STOCK_FILL_LABEL, "sellPricePerUnit": sell,
            "pricedTotalCostPerUnit": _round(priced_total, 100.0),
            "pricedMarginPct": margin(priced_total), "stockAdjustedTotalCostPerUnit": adjusted,
            "stockAdjustedMarginPct": margin(adjusted), "linesFromStock": from_stock,
            "linesAtPricedBasis": at_priced}


def _count_of(value: Any) -> int:
    """ready-to-quote.ts ``countOf``: a finite count above 0, floored, else 0."""
    return math.floor(value) if _finite(value) and value > 0 else 0


def derive_ready_to_quote(inp: Mapping[str, Any]) -> dict[str, Any]:
    """ready-to-quote.ts ``deriveReadyToQuote`` (Q2-C ``volumeAdvisories`` pass through)."""
    items: list[dict[str, Any]] = []
    fill, sg = inp.get("fillWeightG"), inp.get("specificGravity")
    item = lambda k, label, sev, detail: {"key": k, "label": label,  # noqa: E731
                                          "severity": sev, "detail": detail}
    items.append(item("fill_weight_g", "Fill weight (g)", "ok", f"{js_string(fill)} g on the "
                      "ASSEMBLY BOM") if fill is not None and fill > 0 else
                 item("fill_weight_g", "Fill weight (g)", "blocker",
                      "ASSEMBLY BOM has no typed fill_weight_g (no silent 500 g default)"))
    if sg is not None and sg > 0 and inp.get("specificGravityConfirmed"):
        items.append(item("specific_gravity", "Specific gravity", "ok", js_string(sg)))
    else:
        items.append(item("specific_gravity", "Specific gravity", "warning",
                          f"Using {js_string(sg)} — confirm on the formulation"
                          if sg is not None and sg > 0 else
                          "Defaulting to 1.00 until Formulation.specificGravity is set"))
    unsourced, linked = inp["unsourcedLineCount"], inp["linkedLineCount"]
    # G9: customer-supplied lines are counted apart, so they never block here.
    lines = _count_of(inp.get("customerSuppliedLineCount"))
    pending = min(_count_of(inp.get("customerPendingLineCount")), lines)
    raws = _count_of(inp.get("customerSuppliedRawCount"))
    if unsourced == 0 and (linked > 0 or lines > 0):
        items.append(item("sourcing", "Packaging sourced", "ok",
                          f"All {lines} line(s) supplied by the customer" if linked == 0 else
                          f"{linked} line(s) linked; estimates cleared; {lines} supplied by the "
                          "customer" if lines > 0 else
                          f"{linked} line(s) linked; estimates cleared"))
    else:
        items.append(item("sourcing", "Packaging sourced",
                          "blocker" if unsourced > 0 else "warning",
                          f"{unsourced} line(s) still on estimatedUnitCost / needsSourcing"
                          if unsourced > 0 else "No ASSEMBLY BOM lines to quote"))
    if lines + raws > 0:
        what = " and ".join(part for part in (f"{lines} packaging line(s)" if lines else None,
                                              f"{raws} raw material(s)" if raws else None) if part)
        supplied = f"{what} supplied by the customer, priced at $0"
        items.append(item("customer_supplied", "Customer-supplied", "warning",
                          f'{supplied}; {pending} line(s) still waiting for "Approve customer '
                          'component"') if pending > 0 else
                     item("customer_supplied", "Customer-supplied", "advisory", supplied))
    hs = inp["missingHsCodeCount"]
    items.append(item("hs_code", "HS code", "ok", "Import HS code present on linked products")
                 if hs == 0 else item("hs_code", "HS code", "warning",
                                      f"{hs} linked product(s) missing Product.hsCode"))
    inc, org = inp["missingIncotermsCount"], inp["missingOriginCount"]
    items.append(item("vendor_card", "Vendor-item card", "ok",
                      "Incoterms and country of origin present") if inc == 0 and org == 0 else
                 item("vendor_card", "Vendor-item card", "warning",
                      f"{inc} missing incoterms, {org} missing origin"))
    items.append(item("labour", "Labour / overhead", "warning",
                      "No routing hours, so assembly and setup labour are priced at $0 and "
                      "reported")
                 if inp["labourIsPlaceholder"] else
                 item("labour", "Labour / overhead", "ok", "Routing hours pinned"))
    sell, retail = inp.get("sellPricePerUnit"), inp.get("retailPerUnit")
    pct = None
    if sell is not None and retail is not None and retail != 0 and _finite(sell) and \
            _finite(retail):
        pct = _js_math_round(((sell - retail) / retail) * 1000) / 10
    return {"ready": all(x["severity"] != "blocker" for x in items), "items": items,
            "sellVsRetail": {"sellPricePerUnit": sell, "retailPerUnit": retail, "deltaPct": pct,
                             "note": "No retail / target price on the project to compare"
                             if pct is None else
                             f"Suggested sell is {'+' if pct > 0 else ''}{js_string(pct)}% vs "
                             "retail / target"},
            "advisories": derive_cost_provenance_advisories(inp.get("provenanceLines") or [],
                                                            inp.get("driftThresholdPct")),
            "marginIfFilledFromStock": derive_margin_if_filled_from_stock(
                sell, inp.get("pricedTotalCostPerUnit"), inp.get("stockFillLines") or []),
            "volumeAdvisories": list(inp.get("volumeAdvisories") or [])}


# ---- quote totals, TASK-2175 matrix fallback, saved offer (lib/quote) -------------------------

def clamp_margin_percent(margin: Any) -> float:
    if not _finite(margin):
        return 0.0
    return min(max(float(margin), 0.0), 99.9)


def derive_unit_price(std_cost: float, margin: Any) -> float:
    return quote_round2(std_cost / (1 - clamp_margin_percent(margin) / 100))


def resolve_unit_price(line: Mapping[str, Any]) -> float:
    std, margin = line.get("stdCost"), line.get("marginPercent")
    if std is not None and std > 0 and margin is not None:
        return derive_unit_price(std, margin)
    return quote_round2(_nc(line.get("unitPrice"), 0))


def line_total(unit_price: float, quantity: Any) -> float:
    return quote_round2(unit_price * (quantity if _finite(quantity) else 0))


def compute_quote_totals(line_totals: Sequence[Any], tax_rate_percent: Any = 0) -> dict[str, float]:
    """quote/totals.ts ``computeQuoteTotals``."""
    total = 0.0
    for value in line_totals:
        total = total + (value if _truthy(value) else 0)
    subtotal = quote_round2(total)
    tax = quote_round2(subtotal * (clamp_margin_percent(tax_rate_percent) / 100))
    return {"subtotal": subtotal, "taxAmount": tax, "totalAmount": quote_round2(subtotal + tax)}


def fallback_matrix_unit_cost(engine_unit_cost: Any, line_std_cost: Any = None,
                              theoretical_fg_cost: Any = None,
                              customer_supplied_count: Any = None) -> dict[str, Any]:
    """quote/matrix-fallback.ts ``fallbackMatrixUnitCost`` (TASK-2175; G-D4, TASK-2611: a
    column whose material the customer sends keeps the engine's value, since a standard cost
    made at PLX prices would put a price back on material the quote prices at $0)."""
    if _finite(engine_unit_cost) and engine_unit_cost > 0:
        return {"unitCost": engine_unit_cost, "source": "engine"}
    if (customer_supplied_count or 0) > 0:
        return {"unitCost": 0, "source": "none"}
    if line_std_cost is not None and _finite(line_std_cost) and line_std_cost > 0:
        return {"unitCost": line_std_cost, "source": "line-std"}
    if theoretical_fg_cost is not None and _finite(theoretical_fg_cost) and theoretical_fg_cost > 0:
        return {"unitCost": theoretical_fg_cost, "source": "theoretical-fg"}
    return {"unitCost": 0, "source": "none"}


def first_positive_line_std_cost(lines: Sequence[Mapping[str, Any]]) -> Any:
    for line in lines:
        std = line.get("stdCost")
        if std is not None and _finite(std) and std > 0:
            return std
    return None


def reprice_margin_tiers(tiers: Sequence[Mapping[str, Any]], unit_cost: float) -> list[dict[str, Any]]:
    out = []
    for tier in tiers:
        m = tier["marginPct"] / 100 if 0 <= tier["marginPct"] < 100 else 0
        out.append({**tier, "sellPricePerUnit": _round(unit_cost / (1 - m), 100.0)})
    return out


def apply_column_unit_cost_fallback(column: Mapping[str, Any], line_std_cost: Any,
                                    theoretical_fg_cost: Any) -> dict[str, Any]:
    resolved = fallback_matrix_unit_cost(column["unitCost"], line_std_cost, theoretical_fg_cost,
                                         len(column.get("customerSupplied") or ()))
    if resolved["source"] in ("engine", "none"):
        return {"column": dict(column), "source": resolved["source"]}
    return {"source": resolved["source"],
            "column": {**column, "unitCost": resolved["unitCost"],
                       "marginTiers": reprice_margin_tiers(column["marginTiers"],
                                                           resolved["unitCost"])}}


def resolve_offer_margin_pct(column: Mapping[str, Any], selections: Sequence[Mapping[str, Any]],
                             quote_margin_target_pct: Any) -> float:
    """quote/matrix.ts ``resolveOfferMarginPct``."""
    explicit = next((s for s in selections if s["askedUnits"] == column["askedUnits"]), None)
    if explicit is not None and _finite(explicit.get("marginPct")):
        return explicit["marginPct"]
    if quote_margin_target_pct is not None and _finite(quote_margin_target_pct):
        return quote_margin_target_pct
    target = next((t for t in column["marginTiers"] if t["isTarget"]), None)
    return target["marginPct"] if target is not None else 0


def price_column_at_margin(column: Mapping[str, Any], margin_pct: Any) -> float:
    """quote/matrix.ts ``priceColumnAtMargin``: the ladder tier, else cost ÷ (1 − m)."""
    tier = next((t for t in column["marginTiers"] if abs(t["marginPct"] - margin_pct) <= 0.01),
                None)
    if tier is not None:
        return tier["sellPricePerUnit"]
    m = margin_pct / 100 if _finite(margin_pct) and 0 <= margin_pct < 100 else 0
    return _round(column["unitCost"] / (1 - m), 100.0)


def offer_line(asked_units: float, unit_cost: float, margin_pct: float,
               unit_price: float) -> dict[str, Any]:
    """quote/service.ts ``buildOfferLineCreates`` arithmetic for one saved asked-volume line."""
    price = quote_round2(unit_price)
    return {"quantity": asked_units, "unitPrice": price,
            "totalPrice": line_total(price, asked_units), "marginPercent": margin_pct,
            "stdCost": unit_cost}


# ---- ERP-D-015 posting (procurement/landed-gl.ts) ----------------------------------------------

def product_kind(product_type: Any) -> str:
    t = (product_type or "").strip().upper() if isinstance(product_type, str) or \
        product_type is None else str(product_type).strip().upper()
    if t in ("RAW_MATERIAL", "RAW"):
        return "RAW_MATERIAL"
    if t in ("PACKAGING", "PACK"):
        return "PACKAGING"
    if t in ("DISTRIBUTION", "RETAIL"):
        return "DISTRIBUTION"
    return "OTHER"


def predominant_kind(types: Sequence[Any]) -> str:
    counts = {"RAW_MATERIAL": 0, "PACKAGING": 0, "DISTRIBUTION": 0, "OTHER": 0}
    for t in types:
        counts[product_kind(t)] += 1
    best = "OTHER"
    for kind in ("RAW_MATERIAL", "PACKAGING", "DISTRIBUTION", "OTHER"):
        if counts[kind] > counts[best]:
            best = kind
    return best


def build_landed_expense_lines(product_types: Sequence[Any], freight_amount: Any = None,
                               duty_amount: Any = None) -> list[dict[str, Any]]:
    """landed-gl.ts ``buildLandedExpenseLines``: freight and duty post as G/L Account lines."""
    kind = predominant_kind(product_types)
    out = []
    freight = js_number(freight_amount)
    if freight is not None and math.isfinite(freight) and freight > 0:
        out.append({"lineType": "Account", "quantity": 1, "unitCost": _round(freight, 100.0),
                    "accountNumber": LANDED_GL["packFreight"] if kind == "PACKAGING"
                    else LANDED_GL["rmFreight"], "description": f"Freight ({kind})"})
    duty = js_number(duty_amount)
    if duty is not None and math.isfinite(duty) and duty > 0:
        account = {"PACKAGING": LANDED_GL["packDuty"],
                   "DISTRIBUTION": LANDED_GL["distDuty"]}.get(kind, LANDED_GL["rmDuty"])
        out.append({"lineType": "Account", "accountNumber": account, "quantity": 1,
                    "unitCost": _round(duty, 100.0),
                    "description": f"Duties & brokerage ({kind})"})
    return out


def _positive(value: Any) -> bool:
    """A finite number above zero (resolvePostedLandedAmounts' test for an actual)."""
    number = js_number(value)
    return number is not None and math.isfinite(number) and number > 0


def resolve_posted_landed_amounts(receipt_freight: Any = None, receipt_duty: Any = None,
                                  po_freight: Any = None, po_duty: Any = None) -> dict[str, float]:
    """landed-gl.ts ``resolvePostedLandedAmounts``: a receipt actual above zero beats the PO
    estimate."""
    def pick(actual: Any, estimate: Any) -> float:
        a = js_number(actual)
        if a is not None and math.isfinite(a) and a > 0:
            return a
        e = js_number(estimate)
        return e if e is not None and math.isfinite(e) and e > 0 else 0.0
    return {"freight": pick(receipt_freight, po_freight), "duty": pick(receipt_duty, po_duty)}


# ---- labour hours, formula and ASSEMBLY BOM selection, raw cost (LOG-08) -----------------------
#
# Record lists are reads: a list (possibly empty) is what the table holds; None is a read that
# did not happen, reproduced as the Portal's failed read (formula pin: pin_unresolved; routing,
# draft costs: none; setup standards: the workbook seed; the ASSEMBLY BOM link, whose failed
# read the Portal does not catch: skipped). quote_landed reports each as RECORDS_NOT_READ.

def _pos(value: Any) -> Any:
    """``positiveOrNull``: a finite number above zero, else None."""
    return value if _finite(value) and value > 0 else None


def _gt0(value: Any) -> bool:
    """``value != null && value > 0`` for the numbers a Prisma row holds."""
    return _is_num(value) and value > 0


def _newest(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Prisma ``orderBy: { updatedAt: "desc" }`` (stable for equal keys)."""
    return sorted(rows, key=lambda row: _iso(row.get("updatedAt")) or "", reverse=True)


def labour_cost_per_unit(hours_per_unit: Any, assembly_rate: Any = None) -> float | None:
    """fg-routing.ts ``labourCostPerUnit``."""
    hours = _pos(hours_per_unit)
    rate = _pos(_nc(assembly_rate, RATE_DEFAULTS["assemblyRate"]))
    return None if hours is None or rate is None else hours * rate


def amortise_setup_hours(setup_minutes: Any, changeover_minutes: Any, charges: Any,
                         asked_units: Any, crew: Any = None) -> dict[str, Any]:
    """setup-standards.ts ``amortiseSetupHours``: setup once per run and changeover once per
    charge beyond the first, in person-hours (× crew, default 1), over the asked units."""
    reasons: list[str] = []
    people = _nc(_pos(crew), 1)
    count = max(1, _js_math_round(_nc(_pos(charges), 1)))
    setup = _pos(setup_minutes)
    if setup is None:
        reasons.append("setup-minutes-missing")
    changeover = _pos(changeover_minutes)
    if changeover is None and count > 1:
        reasons.append("changeover-minutes-missing-multi-charge")
    per_run = (_nc(setup, 0) * people) / 60
    per_charge = (_nc(changeover, 0) * people) / 60
    qty = _pos(asked_units)
    if qty is None:
        reasons.append("asked-units-missing-no-amortisation-basis")
    per_unit = None if qty is None else (per_run + per_charge * (count - 1)) / qty
    return {"setupHoursPerUnit": per_unit, "perRunSetupHours": per_run,
            "perChargeChangeoverHours": per_charge,
            "charges": count, "reasons": reasons}


def compounding_crew_hours_per_unit(compounder_count: Any, hours_per_charge: Any, charges: Any,
                                    asked_units: Any) -> float | None:
    """setup-standards.ts ``compoundingCrewHoursPerUnit``: crew × hours per charge × charges
    over the asked units."""
    crew, hours, count, qty = (_pos(compounder_count), _pos(hours_per_charge), _pos(charges),
                               _pos(asked_units))
    if crew is None or hours is None or count is None or qty is None:
        return None
    return (crew * hours * count) / qty


def quote_labour_from_routing(routing: Mapping[str, Any] | None, assembly_rate: Any = None,
                              run_qty: Any = None) -> dict[str, Any]:
    """fg-routing.ts ``quoteLabourFromRouting``: the routing's standard hours plus its setup
    amortised over one charge of the run quantity."""
    unrouted = {"status": "unrouted", "assemblyHoursPerUnit": None, "assemblyLabourPerUnit": None}
    if routing is None:
        return dict(unrouted, reason="no-routing")
    base = _pos(routing.get("standardHoursPerUnit"))
    if base is None:
        return dict(unrouted, reason="hours-missing")
    hours = base
    setup, crew, qty = (_pos(routing.get("standardSetupMinutes")),
                        _pos(routing.get("totalOperators")), _pos(run_qty))
    if setup is not None and crew is not None and qty is not None:
        hours = hours + _nc(amortise_setup_hours(setup, None, 1, qty, crew)["setupHoursPerUnit"], 0)
    return {"status": "routed", "assemblyHoursPerUnit": hours,
            "assemblyLabourPerUnit": labour_cost_per_unit(hours, assembly_rate), "reason": "ok"}


def assembly_labour_per_unit(headcount: Any, rate_bpm: Any) -> float:
    """template-cost-matrix.ts ``assemblyLabourPerUnit``: headcount ÷ (BPM × 60) hours/unit."""
    if not (_gt0(headcount) and _gt0(rate_bpm)):
        raise LandedCostError("assembly labour requires positive headcount and rateBpm")
    return headcount / (rate_bpm * 60)


def compounding_labour_per_unit(compounder_count: Any, hours: Any, yield_units: Any) -> float:
    """template-cost-matrix.ts ``compoundingLabourPerUnit``: compounders × shift hours ÷ yield."""
    if not (_gt0(compounder_count) and _gt0(hours) and _gt0(yield_units)):
        raise LandedCostError("compounding labour requires positive compounders, hours, and yield")
    return (compounder_count * hours) / yield_units


_SETUP_RANK = {"confirmed": 0, "draft": 1, "inferred": 2, "excel": 3, "seed": 4}
_DRAFT_RANK = {"confirmed": 0, "draft": 1, "seed": 4}
_PROVENANCE_RANK = {"confirmed": 0, "draft": 1, "excel": 2, "inferred": 3}  # line-tank-keys.ts
# setup-standards.ts workbookSeedRow: the workbook wedge (6.5 h uptime - 4 h productive setup,
# 7.5 h paid - 6.5 h uptime changeover), the Portal's code default. Never mutated.
WORKBOOK_SETUP_SEED = {"lineNumber": 0, "groupCode": "*", "actionKey": "*",
                       "setupMinutes": (6.5 - 4) * 60, "changeoverMinutes": (7.5 - 6.5) * 60,
                       "source": "seed"}
RUN_VOLUME_TIER_CODES = tuple(code for code, _ in RUN_VOLUME_TIERS)
DEFAULT_RUN_VOLUME_TIER_CODE = "5k"


def _rank(source: Any, table: Mapping[str, int]) -> int:
    """``setupProvenanceRank`` / ``draftCostProvenanceRank``, then line-tank ``provenanceRank``."""
    if not isinstance(source, str) or not source:
        return 99
    key = source.lower()
    return table[key] if key in table else _PROVENANCE_RANK.get(key, 50)


def _js_max(values: Sequence[Any]) -> float:
    """``Math.max(...values)`` over row minutes (null reads as 0)."""
    numbers = [0 if v is None else v for v in values]
    if not all(_is_num(v) for v in numbers):
        raise LandedCostError("setup_standard_minutes_not_numeric")
    return max(numbers)


def resolve_setup_standard(rows: Sequence[Mapping[str, Any]], *, line_number: Any = None,
                           group_code: Any = None, action_keys: Any = None) -> dict[str, Any]:
    """setup-standards.ts ``resolveSetupStandard``: confirmed beats draft beats inferred beats
    excel/seed, then the most specific key (line, group, action), then the largest minutes;
    always falls back to the workbook seed."""
    line = line_number if _gt0(line_number) else None
    group = group_code.strip() if isinstance(group_code, str) and group_code.strip() else None
    keys = [k.strip() for k in action_keys or [] if isinstance(k, str) and k.strip()]

    def matches(row: Mapping[str, Any]) -> bool:
        at_line, at_group = row.get("lineNumber"), row.get("groupCode")
        return (at_line == 0 or (line is not None and at_line == line)) \
            and (at_group == "*" or (group is not None and at_group == group)) \
            and (row.get("actionKey") == "*" or row.get("actionKey") in keys)

    def specificity(row: Mapping[str, Any]) -> int:
        return (4 if row.get("lineNumber") != 0 else 0) \
            + (2 if row.get("groupCode") != "*" else 0) + (1 if row.get("actionKey") != "*" else 0)

    catalog = list(rows) or [WORKBOOK_SETUP_SEED]
    pool = [row for row in catalog if matches(row)] or [WORKBOOK_SETUP_SEED]
    best_rank, best_spec = math.inf, -1
    for row in pool:
        rank, spec = _rank(row.get("source"), _SETUP_RANK), specificity(row)
        if rank < best_rank or (rank == best_rank and spec > best_spec):
            best_rank, best_spec = rank, spec
    winners = [row for row in pool if _rank(row.get("source"), _SETUP_RANK) == best_rank
               and specificity(row) == best_spec]
    return {"setupMinutes": _js_max([row.get("setupMinutes") for row in winners]),
            "changeoverMinutes": _js_max([row.get("changeoverMinutes") for row in winners]),
            "source": _nc(winners[0].get("source"), "seed"), "matched": winners}


def primary_routing(fg_routings: Sequence[Mapping[str, Any]] | None, project_key: Any,
                    setup_standards: Sequence[Mapping[str, Any]] | None
                    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``getPrimaryRouting(key, {persist: false})`` (refreshPrimaryRoutingSetup): the primary
    FgRouting of the project, its blank setup/changeover minutes filled, unsaved, from the setup
    standard of its line, group and actions. Returns (routing, fill): fill names the filled
    fields and the standard that filled them, None when nothing was filled."""
    key = project_key.strip() if isinstance(project_key, str) else ""
    if not key or fg_routings is None:
        return None, None
    row = next((r for r in fg_routings
                if r.get("projectKey") == key and r.get("isPrimary") is True), None)
    if row is None:
        return None, None
    if row.get("standardSetupMinutes") is not None and \
            row.get("standardChangeoverMinutes") is not None:
        return dict(row), None
    actions = row.get("actionKeys")
    standard = resolve_setup_standard(
        setup_standards if setup_standards is not None else [WORKBOOK_SETUP_SEED],
        line_number=row.get("lineNumber"), group_code=row.get("groupCode"),
        action_keys=[k for k in actions if isinstance(k, str)] if isinstance(actions, list) else [])
    fields = {"standardSetupMinutes": "setupMinutes",
              "standardChangeoverMinutes": "changeoverMinutes"}
    filled = [f for f in fields if row.get(f) is None]
    return dict(row, **{f: _nc(row.get(f), standard[m]) for f, m in fields.items()}), \
        {"fields": filled, "standard": standard}


_DRAFT_FIELDS = ("tankCode", "batchType", "compounderCount", "hours", "yieldUnits", "labourPerUnit",
                 "lineNumber", "lineCode", "headcount", "rateBpm", "uptimeHours", "setupMinutes",
                 "teardownMinutes", "overheadPerUnit")


def draft_cost_rows(rows: Sequence[Mapping[str, Any]] | None,
                    project_key: Any) -> list[dict[str, Any]] | None:
    """``loadDraftCostStore(key).drafts`` (toDraftRow): the project's draft cost override rows;
    a row without a run-volume tier is skipped; an unknown source reads as ``seed``. None when
    the project has no key or the rows were not read."""
    key = project_key.strip() if isinstance(project_key, str) else ""
    if not key or rows is None:
        return None
    out = []
    for row in rows:
        if row.get("projectKey") != key:
            continue
        code = (row.get("runVolumeTier") or {}).get("code")
        if not _truthy(code):
            continue
        tier = code.strip() if isinstance(code, str) else None
        source = row.get("source")
        out.append({"projectKey": row.get("projectKey"), "fgItemId": row.get("fgItemId"),
                    "process": "assembly" if row.get("process") == "assembly" else "compounding",
                    "runVolumeTierCode": tier if tier in RUN_VOLUME_TIER_CODES
                    else DEFAULT_RUN_VOLUME_TIER_CODE,
                    "templateId": row.get("templateId"),
                    "source": source if source in ("confirmed", "draft") else "seed",
                    **{f: row.get(f) for f in _DRAFT_FIELDS}, "_source": row.get("_source")})
    return out


def resolve_draft_cost(drafts: Sequence[Mapping[str, Any]] | None, project_key: Any,
                       process: str, tier_code: Any) -> dict[str, Any] | None:
    """draft-cost-overrides.ts ``resolveDraftCost`` (no template candidate, as the engine asks):
    the project's row for the process and tier with the best provenance."""
    candidates = [row for row in drafts or [] if row.get("projectKey") == project_key
                  and row.get("process") == process and row.get("runVolumeTierCode") == tier_code]
    if not candidates:
        return None
    return dict(sorted(candidates, key=lambda row: _rank(row.get("source"), _DRAFT_RANK))[0])


def _draft_assembly_hours(row: Mapping[str, Any]) -> float | None:
    if _gt0(row.get("labourPerUnit")):
        return row["labourPerUnit"]
    if _gt0(row.get("headcount")) and _gt0(row.get("rateBpm")):
        return assembly_labour_per_unit(row["headcount"], row["rateBpm"])
    return None


def _draft_compounding_hours(row: Mapping[str, Any]) -> float | None:
    if _gt0(row.get("labourPerUnit")):
        return row["labourPerUnit"]
    if _gt0(row.get("compounderCount")) and _gt0(row.get("hours")) and _gt0(row.get("yieldUnits")):
        return compounding_labour_per_unit(row["compounderCount"], row["hours"], row["yieldUnits"])
    return None


def resolve_quote_hours(project_key: Any, drafts: Sequence[Mapping[str, Any]] | None,
                        routing: Mapping[str, Any] | None, run_qty: Any, fg_target_qty: Any,
                        assembly_rate: Any) -> dict[str, Any]:
    """project-landed-quote.ts ``resolveQuoteHours``: draft tier rows win for the priced tier
    (assembly from labourPerUnit or headcount ÷ BPM, plus setup amortised over the run;
    compounding from labourPerUnit or crew × shift ÷ yield); the primary routing is the fallback
    per process; neither is ``missing`` and reported, never defaulted."""
    reasons: list[str] = []
    rows = [row for row in drafts or [] if row.get("projectKey") == project_key]
    has_drafts = len(rows) > 0
    qty_basis = _nc(run_qty, fg_target_qty)
    tier = None
    if has_drafts:
        tier = pick_run_volume_tier(qty_basis)
        if not tier:
            tier = DEFAULT_RUN_VOLUME_TIER_CODE
            reasons.append("tier-defaulted-no-qty-basis")
    sources = {"assembly": "missing", "compounding": "missing"}
    run_hours = assembly_hours = compounding_hours = None
    if tier and drafts is not None and project_key:
        assembly = resolve_draft_cost(drafts, project_key, "assembly", tier)
        compounding = resolve_draft_cost(drafts, project_key, "compounding", tier)
        hours = _draft_assembly_hours(assembly) if assembly else None
        if hours is not None:
            sources["assembly"] = "draft_tier"
            run_hours = assembly_hours = hours
            if _gt0(assembly.get("setupMinutes")) and _gt0(assembly.get("headcount")) and \
                    _gt0(qty_basis):
                assembly_hours = assembly_hours + _nc(amortise_setup_hours(
                    assembly["setupMinutes"], None, 1, qty_basis,
                    assembly["headcount"])["setupHoursPerUnit"], 0)
        hours = _draft_compounding_hours(compounding) if compounding else None
        if hours is not None:
            sources["compounding"] = "draft_tier"
            compounding_hours = hours
    routed = quote_labour_from_routing(routing, assembly_rate, run_qty)
    usable = routed["status"] == "routed" and routed["assemblyHoursPerUnit"] is not None
    if sources["assembly"] == "missing":
        if usable:
            sources["assembly"] = "routing"
            run_hours = _nc((routing or {}).get("standardHoursPerUnit"),
                            routed["assemblyHoursPerUnit"])
            assembly_hours = routed["assemblyHoursPerUnit"]
        else:
            reasons.append(f"assembly-hours-missing:{routed['reason']}")
    if sources["compounding"] == "missing":
        if usable:
            sources["compounding"] = "routing"
            compounding_hours = routed["assemblyHoursPerUnit"]
        else:
            reasons.append(f"compounding-hours-missing:{routed['reason']}")
    same = sources["assembly"] == sources["compounding"]
    return {"labour": {"source": sources["assembly"] if same else "mixed",
                       "assemblySource": sources["assembly"],
                       "compoundingSource": sources["compounding"], "pricedTierCode": tier,
                       "pricedQtyBasis": qty_basis, "assemblyRunHoursPerUnit": run_hours,
                       "assemblyHoursPerUnit": assembly_hours,
                       "compoundingHoursPerUnit": compounding_hours, "reasons": reasons},
            "hasDraftOverrides": has_drafts}


def resolve_expected_units_override(drafts: Sequence[Mapping[str, Any]] | None, project_key: Any,
                                    priced_tier_code: Any) -> dict[str, Any] | None:
    """project-landed-quote.ts ``resolveExpectedUnitsOverride``: the priced assembly tier row's
    expected units per shift (yieldUnits) and its up-time hours, for the OD-14 absorption."""
    if drafts is None or not project_key or not priced_tier_code:
        return None
    row = resolve_draft_cost(drafts, project_key, "assembly", priced_tier_code)
    if not row or not _gt0(row.get("yieldUnits")):
        return None
    return {"expectedUnitsPerShift": row["yieldUnits"],
            "uptimeHours": row["uptimeHours"] if _gt0(row.get("uptimeHours")) else None}


def volume_column_labour(asked_units: Any, project_key: Any,
                         drafts: Sequence[Mapping[str, Any]] | None,
                         routing: Mapping[str, Any] | None,
                         setup_standard: Mapping[str, Any] | None, charges: Any,
                         assembly_rate: Any, rates: Mapping[str, Any] | None) -> dict[str, Any]:
    """The hours of ``buildVolumeMatrixColumn`` at one asked volume: W2b hours at that volume,
    setup minutes from the tier's draft row, else the routing, else the setup standard;
    changeover from the setup standard per charge beyond the first; compounding from the
    draft crew per charge when a vessel pick exists, else the per-unit hours. ``charges``: the
    vessel pick's charge count (an input; None = no pick, one charge assumed)."""
    count = _nc(charges, 1)
    base = resolve_quote_hours(project_key, drafts, routing, asked_units, None,
                               assembly_rate)["labour"]
    reasons = list(base["reasons"])
    tier = _nc(base["pricedTierCode"], pick_run_volume_tier(asked_units))
    resolvable = drafts is not None and project_key and tier
    assembly = resolve_draft_cost(drafts, project_key, "assembly", tier) if resolvable else None
    compounding = resolve_draft_cost(drafts, project_key, "compounding", tier) \
        if resolvable else None
    setup_minutes, setup_source = None, "missing"
    if assembly and _gt0(assembly.get("setupMinutes")):
        setup_minutes, setup_source = assembly["setupMinutes"], "draft_tier"
    elif routing and _gt0(routing.get("standardSetupMinutes")):
        setup_minutes, setup_source = routing["standardSetupMinutes"], "routing"
    elif setup_standard:
        setup_minutes = setup_standard["setupMinutes"]
        setup_source = f"setup-standard:{setup_standard['source']}"
    changeover = (setup_standard or {}).get("changeoverMinutes")
    crew = _nc((assembly or {}).get("headcount"), (routing or {}).get("totalOperators"))
    assembly_hours = None
    if base["assemblyRunHoursPerUnit"] is not None:
        amortised = amortise_setup_hours(setup_minutes, changeover, count, asked_units, crew)
        reasons += [r for r in amortised["reasons"] if r not in reasons]
        assembly_hours = base["assemblyRunHoursPerUnit"] + _nc(amortised["setupHoursPerUnit"], 0)
    compounding_hours, basis = None, "missing"
    per_charge = compounding_crew_hours_per_unit(
        (compounding or {}).get("compounderCount"), (compounding or {}).get("hours"), count,
        asked_units) if charges is not None else None
    if per_charge is not None:
        compounding_hours, basis = per_charge, "crew_per_charge"
    elif base["compoundingHoursPerUnit"] is not None:
        compounding_hours, basis = base["compoundingHoursPerUnit"], "per_unit_fallback"
        if count > 1:
            reasons.append("compounding-hours-per-unit-basis-no-charge-scaling "
                           "(crew-per-charge facts missing)")
    else:
        reasons.append("compounding-hours-missing-for-volume")
    override = resolve_expected_units_override(drafts, project_key, base["pricedTierCode"])
    return {"labour": {"assemblySource": base["assemblySource"],
                       "compoundingSource": "missing" if basis == "missing"
                       else base["compoundingSource"],
                       "tierCode": tier, "assemblyRunHoursPerUnit": base["assemblyRunHoursPerUnit"],
                       "assemblyHoursPerUnit": assembly_hours,
                       "compoundingHoursPerUnit": compounding_hours, "compoundingBasis": basis,
                       "setupSource": setup_source, "reasons": reasons},
            "overhead": _overhead(rates, assembly_hours, base["assemblySource"], override)}


def raw_cost_per_kg(product: Mapping[str, Any], supply_source: str = "PLX") -> Any:
    """raw-cost.ts ``rawCostPerKg``: 0 when the customer supplies the raw (G9), else the FM
    average cost (Product.unitCost) when above 0, else the first preferred active card's
    systemCost (CAD per kg) above 0, else 0."""
    if supply_source != "PLX":
        return 0
    if _pos(product.get("unitCost")) is not None:
        return product["unitCost"]
    card = _raw_cost_card(product.get("supplierProducts") or [])
    return card["systemCost"] if card is not None else 0


def _raw_cost_card(cards: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((c for c in cards if _truthy(c.get("isPreferred")) and _truthy(c.get("isActive"))
                 and _pos(c.get("systemCost")) is not None), None)


def _raw_cost_cards(product: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``RAW_COST_CARD_INCLUDE``: active preferred cards, newest first."""
    return _newest([c for c in product.get("supplierProducts") or []
                    if c.get("isActive") is True and c.get("isPreferred") is True])


def quote_raw_cost_per_kg(product: Mapping[str, Any], supply_source: str = "PLX") -> Any:
    """gatherQuoteFacts' ``costPerKg``: rawCostPerKg over the cards the Portal's include loads."""
    return raw_cost_per_kg(dict(product, supplierProducts=_raw_cost_cards(product)), supply_source)


def bare_project_key(project_key: str) -> str:
    """overview-shared.ts ``bareProjectKey``: ``fm-proj-77`` -> ``77``."""
    key = project_key.strip()
    if not key.startswith("fm-proj-"):
        return key
    return key[len("fm-proj-"):] or key


def resolve_quote_formulation(project_key: Any, project_id: Any,
                              subassemblies: Sequence[Mapping[str, Any]] | None,
                              fg_items: Sequence[Mapping[str, Any]] | None,
                              formulations: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """formulation-resolution.ts ``resolveQuoteFormulation``: the project pin (the newest formula
    subassembly row of the project's id, key or bare key), else the FG pin, else none; the pinned
    formulation must exist, be undeleted and not OBSOLETE. A failed read, or a pin that cannot
    be loaded, is ``pin_unresolved``: never a fallthrough to another formula. ``pin``: the record
    that pinned it."""
    key = project_key.strip() if isinstance(project_key, str) and project_key.strip() else None
    unresolved = {"formulation": None, "source": "pin_unresolved", "pin": None}
    pin_keys = list(dict.fromkeys(
        k for k in (project_id.strip() if isinstance(project_id, str) else None, key,
                    bare_project_key(key) if key else None) if k))
    pin = None
    if pin_keys:
        if subassemblies is None:
            return unresolved
        rows = _newest([r for r in subassemblies if r.get("kind") == "formula"
                        and r.get("fmProjectId") in pin_keys
                        and r.get("formulationId") is not None])
        pin = rows[0] if rows else None
    source, pinned = "project_pin", (pin or {}).get("formulationId")
    if not pinned and key:
        if fg_items is None:
            return unresolved
        pin = next((r for r in fg_items if r.get("projectKey") == key), None)
        source, pinned = "fg_pin", (pin or {}).get("pinnedFormulationId")
    if not pinned:
        return {"formulation": None, "source": "none", "pin": None}
    if formulations is None:
        return dict(unresolved, pin=pin)
    row = next((f for f in formulations if f.get("id") == pinned and f.get("status") is not None
                and f.get("status") != "OBSOLETE" and f.get("deletedAt") is None), None)
    if row is None:
        return dict(unresolved, pin=pin)
    return {"formulation": row, "source": source, "pin": pin}


def select_assembly_bom(project: Mapping[str, Any],
                        subassemblies: Sequence[Mapping[str, Any]] | None,
                        boms: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """project-landed-quote.ts ``findAssemblyBomForQuote``: the BOM the project's newest assembly
    subassembly row pins (ASSEMBLY, undeleted), else the newest-version ASSEMBLY BOM stamped with
    the project's key or id (or gold id). Returns ``{"bom", "link"}``."""
    keys = list(dict.fromkeys(k for k in (project.get("id"), project.get("projectKey")) if k))
    live = [b for b in boms or [] if b.get("category") == "ASSEMBLY" and b.get("deletedAt") is None]
    if keys and subassemblies is not None:
        links = _newest([r for r in subassemblies if r.get("kind") == "assembly"
                         and r.get("fmProjectId") in keys and r.get("bomId") is not None])
        if links and _truthy(links[0].get("bomId")):
            pinned = next((b for b in live if b.get("id") == links[0]["bomId"]), None)
            if pinned is not None:
                return {"bom": pinned, "link": links[0]}
    stamped = []
    if project.get("projectKey"):
        stamped.append(("projectKey", project["projectKey"]))
    if project.get("id"):
        stamped.append(("projectId", project["id"]))
    if project.get("goldId") and project.get("goldId") != project.get("id"):
        stamped.append(("projectId", project["goldId"]))
    rows = [b for b in live if any(b.get(field) == value for field, value in stamped)]
    rows = sorted(_newest(rows), key=lambda b: b.get("version") if _is_num(b.get("version")) else 0,
                  reverse=True)
    return {"bom": rows[0] if rows else None, "link": None}


# ---- G3 supply source: who supplies a component (lib/customer-materials) ------------------------

SUPPLY_SOURCES = ("PLX", "CUSTOMER", "CUSTOMER_PENDING")
CUSTOMER_SOURCING_PATH = "CUSTOMER"
SOURCING_CLOSED_STATUSES = ("COMPLETED", "CANCELLED", "LINKED")  # sourcing/status.ts
_ANCHOR_PREFIXES = ("fm-cust-", "local-cust-")  # customers/anchor.ts FM_/LOCAL_ANCHOR_PREFIX
_FM_RAW_ID = re.compile(r"fm-raw-([0-9]+)", re.I)
_FM_RAW_EXTERNAL_ID = re.compile(r"(?:fm-raw-)?([0-9]+)", re.I)
# fact key -> the Portal relation the supply-source read selects from. Read only when the quote
# asks about a product or BOM line, as the Portal reads it; a needed list that is None is the
# Portal's failed read, which stops the quote (SupplySourceReadError), never PLX pricing.
SUPPLY_RELATIONS = {"customers": "app_customer", "products": "app_product",
                    "sourcing_requests": "app_sourcing_request",
                    "customer_supplied_items": "app_customer_supplied_item"}


def _id_text(value: Any) -> str:
    """``value?.trim()`` of an id: "" for null; anything but text fails closed."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LandedCostError("supply_source_id_must_be_text")
    return js_trim(value)


def _unique_ids(ids: Sequence[Any]) -> list[str]:
    """supply-source.ts ``uniqueIds``: trimmed, blank dropped, first occurrence kept."""
    return list(dict.fromkeys(t for t in map(_id_text, ids) if t))


def supply_source_from_facts(registry: str, requests: Sequence[Mapping[str, Any]]) -> str:
    """supply-source-shared.ts ``supplySourceFromFacts``: an open CUSTOMER-path request is
    CUSTOMER_PENDING (an unknown status counts as open), else an ACTIVE registry row is
    CUSTOMER, else PLX."""
    if any(r.get("status") not in SOURCING_CLOSED_STATUSES for r in requests):
        return "CUSTOMER_PENDING"
    return "CUSTOMER" if registry == "ACTIVE" else "PLX"


def raw_material_id_for_product(product: Mapping[str, Any]) -> str:
    """raw-product.ts ``rawMaterialIdForProduct``: the FileMaker raw id of the external id
    (``fm-raw-<n>`` or a bare ``<n>``), else of an ``fm-raw-<n>`` product id, else the id."""
    external = product.get("externalId")
    match = _FM_RAW_EXTERNAL_ID.fullmatch(js_trim(external)) if isinstance(external, str) \
        else None
    match = match or _FM_RAW_ID.fullmatch(js_trim(product["id"]))
    return match.group(1) if match else product["id"]


def resolve_supply_sources(customer_key: str, product_ids: Sequence[Any],
                           bom_lines: Sequence[Mapping[str, Any]],
                           facts: Mapping[str, Any]) -> dict[str, Any]:
    """supply-source.ts ``resolveSupplySources`` for one customer (its unified key, resolved
    through its gold anchor) over the records it reads (``SUPPLY_RELATIONS``): PLX, CUSTOMER or
    CUSTOMER_PENDING per product (formula raws) and per BOM line. ``records``: the anchor,
    registry rows and requests that decided each answer, keyed ``product:<id>`` / ``line:<id>``."""
    pids = _unique_ids(product_ids)
    lids = _unique_ids([line.get("id") for line in bom_lines])
    out: dict[str, Any] = {"byProductId": {}, "byBomLineId": {}, "records": {}}
    if not pids and not lids:
        return out

    def read(name: str) -> list[Mapping[str, Any]]:
        rows = facts.get(name)
        if rows is None:
            raise LandedCostError(f"supply_source_read_failed:{SUPPLY_RELATIONS[name]}")
        return list(rows)

    key = _id_text(customer_key)
    anchor = None
    if key:
        candidates = [prefix + key for prefix in _ANCHOR_PREFIXES]
        anchor = next((c for c in read("customers") if c.get("deletedAt") is None and (
            c.get("externalId") in candidates or c.get("id") in [key, *candidates])), None)
    gold_id = anchor["id"] if anchor else None
    line_product = {line.get("id"): line.get("productId") for line in bom_lines
                    if line.get("id") in lids}
    requests = read("sourcing_requests") if lids else []
    raw_products = [p for p in read("products") if p.get("id") in pids
                    and p.get("productType") == "RAW_MATERIAL"] if pids else []
    registry_ids = _unique_ids([*pids, *line_product.values()])
    rows = [r for r in read("customer_supplied_items") if r.get("customerId") == gold_id
            and r.get("productId") in registry_ids] if gold_id and registry_ids else []
    registry = {r["productId"]: ("ACTIVE" if _truthy(r.get("isActive")) else "STOPPED", r)
                for r in rows}
    raw_requests: dict[str, list[Mapping[str, Any]]] = {}
    keys = _unique_ids([customer_key, gold_id]) if raw_products else []
    if keys:
        by_raw_id: dict[str, list[str]] = {}
        for product in raw_products:
            by_raw_id.setdefault(raw_material_id_for_product(product), []).append(product["id"])
        raw_ids = [product["id"] for product in raw_products]
        for request in read("sourcing_requests"):
            if not (request.get("materialType") == "RAW"
                    and request.get("sourcingPath") == CUSTOMER_SOURCING_PATH
                    and request.get("status") is not None  # SQL NOT IN drops a null
                    and request.get("status") not in SOURCING_CLOSED_STATUSES
                    and request.get("customerId") in keys
                    and (request.get("linkedProductId") in raw_ids
                         or request.get("rawMaterialId") in by_raw_id)):
                continue
            named = list(by_raw_id.get(request.get("rawMaterialId"), [])) \
                if request.get("rawMaterialId") else []
            if request.get("linkedProductId") in raw_ids:
                named.append(request["linkedProductId"])
            for product_id in dict.fromkeys(named):
                raw_requests.setdefault(product_id, []).append(request)
    by_line: dict[str, list[Mapping[str, Any]]] = {}
    for request in requests:
        if request.get("bomLineId") in lids and \
                request.get("sourcingPath") == CUSTOMER_SOURCING_PATH:
            by_line.setdefault(request["bomLineId"], []).append(request)

    def answer(product_id: Any, asked: list[Mapping[str, Any]], record_key: str) -> str:
        state, row = registry.get(product_id, ("NONE", None)) if product_id else ("NONE", None)
        source = supply_source_from_facts(state, asked)
        basis = asked if source == "CUSTOMER_PENDING" else [anchor, row] if row else []
        out["records"][record_key] = [r for r in basis if r is not None]
        return source

    for product_id in pids:
        out["byProductId"][product_id] = answer(product_id, raw_requests.get(product_id, []),
                                                "product:" + product_id)
    for line_id in lids:
        out["byBomLineId"][line_id] = answer(line_product.get(line_id), by_line.get(line_id, []),
                                             "line:" + line_id)
    return out


def _supply_for(sources: Mapping[str, str], id_: Any) -> str:
    """project-landed-quote.ts ``supplySourceFor``: an id never asked about (blank) is PLX."""
    key = _id_text(id_)
    return (sources.get(key) if key else None) or "PLX"


def _round_qty(value: float) -> float:
    """project-landed-quote.ts ``roundQty``: ``Number(n.toPrecision(10))`` (exact decimal,
    ties away from zero, as toPrecision rounds)."""
    if not math.isfinite(value) or value == 0:
        return value
    exact = Decimal(value)
    return float(exact.quantize(Decimal(1).scaleb(exact.adjusted() - 9), rounding=ROUND_HALF_UP))


def customer_supplied_requirements(materials: Sequence[Mapping[str, Any]],
                                   units: Any) -> list[dict[str, Any]]:
    """project-landed-quote.ts ``customerSuppliedRequirements``: the quantity the customer must
    send for ``units`` finished units (null without a unit basis or a per-unit quantity)."""
    basis = units if _finite(units) and units > 0 else None
    return [dict(m, requiredQty=_round_qty(basis * m["qtyPerUnit"])
                 if basis is not None and m["qtyPerUnit"] is not None else None)
            for m in materials]


# ---- the engine: quoteProjectLanded over supplied facts ----------------------------------------

def _active_cards(product: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """Prisma include of gatherQuoteFacts: ``supplierProducts where isActive, orderBy
    isPreferred desc, updatedAt desc``; tiers ``orderBy minQty asc``."""
    if not product:
        return []
    cards = [c for c in product.get("supplierProducts") or [] if c.get("isActive") is True]
    cards.sort(key=lambda c: _iso(c.get("updatedAt")) or "", reverse=True)
    cards.sort(key=lambda c: c.get("isPreferred") is True, reverse=True)
    return cards


def _tiers(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(({"minQty": t.get("minQty"), "unitCost": t.get("unitCost"),
                    "effectiveFrom": t.get("effectiveFrom"), "updatedAt": t.get("updatedAt"),
                    "_source": t.get("_source")} for t in card.get("tiers") or []),
                  key=lambda t: t["minQty"])


# fact key -> the Portal relation its records come from (a None list is a read that did not
# happen; quote_landed reports it as RECORDS_NOT_READ)
QUOTE_RELATIONS = {"project_subassemblies": "app_project_subassembly", "fg_items": "app_fg_item",
                   "formulations": "app_formulation", "boms": "app_bill_of_materials",
                   "fg_routings": "app_fg_routing",
                   "draft_cost_overrides": "app_draft_cost_override",
                   "setup_standards": "app_line_action_setup_standard"}


def quote_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """``gatherQuoteFacts`` over the records the Portal reads: ``project`` (the QuoteProjectRef:
    id, projectKey, goldId, customerId), the RateSettings row, project core (``target_price``,
    ``customer_id``), the record lists of ``QUOTE_RELATIONS`` that its queries select among
    (formula pin, FG item, formulations with ingredient products and their supplier cards,
    ASSEMBLY BOMs with lines/products/cards/tiers) and of ``SUPPLY_RELATIONS`` (G9: who supplies
    each BOM line and formula raw). A list is a read; None is a read that did not happen."""
    project = facts.get("project") or {}
    project_key = project.get("projectKey")
    subassemblies, fg_items = facts.get("project_subassemblies"), facts.get("fg_items")
    formulations = facts.get("formulations")
    resolved = resolve_quote_formulation(project_key, project.get("id"), subassemblies, fg_items,
                                         formulations)
    formulation = resolved["formulation"]
    sg_row = next((f for f in formulations or [] if f.get("id") == formulation["id"]), None) \
        if formulation else None
    selected = select_assembly_bom(project, subassemblies, facts.get("boms"))
    bom = selected["bom"]
    fg_key = project_key.strip() if isinstance(project_key, str) else ""
    fg = next((r for r in fg_items or [] if r.get("projectKey") == fg_key), None) \
        if fg_key else None
    lines_in = [line for line in (bom or {}).get("lines") or [] if line.get("deletedAt") is None]
    ingredients = (formulation or {}).get("ingredients") or []
    core = facts.get("project_core") or {}
    # G9: one supply-source batch for the project's customer, keyed as the sourcing writers key
    # it (the PD spine's core.customer_id), else the gold project's customer.
    supply = resolve_supply_sources(
        _id_text(core.get("customer_id")) or _id_text(project.get("customerId")),
        [ing.get("productId") for ing in ingredients], lines_in, facts)
    fill_weight_g = _nc((bom or {}).get("fillWeightG")) if bom else None
    specific_gravity = (sg_row or {}).get("specificGravity")
    mass = fill_mass_kg(fill_weight_g, specific_gravity)
    customer_supplied: list[dict[str, Any]] = []
    supply_basis: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    customer_raws: dict[Any, dict[str, Any]] = {}  # one entry per raw, inclusion summed
    raw = []
    for ing in ingredients:
        source = _supply_for(supply["byProductId"], ing.get("productId"))
        if source != "PLX":
            if not _finite(ing.get("percentage")):
                raise LandedCostError("customer_raw_inclusion_must_be_a_number")
            prior = customer_raws.get(ing["productId"])
            customer_raws[ing["productId"]] = {
                "name": _nc(prior["name"] if prior else None, ing["product"]["name"]),
                "supplySource": source,
                "inclusionPct": (prior["inclusionPct"] if prior else 0) + ing["percentage"]}
        raw.append({"name": ing["product"]["name"], "inclusionPct": ing.get("percentage"),
                    "costPerKg": quote_raw_cost_per_kg(ing["product"], source)})
    for product_id, entry in customer_raws.items():
        customer_supplied.append({
            "kind": "raw", "key": product_id, "name": entry["name"], "productId": product_id,
            "supplySource": entry["supplySource"],
            "qtyPerUnit": _round_qty((mass * entry["inclusionPct"]) / 100) if mass > 0 else None,
            "unit": "kg"})
        supply_basis[("raw", product_id)] = supply["records"]["product:" + _id_text(product_id)]
    counts = {"unsourced": 0, "linked": 0, "hs": 0, "incoterms": 0, "origin": 0,
              "customer": 0, "pending": 0}
    component_lines: list[dict[str, Any]] = []
    packaging: list[dict[str, Any]] = []
    for line in lines_in:
        product = line.get("product") or None
        name = _nc((product or {}).get("name"), line.get("packagingType"), line.get("id"))
        qty = line.get("quantity") if _truthy(line.get("quantity")) else 1
        linked = _truthy(line.get("productId")) and not _truthy(line.get("needsSourcing")) and \
            line.get("estimatedUnitCost") is None
        key = _nc(line.get("id"), name)
        source = _supply_for(supply["byBomLineId"], line.get("id"))
        if source != "PLX":
            # G9 (G-D4, G-D5): the customer sends it: $0 with no card, counted apart from
            # linked and unsourced lines, no HS, incoterms or origin check.
            counts["customer"] += 1
            counts["pending"] += source == "CUSTOMER_PENDING"
            customer_supplied.append({"kind": "packaging", "key": key, "name": name,
                                      "productId": _nc(line.get("productId")),
                                      "supplySource": source, "qtyPerUnit": qty, "unit": "each"})
            supply_basis[("packaging", key)] = supply["records"]["line:" + _id_text(line["id"])]
            component_lines.append({"lineKey": key, "lineName": name,
                                    "productId": _nc(line.get("productId")), "qtyPerUnit": qty,
                                    "linked": linked, "pricedUnitCost": 0, "card": None,
                                    "supplySource": source})
            packaging.append({"name": name, "qtyPerUnit": qty, "unitCost": 0})
            continue
        if not linked:
            counts["unsourced"] += 1
            unit = _nc(line.get("estimatedUnitCost"), line.get("cost"), 0)
            component_lines.append({"lineKey": key, "lineName": name,
                                    "productId": _nc(line.get("productId")), "qtyPerUnit": qty,
                                    "linked": False, "pricedUnitCost": unit, "card": None,
                                    "supplySource": source})
            packaging.append({"name": name, "qtyPerUnit": qty, "unitCost": unit})
            continue
        counts["linked"] += 1
        cards = _active_cards(product)
        card = cards[0] if cards else None
        if product and not _truthy(product.get("hsCode")):
            counts["hs"] += 1
        if card and not _truthy(card.get("incoterms")) and \
                not _truthy((card.get("supplier") or {}).get("incoterms")):
            counts["incoterms"] += 1
        if card and not _truthy(card.get("countryOfOrigin")):
            counts["origin"] += 1
        landed = landed_component_cost(_nc(card.get("unitCost") if card else None,
                                           line.get("cost"), 0),
                                       _nc(card.get("deliveryCost") if card else None, 0))
        component_lines.append({
            "lineKey": key, "lineName": name, "productId": _nc(line.get("productId")),
            "qtyPerUnit": qty, "linked": True, "pricedUnitCost": landed["landedUnitCost"],
            "supplySource": source,
            "card": {"unitCost": card.get("unitCost"), "deliveryCost": _nc(card.get("deliveryCost")),
                     "updatedAt": _nc(card.get("updatedAt")), "tiers": _tiers(card)}
            if card else None,
            "landed": landed})
        packaging.append({"name": name, "qtyPerUnit": qty, "unitCost": landed["landedUnitCost"]})
    target = (fg or {}).get("targetQty")
    run = facts.get("quote_run") or {}
    run_qty = run.get("quantity")
    return {
        "project": project, "formulation": formulation, "formulationPin": resolved["pin"],
        "bom": bom, "bomLink": selected["link"],
        "unread": [name for name in QUOTE_RELATIONS if facts.get(name) is None],
        "rates": facts.get("rate_settings") or None,
        "resolutionSource": resolved["source"],
        "coreTargetPrice": core.get("target_price"),
        "specificGravity": specific_gravity, "fillWeightG": fill_weight_g,
        # G9: a customer-supplied line costs PLX nothing.
        "bomTotal": rollup_bom_total([
            {"quantity": line.get("quantity"), "cost": 0, "estimatedUnitCost": None}
            if _supply_for(supply["byBomLineId"], line.get("id")) != "PLX" else
            {"quantity": line.get("quantity"), "cost": line.get("cost"),
             "estimatedUnitCost": line.get("estimatedUnitCost")} for line in lines_in]),
        "rawMaterials": raw, "packaging": packaging, "componentLines": component_lines,
        "fgTargetQty": target if target is not None and _finite(target) and target > 0 else None,
        "runQty": run_qty if run_qty is not None and _finite(run_qty) and run_qty > 0 else None,
        "unsourcedLineCount": counts["unsourced"], "linkedLineCount": counts["linked"],
        "missingHsCodeCount": counts["hs"], "missingIncotermsCount": counts["incoterms"],
        "missingOriginCount": counts["origin"],
        "customerSuppliedLineCount": counts["customer"],
        "customerPendingLineCount": counts["pending"],
        "customerSupplied": customer_supplied, "supplyBasis": supply_basis}


def line_cost_evidence(product_ids: Sequence[Any], po_lines: Sequence[Mapping[str, Any]],
                       inventory: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``loadLineCostEvidence`` over supplied rows: the newest RECEIVED PO line per product
    (``receivedDate desc, updatedAt desc``) with header freight allocated by received value, and
    the weighted-average stock cost across the product's inventory rows."""
    ids = list(dict.fromkeys(i for i in product_ids if _truthy(i)))
    last_po: dict[str, Any] = {}
    stock: dict[str, Any] = {}
    if not ids:
        return {"lastPo": last_po, "stock": stock}
    received = [line for line in po_lines if line.get("productId") in ids
                and _is_num(line.get("quantityReceived")) and line["quantityReceived"] > 0
                and (line.get("purchaseOrder") or {}).get("receivedDate") is not None]
    received.sort(key=lambda line: _iso(line.get("updatedAt")) or "", reverse=True)
    received.sort(key=lambda line: _iso(line["purchaseOrder"]["receivedDate"]) or "",
                  reverse=True)
    newest: dict[str, Mapping[str, Any]] = {}
    for line in received:
        newest.setdefault(line["productId"], line)
    for product_id, line in newest.items():
        siblings = [{"id": x.get("id"), "unitPrice": x.get("unitPrice"),
                     "quantityReceived": x.get("quantityReceived")}
                    for x in po_lines if x.get("purchaseOrderId") == line.get("purchaseOrderId")]
        alloc = allocate_po_freight_per_unit(line["purchaseOrder"].get("shippingCost"),
                                             siblings or [line], line.get("id"))
        last_po[product_id] = {
            "source": "last_po_landed_approx", "poNumber": line["purchaseOrder"].get("poNumber"),
            "unitPrice": line.get("unitPrice"), "freightPerUnit": alloc["freightPerUnit"],
            "landedApproxUnitCost": _round(line["unitPrice"] + alloc["freightPerUnit"], 1e4)
            if alloc["freightPerUnit"] is not None else None,
            "quantityReceived": line["quantityReceived"], "freightReason": alloc["reason"],
            "asOf": _iso(line["purchaseOrder"]["receivedDate"]),
            "_source": [line.get("_source"), line["purchaseOrder"].get("_source")]}
    rows_by: dict[str, list[Mapping[str, Any]]] = {}
    for row in inventory:
        if row.get("productId") in ids and _truthy(row.get("productId")):
            rows_by.setdefault(row["productId"], []).append(row)
    for product_id, rows in rows_by.items():
        total_qty = costed_qty = costed_value = 0.0
        newest_ms: int | None = None
        for row in rows:
            qty = max(0.0, _nc(_to_num(row.get("currentQuantity")), 0.0))
            total_qty = total_qty + qty
            cost = _to_num(row.get("unitCost"))
            if cost is not None and qty > 0:
                costed_qty = costed_qty + qty
                costed_value = costed_value + qty * cost
            ms = _epoch_ms(_iso(_nc(row.get("lastTransactionAt"), row.get("updatedAt"))))
            if ms is not None and (newest_ms is None or ms > newest_ms):
                newest_ms = ms
        if costed_qty > 0:
            stock[product_id] = {"evidence": {
                "source": "stock_weighted_avg", "unitCost": _round(costed_value / costed_qty, 1e4),
                "quantityOnHand": total_qty,
                "asOf": _iso(_EPOCH + timedelta(milliseconds=newest_ms))
                if newest_ms is not None else None}, "missingReason": None,
                "_source": [row.get("_source") for row in rows]}
        else:
            stock[product_id] = {"evidence": None, "missingReason": "stock-cost-missing",
                                 "_source": [row.get("_source") for row in rows]}
    return {"lastPo": last_po, "stock": stock}


def build_line_cost_provenance(component_lines: Sequence[Mapping[str, Any]], qty_basis: Any,
                               evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``buildLineCostProvenance`` (evidence reads in the Workbench never fail silently: a
    caller with a failed read passes no rows and the Workbench layer names it)."""
    run_tier = pick_run_volume_tier(qty_basis)
    out = []
    for line in component_lines:
        if line.get("supplySource", "PLX") != "PLX":
            # G9: no card, PO or stock cost applies to what the customer sends.
            out.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                        "productId": line["productId"], "qtyPerUnit": line["qtyPerUnit"],
                        "pricedUnitCost": line["pricedUnitCost"], "cardTier": None,
                        "lastPo": None, "stock": None,
                        "missing": ["card:customer-supplied", "last_po:customer-supplied",
                                    "stock:customer-supplied"], "evidenceDbError": False})
            continue
        missing: list[str] = []
        card_tier = None
        if line["card"]:
            card_tier = pick_supplier_card_tier_price(
                line["card"], qty_basis * line["qtyPerUnit"]
                if qty_basis is not None and qty_basis > 0 else None, run_tier)
        else:
            missing.append("card:no-supplier-card" if line["linked"] else "card:line-unsourced")
        last_po = stock = None
        if not _truthy(line["productId"]):
            missing += ["last_po:no-product-link", "stock:no-product-link"]
        else:
            last_po = evidence["lastPo"].get(line["productId"])
            if not last_po:
                missing.append("last_po:none-received")
            entry = evidence["stock"].get(line["productId"])
            if not entry:
                missing.append("stock:no-inventory-row")
            elif entry["evidence"]:
                stock = entry["evidence"]
            else:
                missing.append(f"stock:{entry['missingReason'] or 'cost-missing'}")
        out.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                    "productId": line["productId"], "qtyPerUnit": line["qtyPerUnit"],
                    "pricedUnitCost": line["pricedUnitCost"], "cardTier": card_tier,
                    "lastPo": {k: v for k, v in last_po.items() if k != "_source"}
                    if last_po else None,
                    "stock": stock, "missing": missing, "evidenceDbError": False})
    return out


def _rates(rates: Mapping[str, Any] | None, key: str, default: float) -> Any:
    value = (rates or {}).get(key)
    return value if _is_num(value) else default


def _estimate(q: Mapping[str, Any], mass: float, packaging: Sequence[Mapping[str, Any]],
              asm_hours: Any, cmp_hours: Any, assembly_rate: Any, overhead: Mapping[str, Any],
              overhead_rate: Any, margin_pct: Any) -> dict[str, Any]:
    rates = q["rates"]
    return estimate_unit_cost({
        "fillWeightG": mass * 1000, "rawMaterials": q["rawMaterials"], "packaging": packaging,
        "rawMaterialsAdjustmentPct": _rates(rates, "rawMaterialsAdjustmentRate",
                                            RATE_DEFAULTS["rawMaterialsAdjustmentPct"]),
        "assemblyHoursPerUnit": _nc(asm_hours, 0), "compoundingHoursPerUnit": _nc(cmp_hours, 0),
        "assemblyRate": assembly_rate,
        "batchingRate": _rates(rates, "batchingQuotingLaborRate", RATE_DEFAULTS["batchingRate"]),
        "overheadRate": overhead_rate, "overheadHoursPerUnit": _nc(overhead["hoursPerUnit"], 0),
        "marginPct": margin_pct})


def _overhead(rates: Any, hours: Any, source: Any, override: Mapping[str, Any] | None) -> dict:
    override = override or {}
    return derive_uptime_overhead(overhead_rate_from_settings(rates), hours, source,
                                  override.get("expectedUnitsPerShift"),
                                  override.get("uptimeHours"))


def _asked(spec: Any) -> dict[str, Any] | None:
    """``normalizeAskedVolume``: a number, or an object's ``askedUnits``; finite and > 0."""
    obj = {"askedUnits": spec} if _is_num(spec) else {
        "askedUnits": spec.get("askedUnits") if isinstance(spec, Mapping) else None}
    if not _finite(obj["askedUnits"]) or obj["askedUnits"] <= 0:
        return None
    return obj


def _vessel_charges(plan: Any) -> int | None:
    """The charge count of a cited vessel plan (a positive whole number), None without one."""
    if plan is None:
        return None
    charges = plan.get("charges") if isinstance(plan, Mapping) else None
    if not (_is_num(charges) and charges >= 1 and float(charges).is_integer()):
        raise LandedCostError("vessel_plan_charges_must_be_a_positive_whole_number")
    return int(charges)


def portal_quote_landed(facts: Mapping[str, Any], *,
                        asked_volumes: Sequence[Any] | None = None) -> dict[str, Any]:
    """``quoteProjectLanded`` (persist=false) over the records it reads (``quote_facts``, plus
    ``fg_routings``, ``draft_cost_overrides``, ``setup_standards`` and the quote run quantity):
    the single estimate, the census, the per-line provenance and one matrix column per asked
    volume, as the Portal computes them, hours included. ``vessel_plans``
    (``{String(askedUnits): {"charges", "_source"}}``) is the one input not derived here: the
    compounding standard and vessel derivation are not ported; without a plan the Portal's own
    no-pick path (one charge) applies."""
    q = quote_facts(facts)
    rates = q["rates"]
    key = q["project"].get("projectKey")
    mass = fill_mass_kg(q["fillWeightG"], q["specificGravity"])
    assembly_rate = _rates(rates, "assemblyQuotingLaborRate", RATE_DEFAULTS["assemblyRate"])
    setup_rows = facts.get("setup_standards")
    routing, routing_fill = primary_routing(facts.get("fg_routings"), key, setup_rows)
    drafts = draft_cost_rows(facts.get("draft_cost_overrides"), key)
    setup_standard = resolve_setup_standard(setup_rows if setup_rows is not None
                                            else [WORKBOOK_SETUP_SEED])
    labour = resolve_quote_hours(key, drafts, routing, q["runQty"], q["fgTargetQty"],
                                 assembly_rate)["labour"]
    placeholder = labour["source"] == "missing"
    ladder = resolve_margin_ladder(rates)
    overhead = _overhead(rates, labour["assemblyHoursPerUnit"], labour["assemblySource"],
                         resolve_expected_units_override(drafts, key, labour["pricedTierCode"]))
    estimate = _estimate(q, mass, q["packaging"], labour["assemblyHoursPerUnit"],
                         labour["compoundingHoursPerUnit"], assembly_rate, overhead,
                         overhead["hourlyRate"], ladder["targetPct"])
    qty_basis = _nc(q["runQty"], q["fgTargetQty"])
    # G9: no purchasing or stock evidence for customer-supplied lines.
    evidence = line_cost_evidence([line["productId"] for line in q["componentLines"]
                                   if line["supplySource"] == "PLX"],
                                  facts.get("purchase_order_lines") or [],
                                  facts.get("inventory") or [])
    provenance = build_line_cost_provenance(q["componentLines"], qty_basis, evidence)
    provenance_lines = [{"lineName": line["lineName"],
                         "cardUnitCost": (prov["cardTier"] or {}).get("unitCost"),
                         "cardUpdatedAt": _iso((line["card"] or {}).get("updatedAt")),
                         "lastPoUnitPrice": (prov["lastPo"] or {}).get("unitPrice"),
                         "lastPoReceivedAt": (prov["lastPo"] or {}).get("asOf")}
                        for line, prov in zip(q["componentLines"], provenance)]
    stock_lines = [{"lineName": line["lineName"], "qtyPerUnit": line["qtyPerUnit"],
                    "pricedUnitCost": line["pricedUnitCost"],
                    "stockUnitCost": (prov["stock"] or {}).get("unitCost"),
                    "stockQuantityOnHand": (prov["stock"] or {}).get("quantityOnHand"),
                    "requiredQty": qty_basis * line["qtyPerUnit"]
                    if qty_basis is not None and qty_basis > 0 else None}
                   for line, prov in zip(q["componentLines"], provenance)]
    specs = [s for s in map(_asked, asked_volumes if asked_volumes is not None
                            else quote_suggestion_volumes(rates)) if s is not None]
    specs.sort(key=lambda s: s["askedUnits"])
    plans = facts.get("vessel_plans") or {}
    if not isinstance(plans, Mapping):
        raise LandedCostError("vessel_plans_must_map_asked_units_to_a_plan")
    columns = []
    for spec in specs:
        units = spec["askedUnits"]
        charges = _vessel_charges(plans.get(js_string(units)))
        hours = volume_column_labour(units, key, drafts, routing, setup_standard, charges,
                                     assembly_rate, rates)
        col_labour, col_overhead = hours["labour"], hours["overhead"]
        comp = []
        for line in q["componentLines"]:
            demand = units * line["qtyPerUnit"]
            tier_code = pick_run_volume_tier(demand)
            if line["supplySource"] != "PLX":
                # G9 (G-D4): $0 at every volume, whatever card PLX may hold.
                comp.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                             "qtyPerUnit": line["qtyPerUnit"], "componentDemand": demand,
                             "runVolumeTierCode": tier_code, "unitCost": 0,
                             "unitCostSource": "priced_basis", "supplySource": line["supplySource"],
                             "tierMinQty": None, "landed": None})
            elif line["card"]:
                tier = pick_supplier_card_tier_price(line["card"], demand, tier_code)
                landed = landed_component_cost(tier["unitCost"], _nc(line["card"]["deliveryCost"], 0))
                comp.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                             "qtyPerUnit": line["qtyPerUnit"], "componentDemand": demand,
                             "runVolumeTierCode": tier_code, "unitCost": landed["landedUnitCost"],
                             "unitCostSource": "card_tier_landed", "supplySource": "PLX",
                             "tierMinQty": tier["tierMinQty"], "landed": landed})
            else:
                comp.append({"lineKey": line["lineKey"], "lineName": line["lineName"],
                             "qtyPerUnit": line["qtyPerUnit"], "componentDemand": demand,
                             "runVolumeTierCode": tier_code, "unitCost": line["pricedUnitCost"],
                             "unitCostSource": "priced_basis", "supplySource": "PLX",
                             "tierMinQty": None, "landed": None})
        col_estimate = _estimate(q, mass, [{"name": c["lineName"], "qtyPerUnit": c["qtyPerUnit"],
                                            "unitCost": c["unitCost"]} for c in comp],
                                 col_labour["assemblyHoursPerUnit"],
                                 col_labour["compoundingHoursPerUnit"], assembly_rate,
                                 col_overhead, col_overhead["hourlyRate"], ladder["targetPct"])
        columns.append({"askedUnits": units, "labour": col_labour, "vesselCharges": charges,
                        "componentLines": comp,
                        "lines": col_estimate["lines"], "estimate": col_estimate,
                        "unitCost": col_estimate["totalCostPerUnit"],
                        "marginTiers": price_margin_tiers(col_estimate["totalCostPerUnit"], ladder),
                        "overhead": col_overhead})
        if q["customerSupplied"]:
            columns[-1]["customerSupplied"] = customer_supplied_requirements(
                q["customerSupplied"], units)
    census = derive_ready_to_quote({
        "fillWeightG": q["fillWeightG"], "specificGravity": q["specificGravity"],
        "specificGravityConfirmed": q["specificGravity"] is not None and q["specificGravity"] > 0,
        "unsourcedLineCount": q["unsourcedLineCount"], "linkedLineCount": q["linkedLineCount"],
        "missingHsCodeCount": q["missingHsCodeCount"],
        "missingIncotermsCount": q["missingIncotermsCount"],
        "missingOriginCount": q["missingOriginCount"],
        "customerSuppliedLineCount": q["customerSuppliedLineCount"],
        "customerPendingLineCount": q["customerPendingLineCount"],
        "customerSuppliedRawCount": sum(m["kind"] == "raw" for m in q["customerSupplied"]),
        "labourIsPlaceholder": placeholder,
        "sellPricePerUnit": estimate["sellPricePerUnit"],
        "retailPerUnit": parse_money(q["coreTargetPrice"]),
        "provenanceLines": provenance_lines, "stockFillLines": stock_lines,
        "pricedTotalCostPerUnit": estimate["totalCostPerUnit"]})
    result = {"estimate": estimate, "fillWeightG": q["fillWeightG"],
              "specificGravity": q["specificGravity"], "fillMassKg": mass,
              "bomTotal": q["bomTotal"], "resolutionSource": q["resolutionSource"],
              "labourIsPlaceholder": placeholder, "labour": labour, "marginLadder": ladder,
              "marginTiers": price_margin_tiers(estimate["totalCostPerUnit"], ladder),
              "overhead": overhead, "census": census, "lineProvenance": provenance,
              "volumeMatrix": columns, "_facts": q, "_evidence": evidence,
              "_labour": {"routing": routing, "routing_fill": routing_fill, "drafts": drafts,
                          "setup_standard": setup_standard}}
    if q["customerSupplied"]:
        # G9: what the customer must send at the priced qty basis (run qty, else FG target).
        result["customerSupplied"] = customer_supplied_requirements(q["customerSupplied"],
                                                                    qty_basis)
    return result


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
        for i, ing in enumerate(formulation.get("ingredients") or []):
            product = ing.get("product") or {}
            subject = product.get("name") or f"ingredient {i}"
            cite(product, f"formulation.ingredients[{i}].product")
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


def po_line_landed(po: Mapping[str, Any], lines: Sequence[Mapping[str, Any]], line_id: Any,
                   *, tariff: Mapping[str, Any] | None = None,
                   receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
    allocation). The Portal's own readings stay in ``portal_allocation`` / ``posted_if_received``
    for comparison.
    """
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
    if shipping is None or not math.isfinite(shipping) or shipping <= 0:
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
    elif rate is None or not rate.is_finite():
        duty_input = {"unknown": "TARIFF_TREATMENT_UNVERIFIED: no cited HS classification and "
                                 "duty rate for this item"
                                 + (f" (the header duty {_dec_str(portal_decimal(duty))} is an "
                                    "estimate, not a classification)"
                                    if duty is not None and math.isfinite(duty) and duty > 0
                                    else "; the header records no duty"),
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
    unread = None if receipt is not None else {
        "unknown": "RECEIPT_ACTUAL_NOT_READ: the Portal posts receipt freight and duty "
                   "actuals over the PO estimates (resolvePostedLandedAmounts); no "
                   "verified receipt of this purchase order is read",
        "who_can_answer": "WH"}
    return {"portal_allocation": portal, "freight": freight, "duty": duty_input,
            "receipt_actual": unread,
            "posted_if_received": posted,
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


# ---- plx.investigate engine (evidence kind "compute" of an ontology/plans plan) ------------------
#
# The landed cost of a purchase order from the cited values a plan collected: one
# ``compute_landed_cost`` record per line (goods = the line's cited unit price per its cited
# unit, freight and duty from ``po_line_landed``), and the purchase order's total only when every
# line's record is computed. Nothing is read here but the plan's answer entries and the citations
# they carry (re-read for the investigating actor by ``resolver``); what the ontology does not
# project stays a typed gap naming what was searched. Roles name plan evidence items:
#   currency          the purchase order's currency (every record of it; two values = mixed)
#   unit_price        line unit price (its entry's unit is the price denominator, currency its own)
#   quantity          line ordered quantity with its unit
#   received          line received quantity (the Portal allocates freight by received value)
#   freight_estimate  header shipping cost estimate; duty_estimate: header duty estimate
#   classification    the line item's HS code (reported; no duty rate is projected)
#   receipt_freight / receipt_duty: receipt actuals (resolvePostedLandedAmounts)
#   units_per_box     the line item's units per case (packaging; case dimensions are not served)
#   quote_*           the typed fields of forwarder quotes naming the purchase order (amount: a
#                     stated total; rate: a price per basis unit; valid_until, basis, the
#                     counts basis_count, volume, chargeable weight and pallet count, each
#                     with its unit, includes, excludes, scope), each quote assessed by
#                     ``assess_freight_quote``: a current quote, never a posted actual
# ``item_link`` is the declared link from a line to the item carrying the classification;
# ``line_link`` the declared link from a line to its purchase order (the complete line set);
# ``quote_link`` the declared link from a quote to the purchase order.
#
# A value a person corrected (``plx.review`` ``correct_value``, handed in by ``plx.investigate``
# as the person's value: this engine takes reviewed inputs) is computed from, never re-read
# from its source: its input is derived from the value it corrects and every record the
# correction cites, each resolved for the actor (``correction_sources``, else unknown), and a
# line whose freight share moves with a sibling line's corrected weight names it as an input.
#
# A line sent unpriced ($0.000, a purchase order issued before the vendor priced it) takes its
# goods from the supplier confirmation (proforma invoice) naming the purchase order
# (``confirmation_link``, a lead): its cited amount, when that confirmation is the line's alone
# (a one-line purchase order whose cited quantity it confirms, or a single-item confirmation,
# unit price x quantity = amount, whose quantity only that line orders). Confirmations whose
# amounts, unit prices or quantities differ are versions none of which is chosen (PI_VERSION_UNRESOLVED, a
# conflict naming every one); a linked confirmation with no readable amount blocks the goods
# too (PI_AMOUNT_UNREAD, a gap naming it), so another one's amount is never taken. Units per
# case are read from the item and its other records (``item_twin_link``); two
# that differ are a UNITS_PER_CASE_CONFLICT, never one picked.
#
# Every freight figure is labelled with its basis: a receipt's historical posted actual, the
# purchase order header estimate (ERP-D-015), or a forwarder's current quote. A header estimate
# within ``ESTIMATE_QUOTE_TOLERANCE`` of a cited quote that is not total freight (same currency)
# is not allocated as freight.

QUOTE_ROLES = ("quote_amount", "quote_rate", "quote_valid_until", "quote_basis",
               "quote_basis_count", "quote_volume", "quote_weight", "quote_pallets",
               "quote_includes", "quote_excludes", "quote_scope")
CONTAINER_UNITS = frozenset(("container", "containers", "cntr", "cntrs"))
# The unit words a count of each basis is cited in; a count in any other unit is not one.
_BASIS_COUNT_UNITS = {"CBM": frozenset(("cbm", "m3")), "KG": frozenset(("kg",)),
                      "PALLET": frozenset(("pallet", "pallets", "plt", "plts"))}
# Bases counted in whole units, a single one of which is a whole unit-shipment.
_WHOLE_BASES = ("PALLET",)
# The roles a quote's count is read from, each a cited value with its unit.
_COUNT_ROLES = ("quote_basis_count", "quote_volume", "quote_weight", "quote_pallets")
# A header shipping estimate this close to the amount of a quote that is not total freight
# (a share of that amount, same currency) is taken to be that quote's figure.
ESTIMATE_QUOTE_TOLERANCE = Decimal("0.005")
CONFIRMATION_ROLES = ("confirmation_amount", "confirmation_unit_price", "confirmation_quantity")
PLAN_ROLES = ("currency", "unit_price", "quantity", "received", "freight_estimate",
              "duty_estimate", "classification", "receipt_freight", "receipt_duty",
              "units_per_box") + QUOTE_ROLES + CONFIRMATION_ROLES
_OPTIONAL_ROLES = frozenset(("classification", "receipt_freight", "receipt_duty",
                             "units_per_box") + QUOTE_ROLES + CONFIRMATION_ROLES)
_LINE_ROLES = ("unit_price", "quantity", "received")
MAX_STEP_CHARS = 400
# The roles whose sources a cost input rests on (named in its gap as what was searched).
_COST_ROLES = {"goods": ("unit_price", "confirmation_amount"), "freight": ("freight_estimate", "receipt_freight"),
               "duty": ("duty_estimate", "classification", "receipt_duty"),
               "receipt_actual": ("receipt_freight", "receipt_duty")}
_QUOTE_SEARCHED = ("quote_amount", "quote_rate", "quote_basis", "quote_basis_count",
                   "quote_includes", "quote_excludes", "quote_valid_until")
# A gap about a quote's count names every role a count is read from (a gap lists 8 sources).
_QUOTE_COUNT_SEARCHED = ("quote_amount", "quote_rate", "quote_basis", *_COUNT_ROLES)
_QUOTE_COUNT_CODES = frozenset(("BASIS_NOT_TOTAL", "FREIGHT_RATE_COUNT_CONFLICT",
                                "FREIGHT_BASIS_TERMS_UNREAD",
                                "FREIGHT_BASIS_COUNTS_CONFLICT", "FREIGHT_BASIS_COUNT_UNKNOWN",
                                "PACK_SPEC_NOT_SERVED"))
# Typed unknowns and the gap state each is reported with (the rest: unknown).
_GAP_STATES = {"DUTY_RATE_SOURCE_ABSENT": "not_ingested",
               "MIXED_CURRENCY_NO_CITED_RATE": "not_ingested",
               "RECEIPT_ACTUAL_CURRENCY_UNRECORDED": "not_ingested",
               "PO_FREIGHT_NOT_RECORDED": "absent_in_source",
               "PO_DUTY_NOT_RECORDED": "absent_in_source",
               "CURRENCY_UNCITED": "absent_in_source",
               "UNIT_PRICE_DENOMINATOR_UNKNOWN": "absent_in_source",
               "LINE_QUANTITY_UNKNOWN": "absent_in_source",
               **{code: state for code, (state, _, _) in _QUOTE_GAPS.items()}}
FREIGHT_LABELS = {"posted": "freight basis: historical posted actual (the receipt's freight, "
                            "which the Portal posts over the estimate)",
                  "estimate": "freight basis: purchase order header estimate (ERP-D-015), not a "
                              "posted actual and not a quote",
                  "quote": "freight basis: current quote (a forwarder's offer read from a "
                           "captured document, a lead), not a posted actual and not the purchase "
                           "order's estimate",
                  "none": "freight basis: none usable (see the freight gap)"}

# ---- estimate mode (before receipt; ERP-D-015: PO header = estimate, receipt = actual) -------
#
# An open purchase order's landed cost is an ESTIMATE, kept apart from the actual landed cost
# (``<name>_line_<i>``): its own entries ``<name>_estimate`` (the purchase order),
# ``<name>_estimate_subtotal`` (goods + freight, every amount cited) and
# ``<name>_estimate_line_<i>`` (per unit of the ordered quantity). Each lists every assumption
# it rests on in its steps and ends with ESTIMATE_LABEL. Freight is the header shipping
# estimate (else one usable forwarder quote total in the purchase order's currency) pro-rated
# over the ORDERED line value; duty is the header duty estimate pro-rated the same way, else a
# person's recorded duty-rate assumption (``duty_assumption``) times the goods, else the Portal
# default tariff rate applied as a labelled assumption beside a gap naming the missing input
# and who can give it, with the range from no duty to that default.
ESTIMATE_LABEL = ("ESTIMATE before receipt (ERP-D-015: the purchase order header is an "
                  "estimate, the receipt the actual); not the actual landed cost, which is "
                  "stated only after receipt")
ESTIMATE_BASIS = ("pro-rated by ordered line value (unit price x ordered quantity); the "
                  "Portal's allocatePoFreightPerUnit splits by received line value, which "
                  "does not exist before receipt")
ASSUMPTION_KINDS = ("duty_rate",)
_ASSUMPTION_KEYS = frozenset(("kind", "rate", "source", "recorded_by", "recorded_at",
                              "author_kind", "case_id", "record_id", "subject"))
_ASSUMPTION_TEXT = re.compile(r"[^\x20-\x7e -ɏ]")


def duty_assumption(record: Mapping[str, Any]) -> dict[str, Any]:
    """A person's recorded duty-rate assumption, validated (``LandedCostError`` otherwise):
    ``{kind: "duty_rate", rate: "<decimal string> in [0, 1]", source, recorded_by,
    recorded_at: ISO date-time, author_kind: "human", case_id, record_id, subject}``. Nothing
    unknown is accepted: an extra key, an agent's record, a float rate, a rate outside [0, 1]
    or a blank field fails closed. Returns the normalised record (texts trimmed, the source
    cut to 120 printable characters: it is a person's note, shown, never followed)."""
    if not isinstance(record, Mapping):
        raise LandedCostError("assumption_must_be_mapping")
    extra = set(record) - _ASSUMPTION_KEYS
    if extra:
        raise LandedCostError("assumption_unknown_keys:" + ",".join(sorted(map(str, extra)))[:200])
    if record.get("kind") not in ASSUMPTION_KINDS:
        raise LandedCostError("assumption_kind_unknown")
    if record.get("author_kind") != "human":
        raise LandedCostError("assumption_not_recorded_by_a_person")
    out: dict[str, Any] = {"kind": record["kind"], "author_kind": "human"}
    for key in ("source", "recorded_by", "case_id", "record_id", "subject"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LandedCostError(f"assumption_needs_{key}")
        out[key] = _ASSUMPTION_TEXT.sub(" ", value.strip())[:120 if key == "source" else 200]
    rate = record.get("rate")
    if not isinstance(rate, str):
        raise LandedCostError("assumption_rate_must_be_decimal_string")
    try:
        number = Decimal(rate.strip())
    except ArithmeticError as exc:
        raise LandedCostError("assumption_rate_must_be_decimal_string") from exc
    if not number.is_finite() or not Decimal(0) <= number <= Decimal(1):
        raise LandedCostError("assumption_rate_out_of_range")
    out["rate"] = _dec_str(number)
    when = record.get("recorded_at")
    try:
        if not isinstance(when, str) or "T" not in when:
            raise ValueError(when)
        datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LandedCostError("assumption_recorded_at_must_be_iso_datetime") from exc
    out["recorded_at"] = when.strip()
    return out


def _assumption_text(record: Mapping[str, Any]) -> str:
    return (f"duty rate {record['rate']} (a person's assumption, recorded by "
            f"{record['recorded_by']} at {record['recorded_at']} on case {record['case_id']}, "
            f"record {record['record_id']}; source stated: {record['source']})")


def prorate(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """``total`` split over ``weights`` (each > 0) in proportion, at the total's precision or
    cents, whichever is finer: largest remainder, ties to the earlier weight, so the shares
    sum to ``total`` exactly and no share is lost or duplicated."""
    if not weights or any(w <= 0 for w in weights) or total < 0:
        raise LandedCostError("prorate_needs_positive_weights")
    exponent = min(total.as_tuple().exponent, -2) if total.is_finite() else -2
    quantum = Decimal(1).scaleb(exponent)
    whole = sum(weights)
    exact = [total * w / whole for w in weights]
    shares = [x.quantize(quantum, rounding="ROUND_DOWN") for x in exact]
    left = int(((total - sum(shares)) / quantum).to_integral_value())
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in order[:left]:
        shares[i] += quantum
    return shares


def _pre_receipt_estimate(*, name: str, target: str | None, line_ids: Sequence[str],
              every_line: Sequence[str], prices: Mapping[str, Mapping[str, Any]],
              quantities: Mapping[str, Mapping[str, Any]], linked: bool, complete: bool,
              covered: bool, freight_est: Mapping[str, Any] | None,
              duty_est: Mapping[str, Any] | None, partial_quote: bool,
              quotes: Sequence[tuple[Decimal, str, Sequence[Mapping[str, Any]]]],
              hs_of: Mapping[str, Mapping[str, Any] | None],
              assumptions: Sequence[Mapping[str, Any]], header_ids: set[str], as_of: str,
              resolver: Resolver | None, actor: Any,
              unknown: Callable[..., None], currency_step: str | None = None
              ) -> list[dict[str, Any]]:
    """The estimate entries of an open purchase order (the block above ``duty_assumption``).

    Amounts are stated only over a line set proven whole (a complete run, a reader whose scope
    covers the lines, every line read with a unit price and an ordered quantity) whose every
    line value (positive unit price per the unit of its ordered quantity, in the purchase
    order's currency) is cited, with a freight source; each line is computed by
    ``compute_landed_cost``, which re-reads every cited input for the actor. Otherwise each
    entry is null and a gap names the missing input and who can give it. Gaps and steps name
    codes, entries, rates and amounts the reader read, never source text."""
    def handle(entry: Mapping[str, Any] | None) -> str | None:
        return entry["citation"]["l1_handle"] if entry and entry.get("citation") else None

    labels = {oid: f"{name}_estimate_line_{i}"[:64] for i, oid in enumerate(line_ids, start=1)}
    lines = [{"name": labels[oid], "kind": "computed", "value": None,
              "inputs": [], "steps": [], "basis": [oid], "check": "not_applicable"}
             for oid in line_ids]
    subtotal: dict[str, Any] = {"name": f"{name}_estimate_subtotal"[:64], "kind": "computed",
                                "value": None, "inputs": [], "steps": [],
                                "check": "not_applicable"}
    total: dict[str, Any] = {"name": f"{name}_estimate"[:64], "kind": "computed", "value": None,
                             "inputs": [e["name"] for e in lines][:50], "steps": [],
                             "check": "not_applicable"}
    out = [total, subtotal, *lines]

    def close(why: str) -> list[dict[str, Any]]:
        for entry in out:
            entry["steps"] = [why[:MAX_STEP_CHARS], ESTIMATE_LABEL]
        return out

    if target is None:
        return close("no estimate: no target currency (see the currency gap)")
    for entry in out:
        entry.update(unit=target, currency=target)

    # The duty-rate assumptions a person recorded on this purchase order (validated, else a gap).
    recorded: list[dict[str, Any]] = []
    for item in assumptions:
        try:
            checked = duty_assumption(item)
            if checked["subject"] not in header_ids:
                raise LandedCostError("assumption_subject_not_this_purchase_order")
        except LandedCostError as exc:
            unknown("ASSUMPTION_RECORD_INVALID", "estimate duty assumption",
                    f"a recorded assumption does not validate ({str(exc).split(':')[0][:80]}); "
                    "it is not applied", holds=False, who="FIN")
            continue
        recorded.append(checked)

    missing = [(role, [oid for oid in every_line if _entry_decimal(found.get(oid)) is None])
               for role, found in (("unit price", prices), ("ordered quantity", quantities))]
    if complete and not covered:
        # A reader limit, not a missing input: a value this reader lacks may be withheld from
        # it, so no person is named for it (a grant would answer it).
        unknown("ESTIMATE_READ_SCOPE_UNPROVEN", "estimate lines",
                "this reader's read scope (every store marking and the line source's "
                "audience) is not proven to show every line of the purchase order; the "
                "estimate pro-rates header freight over every ordered line, never a partial "
                "set", "unit_price", "quantity")
        return close("no estimate: ESTIMATE_READ_SCOPE_UNPROVEN (see the gaps)")
    if not every_line:
        # No line reached: PO_LINES_INCOMPLETE already names that cause and who can give the
        # lines; the estimate cites it rather than stating the same cause twice.
        return close("no estimate: PO_LINES_INCOMPLETE (no line of the purchase order was "
                     "reached; see the gaps)")
    if not (complete and covered and linked) or any(oids for _, oids in missing):
        why = "; ".join(f"{len(oids)} of {len(every_line)} line(s) were read with no {role}"
                        for role, oids in missing if oids) or (
            "the run was truncated or cut by its step budget" if not complete else
            "this reader's read scope is not proven to show every line of the purchase order"
            if not covered else "no line of the purchase order was reached")
        unknown("ESTIMATE_LINES_INCOMPLETE", "estimate lines",
                why + "; the estimate pro-rates header freight over every ordered line, never a "
                "partial set", "unit_price", "quantity", who="PROC")
        return close("no estimate: ESTIMATE_LINES_INCOMPLETE (see the gaps)")

    def line_value(oid: str) -> Decimal | None:
        price, qty = prices[oid], quantities[oid]
        unit = unit_token(price.get("unit"))
        value, count = _entry_decimal(price), _entry_decimal(qty)
        if unit is None or unit != unit_token(qty.get("unit")) or price.get("currency") != target \
                or value is None or count is None or value <= 0 or count <= 0:
            return None
        return value * count
    values = {oid: line_value(oid) for oid in line_ids}
    if any(v is None for v in values.values()):
        unknown("ESTIMATE_LINE_VALUE_UNCITED", "estimate lines",
                f"{sum(v is None for v in values.values())} of {len(line_ids)} line(s) state no "
                f"positive unit price per the unit of their ordered quantity in {target}; no "
                "line value to pro-rate over", "unit_price", "quantity", who="PROC")
        return close("no estimate: ESTIMATE_LINE_VALUE_UNCITED (see the gaps)")
    weights = [values[oid] for oid in line_ids]
    weight_handles = [h for oid in line_ids
                      for h in (handle(prices[oid]), handle(quantities[oid])) if h]
    goods_total = sum(weights, Decimal(0))

    # Freight: the header estimate, else one usable quote total, in the target currency.
    freight: tuple[Decimal, list[Mapping[str, Any]], str] | None = None
    header = _entry_decimal(freight_est)
    if header is not None and header > 0 and freight_est.get("currency") == target \
            and not partial_quote:
        freight = (header, [freight_est], f"purchase order header shipping estimate "
                   f"{freight_est['name']}")
    elif header is not None and header > 0 and freight_est.get("currency") != target:
        unknown("PO_ESTIMATE_CURRENCY_DIFFERS", "estimate freight",
                "the header shipping estimate carries no cited currency equal to the purchase "
                "order's; no exchange rate is projected", "freight_estimate", who="PROC")
    elif header is not None and header > 0:
        unknown("FREIGHT_ESTIMATE_IS_PARTIAL_QUOTE", "estimate freight",
                "the header shipping estimate is the amount of a cited forwarder quote that is "
                "not the total freight (see the freight quote gaps)", "freight_estimate",
                who=FORWARDER)
    if freight is None:
        totals = sorted({value for value, _, _ in quotes})
        if len(totals) == 1:
            entries = [e for value, _, found in quotes for e in found]
            freight = (totals[0], entries, "forwarder quote total " + ", ".join(
                n for _, n, _ in quotes) + " (a lead read from a captured document)")
        elif totals:
            unknown("FREIGHT_QUOTES_DIFFER", "estimate freight",
                    f"{len(totals)} usable forwarder quotes state different total freight in "
                    f"{target}; none is chosen", "freight_estimate", who="PROC")
        elif header is None or header <= 0:
            unknown("PO_FREIGHT_NOT_RECORDED", "estimate freight",
                    "the purchase order header has no shipping estimate and no usable forwarder "
                    f"quote states the total freight in {target}; the Portal would read that as "
                    "zero freight", "freight_estimate", who=FORWARDER)
    goods_step = (f"goods: {_dec_str(goods_total)} {target} over {len(line_ids)} line(s) (cited "
                  "unit price x ordered quantity)")
    if freight is None:
        subtotal["steps"] = [goods_step, "freight: no source (see the estimate freight gap); no "
                             "subtotal is stated", ESTIMATE_LABEL]
        total["steps"] = ["no estimate: no freight source (see the estimate freight gap)",
                          ESTIMATE_LABEL]
        for entry in lines:
            entry["steps"] = ["no estimate: no freight source (see the estimate freight gap)",
                              ESTIMATE_LABEL]
        return out
    freight_total, freight_entries, freight_label = freight
    freight_shares = prorate(freight_total, weights)
    freight_from = [h for h in (handle(e) for e in freight_entries) if h] + weight_handles

    # Duty: the header duty estimate, else a person's recorded rate, else the Portal default.
    duty_header = _entry_decimal(duty_est)
    rates = sorted({r["rate"] for r in recorded})
    assumption = max(recorded, key=lambda r: r["recorded_at"]) if len(rates) == 1 else None
    if len(rates) > 1:
        unknown("DUTY_ASSUMPTIONS_DISAGREE", "estimate duty assumption",
                f"{len(recorded)} recorded duty-rate assumptions state {len(rates)} different "
                "rates; none is applied", holds=True, who="FIN")
    duty_from: list[str] = list(weight_handles)
    listed: list[str] = []
    default = False
    if duty_header is not None and duty_header > 0 and duty_est.get("currency") == target:
        duty_shares = prorate(duty_header, weights)
        duty_from = [h for h in (handle(duty_est),) if h] + duty_from
        duty_rule = (f"estimate duty: purchase order header duty estimate {duty_est['name']} "
                     f"{_dec_str(duty_header)} {target} {ESTIMATE_BASIS}")
        listed.append("assumption: the header duty estimate spreads by ordered line value (one "
                      "rate for every line; the Portal has no per-line duty allocation)")
    else:
        if duty_header is not None and duty_header > 0:
            unknown("PO_ESTIMATE_CURRENCY_DIFFERS", "estimate duty",
                    "the header duty estimate carries no cited currency equal to the purchase "
                    "order's; no exchange rate is projected", "duty_estimate", who="FIN")
        if assumption is not None:
            rate = Decimal(assumption["rate"])
            duty_rule = "estimate duty: goods x " + _assumption_text(assumption)
            listed.append("assumption: " + _assumption_text(assumption))
        else:
            default = True
            rate = Decimal(str(PORTAL_QUOTE_DUTY_RATE))
            duty_rule = (f"estimate duty: goods x the Portal default tariffRate {_dec_str(rate)} "
                         f"({PORTAL_SOURCES['rate_defaults']} RATE_MASTER_DEFAULTS), an "
                         "assumption no person recorded")
            listed.append(f"assumption: duty at the Portal default tariffRate {_dec_str(rate)} "
                          f"({PORTAL_SOURCES['rate_defaults']}); no duty rate is cited or "
                          "recorded (see the estimate duty gap)")
            for oid in line_ids:
                hs = hs_of.get(oid)
                code, who = (("DUTY_RATE_SOURCE_ABSENT", "FIN") if _entry_text(hs) else
                             ("HS_CODE_NOT_CITED", "PROC"))
                unknown(code, "estimate duty",
                        ("an HS code is cited for the item, but no duty rate is cited or "
                         "recorded; Finance can record the rate (with its source) on the case"
                         if code == "DUTY_RATE_SOURCE_ABSENT" else
                         "no HS code is cited for the item and no duty rate is recorded; "
                         "Procurement can give the HS code, Finance the rate")
                        + f"; the estimate applies the Portal default tariffRate {_dec_str(rate)}"
                        " as a labelled assumption", "duty_estimate", "classification",
                        line=labels[oid], who=who)
        duty_shares = [rate * w for w in weights]
    if any(_entry_text(hs_of.get(oid)) for oid in line_ids):
        listed.append("tariff: the ontology projects no duty rate for a cited HS code")

    line_totals: list[Decimal] = []
    for oid, entry, share, duty in zip(line_ids, lines, freight_shares, duty_shares):
        price, qty = prices[oid], quantities[oid]
        unit = unit_token(qty.get("unit"))
        entry.update(unit=f"{target}/{unit}"[:20])
        # Inputs name the header entries only: every line's price and quantity entries are
        # already the actual-cost lines' inputs, and naming them on every estimate line would
        # make them shared across all answer pages (result_too_large on a long PO). The line's
        # own cited price and quantity are its basis object and its compute steps.
        header_used = [*freight_entries] + (
            [duty_est] if duty_from and duty_est and handle(duty_est) in duty_from else [])
        entry["inputs"] = list(dict.fromkeys(e["name"] for e in header_used if e))[:50]
        entry["basis"] = sorted({oid} | {e["object_id"] for e in header_used
                                         if e and e.get("object_id")})[:100]
        costs = [{"name": "goods", "category": "goods",
                  **(_corrected_input(price, f"{target}/{unit}", target) or {
                      "citation": handle(price), "field": price["citation"]["field_path"],
                      "unit": f"{target}/{unit}", "currency": target})},
                 {"name": "freight", "category": "freight", "unit": target, "currency": target,
                  "derived": {"value": _dec_str(share), "from": freight_from,
                              "rule": f"estimate freight: {freight_label} "
                                      f"{_dec_str(freight_total)} {target} {ESTIMATE_BASIS}"}},
                 {"name": "duty", "category": "duty", "unit": target, "currency": target,
                  "derived": {"value": _dec_str(duty), "from": duty_from, "rule": duty_rule}}]
        try:
            record = compute_landed_cost(costs, _corrected_input(qty, unit) or {
                "citation": handle(qty), "field": qty["citation"]["field_path"], "unit": unit},
                target, as_of=as_of, resolver=resolver, actor=actor)
        except (ValidationError, ArithmeticError) as exc:
            unknown(_code_of(str(exc)), "estimate inputs",
                    "the landed-cost engine refused the estimate inputs", line=entry["name"])
            entry["steps"] = [f"refused: {_code_of(str(exc))}", ESTIMATE_LABEL]
            continue
        for item in record["unknowns"]:
            unknown(_code_of(item["reason"]), f"estimate {item['input']}",
                    f"{item['reason']} for this reader", line=entry["name"],
                    who=item.get("who_can_answer") or "PROC")
        rules = [f"goods: {price['name']} x {qty['name']} (this line's cited inputs)",
                 f"freight share: {_dec_str(share)} {target} of the {freight_label} "
                 f"{_dec_str(freight_total)} {target}, {ESTIMATE_BASIS}",
                 f"duty: {_dec_str(duty)} {target}; {duty_rule}"]
        entry["steps"] = ([s[:MAX_STEP_CHARS] for s in record["steps"]][:30]
                          + [s[:MAX_STEP_CHARS] for s in rules + listed] + [ESTIMATE_LABEL])
        entry["digest"] = record["digest"]
        if record["status"] == "computed":
            entry["value"] = record["per_unit"]["value"]
            line_totals.append(Decimal(record["total"]["value"]))
    subtotal["inputs"] = list(dict.fromkeys(
        [e["name"] for e in freight_entries] + [e["name"] for e in lines]))[:50]
    computed = len(line_totals) == len(line_ids)
    duty_total = sum(duty_shares, Decimal(0))
    sub = goods_total + freight_total
    subtotal["steps"] = [goods_step, f"freight: {_dec_str(freight_total)} {target} "
                         f"({freight_label}), {ESTIMATE_BASIS}"[:MAX_STEP_CHARS],
                         f"subtotal: goods + freight = {_dec_str(sub)} {target}",
                         ESTIMATE_LABEL]
    if not computed:
        subtotal["steps"].insert(-1, "not every line's cited inputs resolved for this reader "
                                     "(see the gaps); no subtotal is stated")
        total["steps"] = [f"{len(line_ids) - len(line_totals)} of {len(line_ids)} estimate "
                          "line(s) have no value; no estimate is stated", ESTIMATE_LABEL]
        return out
    subtotal["value"] = _dec_str(sub)
    subtotal["digest"] = _sha(_canon({"steps": subtotal["steps"]}))
    estimate = sum(line_totals, Decimal(0))
    steps = [f"{e['name']}: total {_dec_str(v)} {target}" for e, v in zip(lines, line_totals)]
    steps = steps[:20] + ([f"... {len(steps) - 20} more line(s)"] if len(steps) > 20 else [])
    steps += [f"subtotal (goods + freight): {_dec_str(sub)} {target}",
              f"duty: {_dec_str(duty_total)} {target}", *listed,
              *([currency_step] if currency_step else [])]
    if default:
        steps.append(f"range: {_dec_str(sub)} {target} (no duty) to {_dec_str(estimate)} "
                     f"{target} (duty at the Portal default tariffRate)")
    steps.append(ESTIMATE_LABEL)
    total.update(value=_dec_str(estimate), steps=[s[:MAX_STEP_CHARS] for s in steps][:40],
                 digest=_sha(_canon({"lines": [e.get("digest") for e in lines],
                                     "steps": steps})))
    total["inputs"] = list(dict.fromkeys([subtotal["name"]] + total["inputs"]))[:50]
    return out


def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with a plan's ``engine_args`` for this engine: every role names a property
    evidence item listed before the compute item, the line roles share one object type, the
    quote roles another, ``item_link`` is a declared link from the line type to the item roles'
    type, ``item_twin_link`` one between records of that type, and ``quote_link`` and
    ``confirmation_link`` each one from the quote (confirmation) type to the root type."""
    out: list[str] = []
    roles = args.get("roles") if isinstance(args, Mapping) else None
    if not isinstance(roles, Mapping):
        return ["engine_args.roles must map roles to evidence names"]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    links = getattr(schema, "link_types", None) or {}
    for role in set(roles) - set(PLAN_ROLES):
        out.append(f"unknown role {role}")
    for role in set(PLAN_ROLES) - _OPTIONAL_ROLES - set(roles):
        out.append(f"role {role} is required")
    for role, name in roles.items():
        if items.get(name, {}).get("kind") != "property":
            out.append(f"role {role}: {name} is not a property evidence item listed before it")

    def types_of(names: Sequence[str]) -> set[str]:
        return {items[roles[r]]["object_type"] for r in names
                if items.get(roles.get(r) or "", {}).get("kind") == "property"}
    line_types = types_of(_LINE_ROLES)
    if len(line_types) > 1:
        out.append("unit_price, quantity and received must be read from one object type")
    from .ontology_tools_investigate import plan_root_types
    root_types = plan_root_types(doc, schema)
    line_link = links.get(args.get("line_link"))
    if schema is not None and (line_link is None or {line_link["from"]} != line_types or
                               line_link["to"] not in root_types):
        out.append("line_link must be a declared link from the line type to the root type")
    for role in ("classification", "units_per_box"):
        item = items.get(roles.get(role) or "", {})
        link = links.get(args.get("item_link"))
        if item.get("kind") == "property" and schema is not None and (
                link is None or {link["from"]} != line_types or link["to"] != item["object_type"]):
            out.append(f"item_link must be a declared link from the line type to the {role}'s "
                       "type")
    if args.get("item_twin_link") is not None:
        link = links.get(args["item_twin_link"])
        box = items.get(roles.get("units_per_box") or "", {})
        if schema is not None and (link is None or link["from"] != link["to"] or
                                   link["from"] != box.get("object_type")):
            out.append("item_twin_link must be a declared link between records of the "
                       "units_per_box's type")
    for group, first, link_arg in ((QUOTE_ROLES, "quote_amount", "quote_link"),
                                   (CONFIRMATION_ROLES, "confirmation_amount",
                                    "confirmation_link")):
        if not set(group) & set(roles):
            continue
        kind = link_arg.split("_")[0]
        group_types = types_of(group)
        link = links.get(args.get(link_arg))
        if first not in roles:
            out.append(f"the {kind} roles need {first}")
        if len(group_types) > 1:
            out.append(f"the {kind} roles must be read from one object type")
        if schema is not None and (link is None or {link["from"]} != group_types or
                                   link["to"] not in root_types):
            out.append(f"{link_arg} must be a declared link from the {kind} type to the root "
                       "type")
    return out


def _entry_text(entry: Mapping[str, Any] | None) -> str | None:
    value = (entry or {}).get("value")
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _entry_decimal(entry: Mapping[str, Any] | None) -> Decimal | None:
    value = (entry or {}).get("value")
    if value is None or isinstance(value, (bool, float, Mapping)):
        return None
    try:
        number = Decimal(str(value).strip())
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def person_correction(entry: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The person's correction an engine taking reviewed inputs was handed in place of the
    source value (``ontology_tools_investigate._as_corrected``): the entry's ``review`` when
    it is a ``correct_value`` whose value the entry carries, else None (the entry is the
    source's value, read through its citation)."""
    review = (entry or {}).get("review")
    if not isinstance(review, Mapping) or review.get("decision") != "correct_value" or \
            "value" not in review or str(review["value"]) != str((entry or {}).get("value")):
        return None
    return review


def correction_sources(entry: Mapping[str, Any], review: Mapping[str, Any]) -> list[str]:
    """The handles a value a person corrected rests on, each re-read for the actor before the
    person's value is used: the corrected value's own citation and every record the
    correction cites."""
    handles = [entry["citation"]["l1_handle"]] if entry.get("citation") else []
    handles += [e["l1_handle"] for e in review.get("evidence") or ()
                if isinstance(e, Mapping) and isinstance(e.get("l1_handle"), str)]
    return list(dict.fromkeys(handles))


def _corrected_input(entry: Mapping[str, Any], unit: str,
                     currency: str | None = None) -> dict[str, Any] | None:
    """A ``compute_landed_cost`` input of a person's corrected value: derived from the value
    it corrects and the records the correction cites (every one resolved for the actor, else
    unknown); unknown when its value is no number (never the source's value); None when the
    entry is not a correction."""
    review = person_correction(entry)
    if review is None:
        return None
    number = _entry_decimal(entry)
    if number is None:
        return {"unknown": "CORRECTED_VALUE_UNREAD: the person's corrected value is not a "
                           "number", "who_can_answer": "PROC"}
    spec: dict[str, Any] = {"derived": {
        "value": _dec_str(number), "from": correction_sources(entry, review),
        "rule": f"a person's correction (plx.review correct_value {review.get('review_id')}) "
                f"of {entry['name']}"}, "unit": unit}
    if currency is not None:
        spec["currency"] = currency
    return spec


def _iso_date(entry: Mapping[str, Any] | None) -> str | None:
    text = _entry_text(entry)
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except ValueError:
        return None


def _code_of(reason: str) -> str:
    """The typed code of an unknown reason: its upper-case prefix, else a stable code."""
    head = reason.split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", head):
        return head
    if head.startswith("fx_"):
        return "MIXED_CURRENCY_NO_CITED_RATE"
    if head == "goods_cost_not_positive":
        return "UNPRICED_LINE"
    return re.sub(r"[^A-Z0-9_]", "_", head.upper())[:64] or "UNKNOWN"


def _equal_at(value: Decimal, stated: Decimal) -> bool:
    """``value`` rounded to the precision ``stated`` is written at equals it."""
    try:
        return value.quantize(stated) == stated
    except ArithmeticError:
        return False


def _quote_counts(quote: Mapping[str, Mapping[str, Any] | None], basis: str | None
                  ) -> list[tuple[Decimal, Mapping[str, Any]]]:
    """Every cited count of the quote's basis unit, with the answer entry citing it: a typed
    count field whose unit is that unit, and a basis line that is itself a count. A count in
    another unit, or a fraction of a container or pallet, is not one."""
    if basis is None or basis == "shipment":
        return []
    container = basis[:2] in ("20", "40", "45")
    units = CONTAINER_UNITS if container else _BASIS_COUNT_UNITS[basis]
    found: list[tuple[Decimal, Mapping[str, Any]]] = []
    for role in _COUNT_ROLES:
        entry = quote[role]
        value = _entry_decimal(entry)
        if entry is not None and value is not None and unit_token(entry.get("unit")) in units:
            found.append((value, entry))
    stated = basis_text_count(_entry_text(quote["quote_basis"]), basis)
    if stated is not None:
        found.append((stated, quote["quote_basis"]))
    whole = container or basis in _WHOLE_BASES
    return [(value, entry) for value, entry in found
            if value > 0 and (not whole or value == value.to_integral_value())]


def _quote_record(quote: Mapping[str, Mapping[str, Any] | None], *, as_of: str,
                  target: str | None) -> tuple[dict[str, Any], list[tuple[str, dict[str, str]]],
                                               tuple[Decimal, str] | None,
                                               list[dict[str, Any]]]:
    """(answer-entry fields, typed unknowns with their template values, the (amount,
    currency) of a quote that is not total freight, its conflicts as ``{about, inputs: [answer
    entry names], note}``) for one forwarder quote's typed fields (``quote``: role -> its
    answer entry).

    The count of the basis unit is read from every typed count field cited in that unit
    (containers, volume, chargeable weight, pallets) and from a basis line that is itself a
    count ("1 x 40HQ"); two different counts are a conflict and none is used. An amount read
    under a total label is never multiplied. On a cited basis other than per shipment (an
    unread basis included) it is the total only when a cited rate in its currency times the
    cited count equals it at its precision, or the cited count is one whole container or pallet
    (one unit-shipment, never 1 CBM or 1 kg) and no cited rate disagrees. A cited rate times
    the count that differs from it is a conflict; otherwise it is ``BASIS_NOT_TOTAL``, whatever
    the label (DECISIONS/2026-10-07-freight-quote-basis.md). A rate (``quote_rate``) is a price
    per basis unit: it becomes a total only times a cited count of that unit."""
    def money(entry: Mapping[str, Any] | None) -> tuple[Decimal, str] | None:
        value, currency = _entry_decimal(entry), (entry or {}).get("currency")
        return (value, currency) if value is not None and value > 0 and \
            currency in ISO_4217 else None
    stated, rate = money(quote["quote_amount"]), money(quote["quote_rate"])
    steps = [FREIGHT_LABELS["quote"]]
    if stated is None and rate is None:
        steps.append("not total freight: FREIGHT_QUOTE_AMOUNT_UNREAD (see the gaps)")
        return {"steps": steps}, [("FREIGHT_QUOTE_AMOUNT_UNREAD", {})], None, []
    amount_entry = quote["quote_amount"] if stated is not None else quote["quote_rate"]
    amount, currency = stated if stated is not None else rate
    covered, _ = freight_scope_terms(_entry_text(quote["quote_includes"]))
    covered |= freight_scope_terms(_entry_text(quote["quote_scope"]))[0]
    excluded, unread = freight_scope_terms(_entry_text(quote["quote_excludes"]))
    basis_text = _entry_text(quote["quote_basis"])
    basis = freight_basis(basis_text)
    whole = basis is not None and (basis[:2] in ("20", "40", "45") or basis in _WHOLE_BASES)
    # A minimum, maximum or second figure on the basis line: no count of it is applied.
    terms = basis_terms_unread(basis_text, basis)
    readings = [] if terms else _quote_counts(quote, basis)
    distinct = sorted({value for value, _ in readings})
    count_value = distinct[0] if len(distinct) == 1 else None
    per_unit = stated is not None and bool(basis_text) and basis != "shipment"
    # A stated amount on a basis other than per shipment: the total only when it is cited so.
    product = rate[0] * count_value if per_unit and rate is not None and \
        rate[1] == currency and count_value is not None else None
    agrees = product is not None and _equal_at(product, amount)
    rate_conflict = product is not None and not agrees
    not_total = per_unit and (terms or not (agrees or whole and count_value == 1 and
                                            not rate_conflict))
    count = ({"count": count_value, "_source": next(
                 e["citation"]["l1_handle"] for v, e in readings if v == count_value)}
             if stated is None and basis not in (None, "shipment") and count_value is not None
             else None)
    valid_until = _iso_date(quote["quote_valid_until"])
    found: list[tuple[str, dict[str, str]]] = []
    conflicts: list[dict[str, Any]] = []
    unit_label = basis or "an unread basis"
    if stated is None and basis is None:
        found.append(("FREIGHT_BASIS_UNREAD", {}))
    if terms:
        found.append(("FREIGHT_BASIS_TERMS_UNREAD", {"entry": quote["quote_basis"]["name"],
                                                     "basis": basis}))
    if len(distinct) > 1:
        found.append(("FREIGHT_BASIS_COUNTS_CONFLICT", {"counts": str(len(distinct)),
                                                        "basis": unit_label}))
        conflicts.append({"about": f"counts of {unit_label}",
                          "inputs": list(dict.fromkeys(e["name"] for _, e in readings)),
                          "note": f"the quote cites {len(distinct)} different counts of "
                                  f"{unit_label} ({', '.join(map(_dec_str, distinct))}); none "
                                  "is chosen"})
    if rate_conflict:
        found.append(("FREIGHT_RATE_COUNT_CONFLICT", {"basis": unit_label}))
        conflicts.append({"about": "stated amount vs rate x count", "inputs": list(dict.fromkeys(
            [quote["quote_amount"]["name"], quote["quote_rate"]["name"],
             *(e["name"] for v, e in readings if v == count_value)])),
            "note": f"rate {_dec_str(rate[0])} {currency} x {_dec_str(count_value)} "
                    f"{unit_label} = {_dec_str(product)} {currency}, not the stated amount "
                    f"{_dec_str(amount)} {currency}; neither is chosen"})
    elif not_total and len(distinct) <= 1 and not terms:
        found.append(("BASIS_NOT_TOTAL", {"priced": f"per {basis}" if basis else
                                          "on a basis the Workbench does not read"}))
    if unread:
        found.append(("FREIGHT_EXCLUSIONS_UNREAD", {"unread": str(unread)}))
    checks = [f"basis check: {conflicts[0]['note']}"] if len(distinct) > 1 else []
    if terms:
        checks.append(f"basis check: the basis line states a term besides its {basis} unit "
                      "(a minimum, a maximum or a second figure), which is not applied")
    if rate_conflict:
        checks.append(f"basis check: {conflicts[-1]['note']}")
    elif agrees:
        checks.append(f"basis check: rate {_dec_str(rate[0])} {currency} x "
                      f"{_dec_str(count_value)} {basis} = the stated total")
    elif per_unit and not not_total:
        checks.append(f"basis check: the cited count is 1 {basis}")
    steps += [f"stated amount: {_dec_str(amount)} {currency} (rate basis "
              f"{basis or 'not read'}; not cited as the shipment total)" if not_total else
              f"quoted total: {_dec_str(amount)} {currency} (the stated total, never "
              f"multiplied; rate basis {basis or 'not read'})" if stated is not None else
              f"quoted rate: {_dec_str(amount)} {currency} per {unit_label}", *checks,
              f"scope cited: includes {_components(covered) or 'no component named'}; excludes "
              f"{_components(excluded) or 'no component named'}",
              f"valid until {valid_until} (as of {as_of})" if valid_until else
              "validity: not read"]
    try:
        assessed = assess_freight_quote(
            {"_source": amount_entry["citation"]["l1_handle"], "amount": str(amount),
             "currency": currency, "basis": basis if stated is None and basis else "shipment",
             "includes": sorted(covered), "excludes": sorted(excluded),
             "valid_until": valid_until, "label": _entry_text(quote["quote_scope"]) or ""},
            as_of=as_of, basis_count=count)
    except LandedCostError:
        assessed = None
        found.append(("FREIGHT_SCOPE_CONTRADICTS", {}))
    if assessed is not None:
        params = {"covered": _components(covered) or "no freight component it names",
                  "missing": _components(set(TOTAL_FREIGHT) - covered),
                  "left": _components((set(TOTAL_FREIGHT) - covered) | excluded),
                  "valid_until": valid_until or "", "as_of": as_of, "basis": basis or ""}
        found += [(item["code"], params) for item in assessed["unknowns"]
                  if len(distinct) <= 1 and not terms or
                  item["code"] != "FREIGHT_BASIS_COUNT_UNKNOWN"]
        if stated is None and basis not in (None, "shipment") and count is None and \
                len(distinct) <= 1 and not terms:
            found.append(("PACK_SPEC_NOT_SERVED", params))
    usable = assessed is not None and assessed["usable_as_total_freight"] and not found
    out: dict[str, Any] = {"unit": currency, "currency": currency}
    if usable:
        total = assessed["total_freight"]["value"]
        out["value"] = total
        steps.append(f"total freight: {_dec_str(amount)} {currency} x "
                     f"{_dec_str(count['count'])} {basis} = {total} {currency}" if count else
                     f"total freight: {total} {currency} (the quote's stated total)")
    else:
        steps.append("not total freight: " + ", ".join(dict.fromkeys(c for c, _ in found))
                     + " (see the gaps)")
    if assessed is None or assessed["duties_and_taxes_separate"]:
        steps.append("duties and taxes: not in this quote; a separate duty input, never freight")
    if target is not None and currency != target:
        steps.append(f"the quote is in {currency}, the purchase order in {target}: no exchange "
                     "rate is projected, so it is not compared with the estimate")
    out["steps"] = steps
    out["digest"] = _sha(_canon({"amount": _dec_str(amount), "stated_total": stated is not None,
                                 "currency": currency, "basis": basis,
                                 "count": None if count_value is None else _dec_str(count_value),
                                 "includes": sorted(covered), "excludes": sorted(excluded),
                                 "valid_until": valid_until, "as_of": as_of,
                                 "unknowns": [c for c, _ in found], "steps": steps}))
    return out, found, None if usable else (amount, currency), conflicts


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Resolver | None, actor: Any,
             complete: bool, covers: Callable[[str], bool],
             assumptions: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Computed answer entries, gaps and reason codes for one plan run (see the block above).

    ``evidence``: the answer entries the plan produced, by evidence name; ``edges``: the links
    the run reached; ``items``: the plan's evidence items by name (the gap's searched sources);
    ``complete``: no step of the run was truncated or cut by its budget; ``covers(link_type)``:
    the reader's read scope shows every object that link can attach to the purchase order
    (decided from its policy, so a line withheld from the reader is never silently missing).
    Header freight and duty are allocated only over a line set proven complete: every line the
    ``line_link`` edges name read with a price, a quantity and a received quantity, in a
    complete run, by a reader whose scope covers the ``line_link``, and only when every line's
    price is per the unit of its quantities in the target currency (else no line has an
    allocation weight that is a cited amount). Each forwarder quote the run reached is its own
    entry (``<name>_freight_quote_<n>``): its total freight only when ``assess_freight_quote``
    finds it complete, valid on ``as_of`` and counted; a quote whose own figures disagree is a
    ``value_disagreement`` conflict (``conflicts``: ``inputs`` names the answer entries whose
    cited values disagree, which the caller attaches). A purchase order with a line sent
    unpriced and a supplier confirmation stating an amount has a goods entry
    (``<name>_goods``): the confirmed amount only when every linked confirmation states a
    readable one and they agree; else null, with PI_VERSION_UNRESOLVED when confirmations
    state different amounts, unit prices or quantities and PI_AMOUNT_UNREAD naming each one whose
    amount is unread. The cases each line ships in (``<name>_packaging``)
    come from the item's units per case; a missing one, or records stating different ones, is a
    gap that does not hold up the landed cost. Gaps never quote source text: they name answer
    entries, codes, freight components, parsed dates and ISO 4217 currencies only.

    A header value (currency, freight or duty estimate) read only through a record the run
    reached by a lead-tier link (``lead_basis``: an unconfirmed twin) is dropped: no amount rests
    on it (PO_HEADER_ON_UNCONFIRMED_TWIN). A purchase order not every line of which is fully
    received, with no receipt actual posted, also gets the estimate entries (``ESTIMATE_LABEL``,
    ``_estimate``); ``assumptions``: the person-recorded assumptions of the case on this
    purchase order (``duty_assumption`` records; one that does not validate is a gap, never used).
    """
    roles = args["roles"]
    got = {role: list(evidence.get(roles.get(role) or "", ())) for role in PLAN_ROLES}
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    reasons: list[str] = []

    def searched(*role_names: str) -> list[str]:
        out = []
        for role in role_names:
            item = items.get(roles.get(role) or "")
            if item:
                out.append(f"{item['object_type']}.{'|'.join(item['properties'])}"[:64])
        return out

    def unknown(code: str, subject: str, detail: str, *role_names: str,
                line: str | None = None, holds: bool = True, who: str | None = None) -> None:
        """One gap per (code, subject, detail); the lines it holds for are listed in it.
        ``holds``: the gap's code is a reason the answer is partial; ``who``: who can answer
        it (``who_can_answer``; without it the executor names the searched sources' owners)."""
        detail = (detail if detail.startswith(code) else f"{code}: {detail}")[:600]
        gap = grouped.get((code, subject, detail))
        if gap is None:
            gap = grouped[(code, subject, detail)] = {
                "state": _GAP_STATES.get(code, "unknown"), "reason": code.lower()[:64],
                "subject": subject, "detail": detail, "lines": []}
            sources = searched(*role_names)
            if sources:
                gap["searched"] = {"sources": sources[:8]}
        if who and who[:200] not in gap.get("who_can_answer", ()):
            gap["who_can_answer"] = (gap.get("who_can_answer", []) + [who[:200]])[:10]
        if line:
            gap["lines"].append(line)
        if holds and code.lower() not in reasons:
            reasons.append(code.lower()[:64])

    def open_input(line: str, cost: str, reason: str, who: str | None) -> None:
        unknown(_code_of(reason), cost, reason, *_COST_ROLES.get(cost, ()), line=line, who=who)

    def entry_names(role: str) -> str:
        return ", ".join(e["name"] for e in got[role])[:200]

    def by_object(role: str) -> dict[str, Mapping[str, Any]]:
        out: dict[str, Mapping[str, Any]] = {}
        for entry in got[role]:  # a required value no record carried has no object
            if entry.get("object_id") is not None:
                out.setdefault(entry["object_id"], entry)
        return out

    def handle(entry: Mapping[str, Any] | None) -> str | None:
        return entry["citation"]["l1_handle"] if entry and entry.get("citation") else None

    # A header value read only through an unconfirmed twin (a lead-tier link) never feeds a
    # number: it is dropped here, before any role is read, and named in a gap.
    for role in ("currency", "freight_estimate", "duty_estimate"):
        kept = [e for e in got[role] if e.get("lead_basis") is not True]
        dropped = [e for e in got[role] if e.get("lead_basis") is True
                   and e.get("value") is not None]
        if dropped:
            unknown("PO_HEADER_ON_UNCONFIRMED_TWIN", "purchase order " + role,
                    f"{', '.join(e['name'] for e in dropped)[:200]} rest(s) only on a record "
                    "reached by a lead-tier link (an unconfirmed twin of the purchase order); "
                    "no amount rests on it until a person confirms the twin", role,
                    holds=not any(e.get("value") is not None for e in kept), who="PROC")
        got[role] = kept
    header_ids = {e["object_id"] for role in ("currency", "freight_estimate", "duty_estimate")
                  for e in got[role] if isinstance(e.get("object_id"), str)}

    # Each cited currency cell is read by the ontology's one rule (``currency_code``: a code
    # in any case, or an ISO code with its own sign attached such as the Portal's ``CAD$``);
    # a cell no rule reads keeps its text, so it stays not an ISO 4217 code, never guessed.
    folded = {t: currency_code(t) for t in map(_entry_text, got["currency"]) if t}
    currencies = sorted({code or t for t, (code, _) in folded.items()})
    rules = sorted({rule for code, rule in folded.values() if rule and code in currencies})
    target: str | None = None
    if not currencies:
        unknown("CURRENCY_UNCITED", "purchase order currency",
                "no record of the purchase order states its currency; none is assumed",
                "currency")
    elif len(currencies) > 1:
        valid = [c for c in currencies if c in ISO_4217]
        others = len(currencies) - len(valid)
        unknown("MIXED_CURRENCY_NO_CITED_RATE", "purchase order currency",
                f"the purchase order's records ({entry_names('currency')}) state "
                f"{len(currencies)} currencies: " + " and ".join(
                    valid + ([f"{others} value(s) that are not ISO 4217 codes"] if others else []))
                + "; the ontology projects no exchange rate, so no amount is converted",
                "currency")
    elif currencies[0] not in ISO_4217:
        unknown("CURRENCY_NOT_ISO_4217", "purchase order currency",
                f"the cited currency ({entry_names('currency')}) is not an ISO 4217 code",
                "currency")
    else:
        target = currencies[0]
    currency_step = (f"currency: the cited currency cell(s) read as {target} by the ontology "
                     f"rule {' and '.join(rules)} (ontology.units.currency_code: a written form "
                     "whose currency is not in doubt)") if target and rules else None

    prices, quantities, received = (by_object(r) for r in _LINE_ROLES)
    line_ids = sorted(set(prices) | set(quantities))
    linked = {e["from"] for e in edges if e.get("link_type") == args.get("line_link")}
    unproven = sorted(oid for oid in linked | set(line_ids) if not all(
        (role.get(oid) or {}).get("value") is not None for role in (prices, quantities, received)))
    covered = covers(args["line_link"])
    lines_complete = complete and covered and bool(linked) and not unproven
    if not lines_complete:
        if unproven:
            why = (f"{len(unproven)} of {len(linked | set(line_ids))} line(s) of the purchase "
                   "order were not read with a price, an ordered and a received quantity")
        elif not complete:
            why = ("the run was truncated or cut by its step budget, so the line set is not "
                   "proven complete")
        elif not covered:
            why = ("this reader's read scope is not proven to show every line of the purchase "
                   "order (a marking or a source grant it lacks may withhold one), so the line "
                   "set is not proven complete")
        else:
            why = "no line of the purchase order was reached"
        unknown("PO_LINES_INCOMPLETE", "purchase order lines", why
                + "; header freight and duty are allocated only over every line, never a partial "
                  "set", "unit_price", "quantity", "received")
    item_of = {e["from"]: e["to"] for e in edges if e.get("link_type") == args.get("item_link")}
    classes = by_object("classification")

    def grounded(oid: str) -> bool:
        """The line's allocation weight (unit price x received quantity) is a cited amount: its
        price is per the unit its ordered and received quantities are in, in the target currency."""
        price, qty, rcv = prices.get(oid), quantities.get(oid), received.get(oid)
        unit = unit_token(price.get("unit")) if price and price.get("value") is not None else None
        return (unit is not None and target is not None and price.get("currency") == target
                and all(q and q.get("value") is not None and unit_token(q.get("unit")) == unit
                        for q in (qty, rcv)))

    # LC-ALLOCATION-DENOMINATOR-UNKNOWN: the Portal splits header freight by line value, so a
    # single line whose price denominator is not cited makes every line's share unknown.
    weights_grounded = bool(line_ids) and all(grounded(oid) for oid in line_ids)
    if lines_complete and target is not None and not weights_grounded:
        unknown("ALLOCATION_DENOMINATOR_UNKNOWN", "purchase order lines",
                f"{sum(not grounded(oid) for oid in line_ids)} of {len(line_ids)} line(s) state "
                "no unit price per the unit of their ordered and received quantities in the "
                "purchase order's currency; header freight and duty are not allocated over "
                "line values that are not cited amounts", "unit_price", "quantity", "received")

    def one(role: str) -> tuple[Mapping[str, Any] | None, bool]:
        """The single non-empty value of a header role, and whether records disagree."""
        values = [e for e in got[role] if e.get("value") is not None]
        distinct = {str(e["value"]) for e in values}
        return (values[0] if len(distinct) == 1 else None), len(distinct) > 1

    freight_est, freight_conflict = one("freight_estimate")
    duty_est, duty_conflict = one("duty_estimate")
    for role, conflict in (("freight_estimate", freight_conflict),
                           ("duty_estimate", duty_conflict)):
        if conflict:
            unknown("PO_ESTIMATE_RECORDS_DISAGREE", "purchase order " + role,
                    "the purchase order's records state different amounts; none is chosen", role)
    off_currency = {part for part, est in (("freight", freight_est), ("duty", duty_est))
                    if est is not None and est.get("currency") != target}
    po = {"_source": handle(freight_est) or handle(duty_est),
          "shippingCost": freight_est["value"] if freight_est else None,
          "dutiesCost": duty_est["value"] if duty_est else None}
    view = [{"id": oid, "_source": handle(prices.get(oid)) or handle(quantities.get(oid)),
             "unitPrice": (prices.get(oid) or {}).get("value"),
             "quantity": (quantities.get(oid) or {}).get("value"),
             "quantityReceived": (received.get(oid) or {}).get("value")} for oid in line_ids]

    # Receipt actuals (first receipt of a PO only): read when any receipt value was reached; an
    # actual is used only with a cited currency equal to the target, else that part is unknown.
    receipt: dict[str, Any] | None = None
    receipt_entries = got["receipt_freight"] + got["receipt_duty"]
    actual = {part: [e for e in got[role] if _positive(e.get("value"))]
              for part, role in (("freight", "receipt_freight"), ("duty", "receipt_duty"))}
    carriers = sorted({e["object_id"] for found in actual.values() for e in found})
    blocked: set[str] = set()
    if len(carriers) > 1:
        blocked = {part for part, found in actual.items() if found}
        unknown("RECEIPT_ACTUALS_ON_SEVERAL_RECEIPTS", "receipt actuals",
                f"{len(carriers)} receipts record freight or duty actuals; the Portal keeps "
                "them on the first receipt of a purchase order only", "receipt_freight", "receipt_duty")
    else:
        for part, found in actual.items():
            if found and any(e.get("currency") is None or e.get("currency") != target
                             for e in found):
                blocked.add(part)
                unknown("RECEIPT_ACTUAL_CURRENCY_UNRECORDED", f"receipt actual {part}",
                        f"the receipt records {part} {found[0]['value']}, which the Portal posts "
                        "over the purchase order estimate, without a cited currency equal to the "
                        "purchase order's", f"receipt_{part}")
    usable: dict[str, Mapping[str, Any]] = {}
    if receipt_entries:
        usable = {part: found[0] for part, found in actual.items()
                  if found and part not in blocked}
        receipt = {"_source": handle(next(iter(usable.values()), receipt_entries[0])),
                   "freightCost": (usable.get("freight") or {}).get("value"),
                   "dutiesCost": (usable.get("duty") or {}).get("value")}

    # Forwarder quotes naming the purchase order: each a current quote, assessed on its own.
    quote_ids = sorted({e["from"] for e in edges if e.get("link_type") == args.get("quote_link")}
                       | {e["object_id"] for role in QUOTE_ROLES for e in got[role]})
    quote_entries: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    partial_quotes: set[tuple[Decimal, str]] = set()
    quote_totals: list[tuple[Decimal, str, list[Mapping[str, Any]]]] = []
    for index, oid in enumerate(quote_ids, start=1):
        fields = {role: next((e for e in got[role] if e["object_id"] == oid), None)
                  for role in QUOTE_ROLES}
        record, found, partial, disagree = _quote_record(fields, as_of=as_of, target=target)
        if partial is not None:
            partial_quotes.add(partial)
        subject = f"freight quote {index}"
        for code, params in found:
            _, who, text = _QUOTE_GAPS[code]
            unknown(code, subject, text.format(**params),
                    *(_QUOTE_COUNT_SEARCHED if code in _QUOTE_COUNT_CODES else _QUOTE_SEARCHED),
                    who=who)
        entry_name = f"{name}_freight_quote_{index}"[:64]
        if record.get("value") is not None and record.get("currency") == target:
            quote_totals.append((Decimal(record["value"]), entry_name,
                                 [e for e in fields.values() if e]))
        # A machine-read quote disagreeing with itself: a lead, never resolved here.
        conflicts += [{"kind": "value_disagreement", "subject": f"{entry_name}: {c['about']}"[:700],
                       "inputs": c["inputs"][:12], "note": c["note"][:600], "tier": "lead"}
                      for c in disagree]
        quote_entries.append({"name": entry_name, "kind": "computed",
                              "value": None, **record,
                              "inputs": [e["name"] for e in fields.values() if e][:50],
                              "basis": [oid], "check": "not_applicable"})
    estimate = _entry_decimal(freight_est)
    estimate_is_partial_quote = estimate is not None and any(
        currency == freight_est.get("currency") and
        abs(estimate - quoted) <= quoted * ESTIMATE_QUOTE_TOLERANCE
        for quoted, currency in partial_quotes)

    # Supplier confirmations (proforma invoices) naming the purchase order: the goods of a line
    # sent unpriced, read from the confirmation's cited figures (a lead, machine-read).
    def figure(oid: str, role: str) -> Mapping[str, Any] | None:
        return next((e for e in got[role] if e.get("object_id") == oid
                     and _entry_decimal(e) is not None), None)

    def same_quantity(one: Mapping[str, Any] | None, other: Mapping[str, Any] | None) -> bool:
        unit = unit_token((one or {}).get("unit"))
        return unit is not None and unit == unit_token((other or {}).get("unit")) and \
            _entry_decimal(one) == _entry_decimal(other)

    # Every confirmation linked to the purchase order (its link, or a figure the walk read
    # from it) is a version; the goods are one amount only when every version states a
    # readable amount and they and their unit prices all agree. A version whose amount is
    # unread is PI_AMOUNT_UNREAD (its amount unknown, so no other version's is taken).
    confirmation_ids = sorted(
        {e["from"] for e in edges if e.get("link_type") == args.get("confirmation_link")
         and args.get("confirmation_link")}
        | {e["object_id"] for role in CONFIRMATION_ROLES for e in got[role]
           if isinstance(e.get("object_id"), str)})
    amounts = [e for e in got["confirmation_amount"] if _entry_decimal(e) is not None]
    versions = {(_entry_decimal(e), e.get("currency")) for e in amounts}
    unit_prices = [e for e in got["confirmation_unit_price"] if _entry_decimal(e) is not None]
    price_versions = {(_entry_decimal(e), e.get("currency"), unit_token(e.get("unit")))
                      for e in unit_prices}
    counts = [e for e in got["confirmation_quantity"] if _entry_decimal(e) is not None]
    count_versions = {(_entry_decimal(e), unit_token(e.get("unit"))) for e in counts}
    unread = [oid for oid in confirmation_ids
              if not any(e.get("object_id") == oid for e in amounts)]
    # Versions differ when their amounts, unit prices or quantities (so unit price x
    # quantity) differ; any one of them is a person's call.
    differ = len(versions) > 1 or len(price_versions) > 1 or len(count_versions) > 1
    differing = [what for what, found in (("amounts", versions), ("unit prices", price_versions),
                                          ("quantities", count_versions)) if len(found) > 1]
    unpriced = [oid for oid in line_ids if not person_correction(prices.get(oid)) and (
        (prices.get(oid) or {}).get("value") is None or
        (_entry_decimal(prices.get(oid)) is not None and _entry_decimal(prices[oid]) <= 0))]
    confirmed: dict[str, Any] | None = None

    def unread_names() -> str:
        """The answer entries of the confirmations whose amount is unread (their null amount,
        else their other figures, else the confirmation itself)."""
        out = []
        for oid in unread:
            own = [e["name"] for role in CONFIRMATION_ROLES for e in got[role]
                   if e.get("object_id") == oid]
            out.append(next((e["name"] for e in got["confirmation_amount"]
                             if e.get("object_id") == oid), None) or
                       (own[0] if own else f"confirmation {oid}"))
        return ", ".join(out)[:300]

    def unread_text() -> str:
        return (f"PI_AMOUNT_UNREAD: no readable amount is cited for {unread_names()} (see "
                f"{name}_goods), a supplier confirmation of the purchase order; no other "
                "confirmation's amount is taken")

    if confirmation_ids and unpriced:
        names = [e["name"] for e in amounts]
        sources = set(confirmation_ids)
        figures = [e for role in CONFIRMATION_ROLES for e in got[role]
                   if e.get("object_id") in sources and e not in amounts]
        confirmed = {"name": f"{name}_goods"[:64], "kind": "computed", "value": None,
                     "inputs": (names + [e["name"] for e in figures])[:50], "steps": [f"{e['name']}: {e['value']} "
                                                     f"{e.get('currency') or '(no currency)'}"
                                                     for e in amounts][:20],
                     "basis": sorted(sources)[:100],
                     "check": "not_applicable"}
        if differ:
            what = " and ".join(differing)
            confirmed["steps"].append(f"PI_VERSION_UNRESOLVED: {len(confirmation_ids)} supplier "
                                      f"confirmations state different {what}; none is chosen")
            listed = names + ([e["name"] for e in unit_prices]
                              if len(price_versions) > 1 else []) + \
                ([e["name"] for e in counts] if len(count_versions) > 1 else [])
            conflicts.append({"kind": "value_disagreement",
                              "subject": f"PI_VERSION_UNRESOLVED: {confirmed['name']}: the "
                                         "supplier confirmations of the purchase order state "
                                         f"different {what}"[:700],
                              "inputs": listed[:12], "tier": "lead",
                              "note": "a revised proforma invoice is a person's call (which "
                                      "version the purchase order is priced at); none is "
                                      "chosen"})
        if unread:
            confirmed["steps"].append(f"PI_AMOUNT_UNREAD: no readable amount is cited for "
                                      f"{unread_names()}; no other confirmation's amount is "
                                      "taken")
            # Its own gap even beside differing versions: a person reads that amount.
            unknown("PI_AMOUNT_UNREAD", "goods", unread_text(), *_COST_ROLES["goods"],
                    who="PROC")
        if differ or unread:
            pass  # no amount is the goods (the steps above name why)
        elif amounts[0].get("currency") is None or amounts[0]["currency"] != target:
            confirmed["steps"].append("CONFIRMATION_CURRENCY_DIFFERS: the confirmed amount "
                                      "carries no cited currency equal to the purchase order's")
        else:
            confirmed.update(value=str(amounts[0]["value"]), unit=target, currency=target)
            confirmed["steps"].append(f"goods: {amounts[0]['value']} {target}, the amount the "
                                      "supplier confirmation states (a lead, machine-read)")

    def confirmed_goods(oid: str) -> tuple[dict[str, Any], list[Mapping[str, Any]]] | None:
        """The goods input of an unpriced line from the confirmations, with the confirmed amount
        it rests on (none while the versions are unresolved: the goods entry names them); None
        when no confirmation states an amount."""
        if confirmed is None or oid not in unpriced:
            return None
        if differ:
            return {"unknown": f"PI_VERSION_UNRESOLVED: {len(confirmation_ids)} supplier "
                               "confirmations of the purchase order state different "
                               f"{' and '.join(differing)} (see "
                               f"{confirmed['name']}); none is chosen",
                    "who_can_answer": "PROC"}, []
        if unread:
            return {"unknown": unread_text(), "who_can_answer": "PROC"}, []
        amount = amounts[0]
        if confirmed["value"] is None:
            return {"unknown": "CONFIRMATION_CURRENCY_DIFFERS: the supplier confirmation's "
                               "amount carries no cited currency equal to the purchase order's",
                    "who_can_answer": "PROC"}, [amount]
        source = amount["object_id"]
        price, count = (figure(source, r) for r in ("confirmation_unit_price",
                                                    "confirmation_quantity"))
        single_item = price is not None and count is not None and \
            unit_token(price.get("unit")) == unit_token(count.get("unit")) and \
            _equal_at(_entry_decimal(price) * _entry_decimal(count), _entry_decimal(amount))
        if line_ids == [oid]:
            own = (count is None or same_quantity(count, quantities.get(oid))) and \
                (price is None or count is None or single_item)
        else:
            own = single_item and [line for line in line_ids if same_quantity(
                count, quantities.get(line))] == [oid]
        if not own:
            return {"unknown": f"CONFIRMATION_NOT_PER_LINE: the supplier confirmation's amount "
                               f"(see {confirmed['name']}) is not cited as this line's alone",
                    "who_can_answer": "PROC"}, [amount]
        return _corrected_input(amount, target, target) or {
            "citation": handle(amount), "field": amount["citation"]["field_path"],
            "unit": target, "currency": target}, [amount]

    entries: list[dict[str, Any]] = []
    totals: list[tuple[str, str, Decimal]] = []
    weights = [e for line in line_ids for e in (prices.get(line), received.get(line)) if e]
    for index, oid in enumerate(line_ids, start=1):
        label = f"{name}_line_{index}"
        qty, price = quantities.get(oid), prices.get(oid)
        from_confirmation = confirmed_goods(oid)
        used = [e for e in (price, qty, received.get(oid), freight_est, duty_est,
                            classes.get(item_of.get(oid, "")), *receipt_entries,
                            *(from_confirmation or ((), ()))[1]) if e]
        entry: dict[str, Any] = {"name": label[:64], "kind": "computed", "value": None,
                                 "inputs": [e["name"] for e in used][:50], "steps": [],
                                 "basis": sorted({oid} | {e["object_id"] for e in used})[:100],
                                 "check": "not_applicable"}
        entries.append(entry)
        if target is None:
            entry["steps"] = ["no target currency: see the currency gap"]
            continue
        qty_unit = unit_token(qty.get("unit")) if qty and qty.get("value") is not None else None
        if qty_unit is not None:
            entry.update(unit=f"{target}/{qty_unit}"[:20], currency=target)
        price_unit = unit_token(price.get("unit")) if price else None
        if from_confirmation is not None:
            goods: dict[str, Any] = from_confirmation[0]
        elif price is None or price.get("value") is None:
            goods = {"unknown": "UNPRICED_LINE: no cited unit price",
                                     "who_can_answer": "PROC"}
        elif price_unit is None:
            goods = {"unknown": "UNIT_PRICE_DENOMINATOR_UNKNOWN: the cited unit price states no "
                                "unit it is per", "who_can_answer": "PROC"}
        elif qty_unit is not None and price_unit != qty_unit:
            goods = {"unknown": f"UNIT_PRICE_DENOMINATOR_MISMATCH: the price is per {price_unit}, "
                                f"the quantity is in {qty_unit}; no conversion is assumed",
                     "who_can_answer": "PROC"}
        elif price.get("currency") is None:
            goods = {"unknown": "UNIT_PRICE_CURRENCY_UNCITED: no currency comes with the cited "
                                "unit price", "who_can_answer": "PROC"}
        else:
            goods = _corrected_input(price, f"{price['currency']}/{qty_unit}",
                                     price["currency"]) or {
                "citation": handle(price), "field": price["citation"]["field_path"],
                "unit": f"{price['currency']}/{qty_unit}", "currency": price["currency"]}
        engine = po_line_landed(po, view, oid, receipt=receipt)
        hs = classes.get(item_of.get(oid, ""))
        costs: list[dict[str, Any]] = [{"name": "goods", "category": "goods", **goods}]
        # What each derived part is computed from: the header (or the receipt actual the
        # Portal posts over it) and, for freight, every line's allocation weight.
        rests = {"freight": [usable.get("freight", freight_est), *weights],
                 "duty": [usable.get("duty", duty_est)], "receipt_actual": []}
        corrected_inputs: list[Mapping[str, Any]] = []
        for part, category in (("freight", "freight"), ("duty", "duty"),
                               ("receipt_actual", "other")):
            spec = engine[part]
            if spec is None:
                continue
            if part in ("freight", "duty") and not lines_complete:
                spec = {"unknown": "PO_LINES_INCOMPLETE: header " + part + " is not allocated "
                                   "over a partial line set (see the lines gap)",
                        "who_can_answer": "PROC"}
            elif part in off_currency and part not in usable:
                spec = {"unknown": "PO_ESTIMATE_CURRENCY_DIFFERS: the header " + part + " "
                                   "estimate carries no cited currency equal to the purchase "
                                   "order's", "who_can_answer": "PROC"}
            elif part in blocked:
                spec = {"unknown": f"RECEIPT_ACTUAL_UNUSABLE: the receipt's actual {part}, which "
                                   "the Portal posts over the estimate, is not usable (see the "
                                   "receipt gap)", "who_can_answer": "WH"}
            elif part == "freight" and "derived" in spec and "freight" not in usable and \
                    estimate_is_partial_quote:
                spec = {"unknown": "FREIGHT_ESTIMATE_IS_PARTIAL_QUOTE: the header shipping "
                                   "estimate is within "
                                   f"{_dec_str(ESTIMATE_QUOTE_TOLERANCE * 100)}% of the amount "
                                   "of a cited forwarder quote that is not the total freight "
                                   "(see the freight quote gaps)",
                        "who_can_answer": FORWARDER}
            elif part in ("freight", "duty") and "derived" in spec and not weights_grounded:
                spec = {"unknown": "ALLOCATION_DENOMINATOR_UNKNOWN: header " + part + " is not "
                                   "allocated over line values whose price denominator is not "
                                   "cited (see the lines gap)", "who_can_answer": "PROC"}
            if "derived" in spec:
                # An amount or allocation weight a person corrected: the derived value rests
                # on the correction too, and the line names it as an input.
                fixed = [e for e in rests[part] if e and person_correction(e)]
                derived = dict(spec["derived"])
                if fixed:
                    derived["from"] = list(dict.fromkeys(derived["from"] + [
                        h for e in fixed for h in correction_sources(e, person_correction(e))]))
                    derived["rule"] += " over a person's correction of " + ", ".join(
                        e["name"] for e in fixed)
                    corrected_inputs += [e for e in fixed if e not in used]
                spec = {"derived": derived, "unit": f"{target}/{qty_unit}"
                        if spec["derived"]["per_quantity_unit"] else target}
            elif part == "duty" and spec["unknown"].startswith("TARIFF_TREATMENT_UNVERIFIED"):
                # The code itself stays in its cited entry: steps carry no source text.
                spec = {"unknown": "DUTY_RATE_SOURCE_ABSENT: an HS code is cited for the item, "
                                   "but no duty-rate source is ingested (the ontology projects no "
                                   "tariff rate)", "who_can_answer": "FIN"} \
                    if hs and _entry_text(hs) else \
                    {"unknown": "HS_CODE_NOT_CITED: no HS code is cited for the item, so its "
                                "tariff treatment is not known", "who_can_answer":
                     "PROC"}
            costs.append({"name": part, "category": category, **spec})
        if corrected_inputs:   # a sibling line's corrected weight moves this line's share
            entry["inputs"] = list(dict.fromkeys(
                entry["inputs"] + [e["name"] for e in corrected_inputs]))[:50]
            entry["basis"] = sorted(set(entry["basis"]) | {
                e["object_id"] for e in corrected_inputs})[:100]
        freight_cost = next((c for c in costs if c["name"] == "freight"), {})
        freight_label = FREIGHT_LABELS["none" if "derived" not in freight_cost else
                                       "posted" if "freight" in usable else "estimate"]
        if qty_unit is None:
            # The engine divides by a known quantity; every other open input is still named.
            unknown("LINE_QUANTITY_UNKNOWN", "quantity", "no cited ordered quantity with a unit",
                    "quantity", line=label)
            for cost in costs:
                if "unknown" in cost:
                    open_input(label, cost["name"], cost["unknown"], cost.get("who_can_answer"))
            entry["steps"] = ["no cited ordered quantity with a unit: nothing to divide by",
                              freight_label]
            continue
        # A step names an open input by its code; the reason is stated once, in its line gap.
        open_reasons = {}
        for cost in costs:
            if "unknown" in cost:
                open_reasons[cost["name"]] = cost["unknown"]
                cost["unknown"] = _code_of(cost["unknown"]) + ": see the gaps"
        try:
            record = compute_landed_cost(
                costs, _corrected_input(qty, qty_unit) or {
                    "citation": handle(qty), "field": qty["citation"]["field_path"],
                    "unit": qty_unit}, target, as_of=as_of, resolver=resolver, actor=actor)
        except (ValidationError, ArithmeticError) as exc:
            code = _code_of(str(exc))
            unknown(code, "inputs", "the landed-cost engine refused the inputs", line=label)
            entry["steps"] = [f"refused: {code}"]
            continue
        for item in record["unknowns"]:
            open_input(label, item["input"], open_reasons.get(item["input"], item["reason"]),
                       item.get("who_can_answer"))
        entry["steps"] = [s[:MAX_STEP_CHARS] for s in record["steps"]][:39] + [freight_label]
        entry["digest"] = record["digest"]
        if record["status"] == "computed":
            entry["value"] = record["per_unit"]["value"]
            totals.append((label, record["digest"], Decimal(record["total"]["value"])))

    # The total rests on its line entries (inputs), each with its own basis.
    total: dict[str, Any] = {"name": name, "kind": "computed", "value": None,
                             "inputs": [e["name"] for e in entries][:50], "steps": [],
                             "check": "not_applicable"}
    if target is not None:
        total.update(unit=target, currency=target)
    if line_ids and len(totals) == len(line_ids):
        steps: list[str] = [f"{label}: total {_dec_str(value)} {target} (record {digest[:12]})"
                            for label, digest, value in totals]
        values = {f"line_{i}": (value, parse_unit(target))
                  for i, (_, _, value) in enumerate(totals, start=1)}
        result, _ = _calc(" + ".join(values), values, steps, "purchase order landed cost")
        total.update(value=_dec_str(result), steps=steps,
                     digest=_sha(_canon({"lines": [d for _, d, _ in totals], "steps": steps})))
    else:
        total["steps"] = [f"{len(line_ids) - len(totals)} of {len(line_ids)} line(s) have no "
                          "computed landed cost; no purchase order total is stated"]
    if currency_step:
        total["steps"].append(currency_step)
    total["steps"].append(("cost treatment: " + COST_TREATMENT["kind"] + "; "
                           + COST_TREATMENT["current_portal_decision"])[:MAX_STEP_CHARS])

    # Estimate mode: an open purchase order (a line not fully received) with no receipt actual.
    def fully_received(oid: str) -> bool:
        ordered, got_in = (_entry_decimal(r.get(oid)) for r in (quantities, received))
        unit = unit_token((quantities.get(oid) or {}).get("unit"))
        return ordered is not None and got_in is not None and ordered > 0 and \
            got_in >= ordered and unit is not None and \
            unit == unit_token((received.get(oid) or {}).get("unit"))
    estimates: list[dict[str, Any]] = []
    # A purchase order header with no line reached is open too: its estimate names the lines.
    if (line_ids or header_ids) and not usable \
            and not (line_ids and all(fully_received(oid) for oid in line_ids)):
        estimates = _pre_receipt_estimate(
            name=name, target=target, line_ids=line_ids, every_line=sorted(linked | set(line_ids)),
            prices=prices, quantities=quantities, linked=bool(linked), complete=complete,
            covered=covered, freight_est=freight_est, duty_est=duty_est,
            partial_quote=estimate_is_partial_quote, quotes=quote_totals,
            hs_of={oid: classes.get(item_of.get(oid, "")) for oid in line_ids},
            assumptions=assumptions or (), header_ids=header_ids, as_of=as_of,
            resolver=resolver, actor=actor, unknown=unknown, currency_step=currency_step)
    elif line_ids and not usable and target is not None:
        # Every line read fully received with no usable receipt actual: no estimate applies
        # (the receipt is the actual), and the null estimate cites the receipt gap that holds
        # the actual back, so an open purchase order never ends with no estimate entry.
        why = ("no estimate: RECEIPT_ACTUAL_NOT_READ (every line read is fully received; the "
               "landed cost rests on the receipt actuals, and no verified receipt of this "
               "purchase order is read; see the gaps)" if receipt is None else
               "no estimate: RECEIPT_ACTUALS_ON_SEVERAL_RECEIPTS (every line read is fully "
               "received; see the gaps)" if blocked and len(carriers) > 1 else
               "no estimate: RECEIPT_ACTUAL_CURRENCY_UNRECORDED (every line read is fully "
               "received; see the gaps)" if blocked else
               "no estimate: every line read is fully received and the receipt records no "
               "freight or duty actual above zero; the landed cost entries state what the "
               "Portal posts")
        # It rests on the line entries (as the total does), each citing its received quantity.
        inputs = [e["name"] for e in entries][:50]
        steps = [why[:MAX_STEP_CHARS], ESTIMATE_LABEL]
        estimates = [{"name": f"{name}_estimate"[:64], "kind": "computed", "value": None,
                      "unit": target, "currency": target, "inputs": inputs, "steps": steps,
                      "digest": _sha(_canon({"inputs": inputs, "steps": steps})),
                      "check": "not_applicable"}]

    # Packaging (LOG-01): the cases each line ships in, from its item's cited units per case,
    # read from every record of the item (its other records by ``item_twin_link``).
    items_of: dict[str, set[str]] = {}
    for e in edges:
        if e.get("link_type") == args.get("item_link"):
            items_of.setdefault(e["from"], set()).add(e["to"])
    for e in edges:
        if args.get("item_twin_link") and e.get("link_type") == args["item_twin_link"]:
            for records in items_of.values():
                if records & {e["from"], e["to"]}:
                    records.update((e["from"], e["to"]))
    packaging: list[dict[str, Any]] = []
    if any(e.get("value") is not None for e in got["units_per_box"]):
        pack: dict[str, Any] = {"name": f"{name}_packaging"[:64], "kind": "computed",
                                "value": None, "unit": "case", "inputs": [], "steps": [],
                                "basis": [], "check": "not_applicable"}
        cases_total, counted = 0, 0
        for index, oid in enumerate(line_ids, start=1):
            label = f"{name}_line_{index}"
            qty = quantities.get(oid)
            boxes = [e for e in got["units_per_box"] if e.get("object_id") in
                     items_of.get(oid, ()) and _entry_decimal(e) is not None]
            box = boxes[0] if boxes else None
            ordered, per_case = _entry_decimal(qty), _entry_decimal(box)
            if len({_entry_decimal(e) for e in boxes}) > 1:
                pack["steps"].append(f"{label}: UNITS_PER_CASE_CONFLICT: the item's records "
                                     "state different units per case; none is chosen")
                pack["inputs"] += [e["name"] for e in boxes]
                pack["basis"] += [e["object_id"] for e in boxes]
                unknown("UNITS_PER_CASE_CONFLICT", "packaging", "the item's records state "
                        f"{len({_entry_decimal(e) for e in boxes})} different units per case "
                        f"({', '.join(e['name'] for e in boxes)})"[:300]
                        + "; none is chosen", "units_per_box", line=label, holds=False,
                        who="PROC")
                continue
            if per_case is None or per_case <= 0:
                why = "no cited units per case for the line's item"
            elif ordered is None or ordered <= 0 or unit_token((qty or {}).get("unit")) != "ea":
                why = "no ordered quantity in ea, the unit units per case counts"
            else:
                cases = int((ordered / per_case).to_integral_value(rounding="ROUND_CEILING"))
                pack["steps"].append(f"{label}: ceil({_dec_str(ordered)} ea / "
                                     f"{_dec_str(per_case)} ea per case) = {cases} cases")
                pack["inputs"] += [qty["name"], box["name"]]
                pack["basis"] += [oid, box["object_id"]]
                cases_total, counted = cases_total + cases, counted + 1
                continue
            pack["steps"].append(f"{label}: {why}")
            unknown("UNITS_PER_CASE_UNKNOWN", "packaging", why, "units_per_box", "quantity",
                    line=label, holds=False, who="PROC")
        if lines_complete and line_ids and counted == len(line_ids):
            pack["value"] = str(cases_total)
            pack["steps"].append(f"cases: {cases_total} over {counted} line(s)")
        else:
            pack["steps"].append("no case total: not every line of the purchase order is counted")
        pack["steps"].append("containers: not derived; case dimensions and gross weight are in "
                             "app_product_pack_spec, which the reporting API does not serve")
        pack["inputs"], pack["basis"] = pack["inputs"][:50], sorted(set(pack["basis"]))[:100]
        packaging.append(pack)

    gaps = []
    for gap in grouped.values():
        lines = gap.pop("lines")
        if lines:
            gap["subject"] = f"{gap['subject']} of {', '.join(lines)}"
        gap["subject"] = gap["subject"][:700]
        gaps.append(gap)
    return {"entries": [total] + entries + ([confirmed] if confirmed else []) + packaging
            + quote_entries + estimates, "gaps": gaps,
            "reasons": reasons, "conflicts": conflicts}


# The draft wording of this engine's gap codes (``draft_request``): a freight quote request for a
# freight figure a forwarder's quote settles; the upstream ask its detail names otherwise.
_QUOTE_REQUEST_CODES = frozenset(_QUOTE_GAPS) - {"PACK_SPEC_NOT_SERVED"} | {
    "PO_FREIGHT_NOT_RECORDED", "FREIGHT_ESTIMATE_IS_PARTIAL_QUOTE"}
_UPSTREAM_ASKS = {"PACK_SPEC_NOT_SERVED": "please serve public.app_product_pack_spec (case "
                                          "length, width, height and gross weight) through the "
                                          "Workbench reporting API"}


def draft_request(subject: str, who: Sequence[str], codes: Sequence[str]) -> str | None:
    """The draft request (LOG-07) for one subject's gaps with one answerer, or None when no
    code is one this engine words. Built from codes and the subject only (never source text);
    a draft is a working note: nothing is sent."""
    quote = [code for code in codes if code in _QUOTE_REQUEST_CODES]
    asks = [code for code in codes if code in _UPSTREAM_ASKS]
    if not quote and not asks:
        return None
    # An undeclared answerer is addressed by the owner it names (operating/answerers.py).
    to = ", ".join(w.removeprefix(UNDECLARED) for w in who)
    parts = [f"Draft freight quote request to {to} (not sent): please quote origin charges, "
             "main carriage and destination charges, with the rate basis, the number of "
             "containers or units it is priced for, what is excluded and the validity date. "
             f"Open for {subject}: {', '.join(quote)}"] if quote else []
    parts += [f"Draft request to {to} (not sent): {_UPSTREAM_ASKS[code]}. Open for {subject}: "
              f"{code}" for code in asks]
    return " ".join(parts)[:1900]


# ``reviewed_inputs``: a person's correction in force is computed from (``person_correction``).
PLAN_ENGINES = {"landed_cost": {"check": plan_check, "run": plan_run, "draft": draft_request,
                                "reviewed_inputs": True}}
