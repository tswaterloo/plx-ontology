"""Typed results of the landed-cost contract.

``compute_landed_cost`` hands out a JSON-shaped record (it is digested, stored and served as
is). Code that combines records (a purchase order's lines into a total, a plan's quote
entries) reads them through :class:`LandedOutcome` instead of indexing implicit keys: one
place states what a record must hold, and a record that breaks it is refused, never summed.

What a record must hold (the whole-set rule of ``operating.landed_cost``):

* ``computed``: no unknown input; ``total`` in the target currency and ``per_unit`` in
  target currency per the quantity unit, both Decimal text; no ``known_subtotal``.
* ``unknown``: at least one unknown input; ``total`` and ``per_unit`` None; a
  ``known_subtotal`` (if any) in the target currency, labelled partial.
* the total of a computed record is the per-unit cost times the quantity (``per_unit`` is
  a quotient of exact Decimals at 50 significant digits, so to ``PRODUCT_TOLERANCE``).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Sequence

from operating.cases import _MAX_DIGITS
from operating.landed_cost_base import LandedCostError

# the significant digits the case evaluator keeps its quotients to
QUOTIENT_DIGITS = _MAX_DIGITS


def product_agrees(per_unit: Decimal, quantity: Decimal, total: Decimal) -> bool:
    """``per_unit`` x ``quantity`` is ``total`` up to the rounding of the quotient
    ``total / quantity`` at ``QUOTIENT_DIGITS`` significant digits. The product and the
    difference are taken exactly (a context wide enough for every digit), never at the
    default 28 digits, which would refuse a valid long-tailed total."""
    with localcontext() as ctx:
        ctx.prec = 4 * QUOTIENT_DIGITS + 50
        gap = abs(per_unit * quantity - total)
        ulp = Decimal(1).scaleb(per_unit.adjusted() - QUOTIENT_DIGITS + 1) if per_unit else \
            Decimal(0)
        return gap <= ulp * abs(quantity) / 2


@dataclass(frozen=True)
class Money:
    """An exact amount and its unit (``USD``, or a price per unit such as ``USD/EA``)."""
    value: Decimal
    unit: str


@dataclass(frozen=True)
class LandedOutcome:
    status: str                       # "computed" | "unknown"
    target_currency: str
    quantity: Decimal | None          # None when the quantity itself is unknown
    quantity_unit: str
    total: Money | None
    per_unit: Money | None
    known_subtotal: Money | None
    unknown_inputs: tuple[str, ...]
    digest: str
    inputs: tuple["InputProvenance", ...] = ()

    @property
    def computed(self) -> bool:
        return self.status == "computed"

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "LandedOutcome":
        """The outcome of one ``compute_landed_cost`` record; a record that breaks the
        whole-set rule is refused (``LandedCostError("landed_record_inconsistent:...")``)."""
        def bad(why: str) -> LandedCostError:
            return LandedCostError(f"landed_record_inconsistent:{why}")

        if not isinstance(record, Mapping):
            raise bad("not_a_mapping")
        target = record.get("target_currency")
        status = record.get("status")
        digest = record.get("digest")
        if not isinstance(target, str) or not isinstance(digest, str):
            raise bad("no_target_currency_or_digest")
        quantity = record.get("quantity") or {}
        qunit = quantity.get("unit") if isinstance(quantity, Mapping) else None
        if not isinstance(qunit, str):
            raise bad("no_quantity_unit")
        qvalue = _decimal(quantity.get("value"), "quantity", bad) \
            if quantity.get("value") is not None else None
        unknowns = tuple(str(item.get("input")) for item in record.get("unknowns") or ()
                         if isinstance(item, Mapping))
        total = _money(record.get("total"), "total", bad)
        per_unit = _money(record.get("per_unit"), "per_unit", bad)
        partial = _money(record.get("known_subtotal"), "known_subtotal", bad)
        if status == "computed":
            if unknowns:
                raise bad("computed_with_unknown_inputs")
            if total is None or per_unit is None or partial is not None:
                raise bad("computed_without_total_and_per_unit")
            if total.unit != target:
                raise bad("total_not_in_target_currency")
            if per_unit.unit != f"{target}/{qunit}":
                raise bad("per_unit_not_target_per_quantity_unit")
            if qvalue is None or qvalue <= 0:
                raise bad("computed_without_a_positive_quantity")
            # per_unit is total / quantity, exact or rounded at 50 significant digits: their
            # product, taken exactly, gives the total back within half a unit in the last of
            # those digits per unit of quantity
            if not product_agrees(per_unit.value, qvalue, total.value):
                raise bad("total_is_not_per_unit_times_quantity")
        elif status == "unknown":
            if not unknowns:
                raise bad("unknown_without_an_unknown_input")
            if total is not None or per_unit is not None:
                raise bad("unknown_with_a_total")
            if partial is not None and partial.unit != target:
                raise bad("known_subtotal_not_in_target_currency")
        else:
            raise bad(f"status:{status}")
        inputs = tuple(InputProvenance.from_input(item, bad)
                       for item in record.get("inputs") or ())
        names = [item.name for item in inputs]
        if len(set(names)) != len(names):
            raise bad("input_named_twice")
        if not set(unknowns) <= set(names):
            raise bad("unknown_names_no_input")
        if status == "computed" and not inputs:
            raise bad("computed_without_inputs")
        return cls(status=status, target_currency=target, quantity=qvalue,
                   quantity_unit=qunit, total=total, per_unit=per_unit,
                   known_subtotal=partial, unknown_inputs=unknowns, digest=digest,
                   inputs=inputs)


def _decimal(value: Any, what: str, bad: Any) -> Decimal:
    if not isinstance(value, str):
        raise bad(f"{what}_not_decimal_text")
    try:
        out = Decimal(value)
    except InvalidOperation:
        raise bad(f"{what}_not_decimal_text") from None
    if not out.is_finite():
        raise bad(f"{what}_not_finite")
    return out


def _money(value: Any, what: str, bad: Any) -> Money | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("unit"), str):
        raise bad(f"{what}_shape")
    return Money(_decimal(value.get("value"), what, bad), value["unit"])


INPUT_KINDS = ("citation", "literal", "unknown", "derived")


@dataclass(frozen=True)
class InputProvenance:
    """Where one input of a landed cost comes from: a cited field (``handle``, ``field``), a
    literal, an unknown (who can answer it) or a value a named rule derived from cited records
    (``derived_from`` handles and ``rule``)."""
    name: str
    category: str
    kind: str
    handle: str | None = None
    field: str | None = None
    derived_from: tuple[str, ...] = ()
    rule: str | None = None
    who_can_answer: str | None = None

    @classmethod
    def from_input(cls, item: Any, bad: Any) -> "InputProvenance":
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise bad("input_shape")
        kind = item.get("kind")
        if kind not in INPUT_KINDS:
            raise bad(f"input_kind:{kind}")
        cited = item.get("citation") if kind == "citation" else None
        if kind == "citation" and not (isinstance(cited, Mapping)
                                       and isinstance(cited.get("handle"), str)
                                       and cited["handle"]):
            raise bad("citation_without_handle")
        derived = tuple(h for h in item.get("derived_from") or () if isinstance(h, str))
        if kind == "derived" and not (isinstance(item.get("rule"), str) and item["rule"]):
            raise bad("derived_without_rule")
        return cls(name=item["name"], category=str(item.get("category")), kind=kind,
                   handle=cited.get("handle") if cited else None,
                   field=cited.get("field") if cited else None,
                   derived_from=derived, rule=item.get("rule") if kind == "derived" else None,
                   who_can_answer=item.get("who_can_answer") if kind == "unknown" else None)


@dataclass(frozen=True)
class FreightQuoteScope:
    """What ``assess_freight_quote`` decided about one forwarder quote: the components it
    covers and leaves out, whether it stands as the total freight (then ``total`` is set,
    and no open reason is left), and whether duties and taxes are a separate input."""
    source: str
    includes: tuple[str, ...]
    excludes: tuple[str, ...]
    usable_as_total_freight: bool
    total: Money | None
    duties_and_taxes_separate: bool
    open_codes: tuple[str, ...]

    @classmethod
    def from_assessment(cls, assessed: Mapping[str, Any]) -> "FreightQuoteScope":
        def bad(why: str) -> LandedCostError:
            return LandedCostError(f"freight_assessment_inconsistent:{why}")
        if not isinstance(assessed, Mapping) or not isinstance(assessed.get("source"), str):
            raise bad("shape")
        codes = tuple(str(u.get("code")) for u in assessed.get("unknowns") or ()
                      if isinstance(u, Mapping))
        usable = assessed.get("usable_as_total_freight") is True
        total = _money(assessed.get("total_freight"), "total_freight", bad)
        if usable and codes:
            raise bad("usable_with_open_reasons")
        if usable and total is None:
            raise bad("usable_without_a_total")
        if not usable and total is not None:
            raise bad("total_without_being_usable")
        if total is not None and total.value <= 0:
            raise bad("total_not_positive")
        return cls(source=assessed["source"],
                   includes=tuple(assessed.get("includes") or ()),
                   excludes=tuple(assessed.get("excludes") or ()),
                   usable_as_total_freight=usable, total=total,
                   duties_and_taxes_separate=assessed.get("duties_and_taxes_separate") is True,
                   open_codes=codes)


@dataclass(frozen=True)
class LineSetCompleteness:
    """Whether the lines a run read are a purchase order's whole line set, the only set header
    freight and duty may be allocated over and a total stated for (BIZ-003): every linked line
    read with a price, an ordered and a received quantity, in a run not cut short, by a reader
    whose scope shows every line. ``why``: the first reason it is not (None when whole)."""
    whole: bool
    lines: int
    unproven: tuple[str, ...]
    withheld: int
    why: str | None

    @classmethod
    def of(cls, *, complete: bool, covered: bool, linked: set[str] | frozenset[str],
           line_ids: Sequence[str], unproven: Sequence[str], withheld: int = 0,
           price_withheld: str | None = None) -> "LineSetCompleteness":
        whole = bool(complete and covered and linked and not unproven)
        why = None
        if not whole:
            if unproven:
                why = (f"{len(unproven)} of {len(set(linked) | set(line_ids))} line(s) of the "
                       "purchase order were not read with a price, an ordered and a received "
                       "quantity" + (f" ({price_withheld})" if price_withheld else ""))
            elif not complete:
                why = ("the run was truncated or cut by its step budget, so the line set is "
                       "not proven complete")
            elif withheld:
                why = (f"{withheld} line(s) of the purchase order carry a marking this reader "
                       "does not hold and are withheld from it, so the lines it reads are not "
                       "the whole set")
            elif not covered:
                why = ("this reader's read scope is not proven to show every line of the "
                       "purchase order (a marking or a source grant it lacks may withhold one), "
                       "so the line set is not proven complete")
            else:
                why = "no line of the purchase order was reached"
        return cls(whole=whole, lines=len(set(linked) | set(line_ids)),
                   unproven=tuple(unproven), withheld=withheld, why=why)
