"""Money and quantity values that keep their dimensions through every calculation.

A figure on its own (``Decimal("12.5")``) loses what makes it comparable: the currency of
an amount, the unit of a quantity, the unit a price is per, the tax basis of an amount
and the role of a quantity (planned, filled ...). ``Measure`` keeps them together with
the amount, and every combination here refuses what the units fold (``ontology/units.py``)
does not make one dimension:

- two values combine (sum, min, max, compare, group) only when both dimensions are known
  and equal: an unknown currency, unit or denominator is never "compatible" with anything,
  not even with another unknown (``comparable``);
- a unit price is a rate and is never summed (``total`` refuses it);
- a total over inputs any of which is unread, unknown or of another dimension is never
  exact: ``total`` says ``partial`` (with how many are missing) or ``mixed`` (one subtotal
  per dimension), and a partial subtotal is a lower bound (``at_least``) only when the
  caller states the values cannot be negative and none read is (a credit memo is);
- nothing is converted: kg and g, CAD and USD stay apart.

The amount is a ``Decimal`` read exactly from its text (``decimal_of``): a float is read
through its shortest repr, never its binary expansion, and NaN, infinities and booleans
are not amounts.

``float_residue`` is the one rule for a binary float's noise in a shown figure
(``116.25000000000001`` is ``116.25``): only text that is exactly the shortest repr of a
double, at least 15 significant digits long, within a few units in the last place of a
figure with fewer digits. A real figure with many places (``100000000.1234567``,
``1234.5678901``, a 29-digit decimal) is never touched. The app's ``withoutFloatNoise``
(``app/src/lib/format.ts``) applies the same rule.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable, Mapping

from ontology.units import currency_code, unit_token

KINDS = ("amount", "unit_price", "quantity", "number")


@dataclass(frozen=True)
class Dimension:
    """What a figure is in: ``kind`` (``KINDS``), an ISO currency (money), a folded unit
    (a quantity's unit, or the unit a unit price is per), the tax basis (money) and the
    quantity role. ``None`` is unknown, never a wildcard."""
    kind: str
    currency: str | None = None
    unit: str | None = None
    basis: str | None = None
    role: str | None = None

    def known(self) -> bool:
        """Whether every part this kind needs is known: an amount its currency, a unit
        price its currency and denominator, a quantity its unit."""
        if self.kind == "amount":
            return self.currency is not None
        if self.kind == "unit_price":
            return self.currency is not None and self.unit is not None
        if self.kind == "quantity":
            return self.unit is not None
        return self.kind == "number"

    def label(self) -> str:
        """``CAD``, ``CAD per kg``, ``kg``, ``CAD (excl_tax)``; ``unknown currency`` ..."""
        if self.kind in ("amount", "unit_price"):
            text = self.currency or "unknown currency"
            if self.kind == "unit_price":
                text += f" per {self.unit or 'unknown unit'}"
            if self.basis and self.basis != "unknown":
                text += f" ({self.basis})"
            return text
        if self.kind == "quantity":
            return self.unit or "unknown unit"
        return ""


def dimension(kind: str, *, currency: Any = None, unit: Any = None, basis: str | None = None,
              role: str | None = None) -> Dimension:
    """A ``Dimension`` from source cells: the currency read by ``currency_code`` (three
    letters, or a written form whose currency is not in doubt; anything else unknown) and
    the unit folded by ``unit_token``. An unknown kind is refused (``ValueError``)."""
    if kind not in KINDS:
        raise ValueError(f"unknown measure kind {kind!r}")
    code = None
    if kind in ("amount", "unit_price"):
        code = currency_code(currency)[0] if currency is not None else None
    folded = unit_token(unit) if kind in ("unit_price", "quantity") else None
    return Dimension(kind, code, folded, basis if kind in ("amount", "unit_price") else None,
                     role if kind == "quantity" else None)


_PLAIN = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def decimal_of(raw: Any) -> Decimal | None:
    """The exact amount a cell holds: a Decimal or int as is, a float through its
    shortest repr (``0.1`` is ``0.1``), plain decimal text (no grouping commas, no
    currency); None for anything else, NaN, infinities and booleans included."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(repr(raw)) if math.isfinite(raw) else None
    if isinstance(raw, str):
        text = raw.strip()
        if not _PLAIN.fullmatch(text):
            return None
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
        return value if value.is_finite() else None
    return None


@dataclass(frozen=True)
class Measure:
    """One figure with its dimension; ``amount`` None is a value that could not be read."""
    amount: Decimal | None
    dim: Dimension

    def known(self) -> bool:
        return self.amount is not None and self.dim.known()


def measure(raw: Any, kind: str, **dims: Any) -> Measure:
    """``Measure(decimal_of(raw), dimension(kind, **dims))``."""
    return Measure(decimal_of(raw), dimension(kind, **dims))


def comparable(a: Dimension | Measure, b: Dimension | Measure) -> bool:
    """Whether two figures may be compared or combined: both dimensions known and equal.
    A missing unit or currency is never compatible, not even with another missing one."""
    da = a.dim if isinstance(a, Measure) else a
    db = b.dim if isinstance(b, Measure) else b
    return da.known() and db.known() and da == db


@dataclass(frozen=True)
class Total:
    """A sum that says how complete it is.

    ``state``: ``exact`` (every input read, one dimension), ``partial`` (one dimension,
    ``missing`` inputs unread or of unknown dimension: ``amount`` is the subtotal of what
    was read, a lower bound only when ``at_least``), ``mixed`` (several dimensions:
    ``amount`` None, ``parts`` one subtotal each), ``refused`` (unit prices are never
    summed) or ``empty`` (nothing to sum)."""
    state: str
    amount: Decimal | None
    dim: Dimension | None
    counted: int
    missing: int
    at_least: bool = False
    parts: Mapping[Dimension, Decimal] = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        return self.state == "exact"


def exact_sum(numbers: Iterable[Decimal]) -> Decimal:
    """The sum of ``numbers`` with every digit kept: the context precision is set from the
    operands (their widest span of digits and how many there are), never the ambient 28
    digits, so a long source value or a sum needing one more digit is never rounded. A
    NaN or infinity sums as before."""
    items = list(numbers)
    finite = [n for n in items if n.is_finite()]
    if not items or len(finite) < len(items):
        return sum(items, Decimal(0))
    top = max((n.adjusted() for n in finite if n), default=0)
    low = min(min(int(n.as_tuple().exponent), 0) for n in finite)
    with localcontext() as ctx:
        ctx.prec = max(ctx.prec, top - low + len(str(len(items))) + 2)
        return sum(items, Decimal(0))


def exact_normal(number: Decimal) -> Decimal:
    """``number`` without trailing zeros, every significant digit kept (``normalize``
    under the ambient context rounds past its 28 digits)."""
    with localcontext() as ctx:
        ctx.prec = max(ctx.prec, len(number.as_tuple().digits))
        return number.normalize(ctx)


def total(measures: Iterable[Measure], *, expected: int | None = None,
          nonnegative: bool = False) -> Total:
    """The sum of ``measures``. ``expected`` is how many inputs the whole holds (more
    than given are missing: a first page, a truncated read). ``nonnegative`` is the
    caller's statement that no input can be below zero (a count, a stock level); only
    then is a partial subtotal a lower bound."""
    items = list(measures)
    if any(m.dim.kind == "unit_price" for m in items):
        return Total("refused", None, None, 0, 0)
    read = [m for m in items if m.known()]
    missing = len(items) - len(read)
    if expected is not None and expected > len(items):
        missing += expected - len(items)
    grouped: dict[Dimension, list[Decimal]] = {}
    for m in read:
        grouped.setdefault(m.dim, []).append(m.amount)
    parts = {dim: exact_sum(amounts) for dim, amounts in grouped.items()}
    if not parts:
        return Total("empty" if not missing else "partial", None, None, 0, missing)
    if len(parts) > 1:
        return Total("mixed", None, None, len(read), missing, parts=parts)
    (dim, amount), = parts.items()
    if missing:
        at_least = nonnegative and all(m.amount >= 0 for m in read)
        return Total("partial", amount, dim, len(read), missing, at_least, parts)
    return Total("exact", amount, dim, len(read), 0, parts=parts)


def extremes(measures: Iterable[Measure]) -> dict[Dimension, tuple[Decimal, Decimal]]:
    """The lowest and highest amount of each known dimension; an unknown one is left out
    (never the extreme of anything)."""
    out: dict[Dimension, tuple[Decimal, Decimal]] = {}
    for m in measures:
        if not m.known():
            continue
        lo, hi = out.get(m.dim, (m.amount, m.amount))
        out[m.dim] = (min(lo, m.amount), max(hi, m.amount))
    return out


def plain(value: Decimal) -> str:
    """A Decimal as plain text, exact (no exponent, no trailing zeros past the point)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_measure(m: Measure) -> str:
    """``12,345.5 CAD``, ``4.25 CAD per kg``, ``10 kg``; an unread amount is ``unknown``
    and an unknown dimension is said (``12.5 unknown currency``), never dropped."""
    if m.amount is None:
        return "unknown"
    text = plain(m.amount)
    sign = "-" if text.startswith("-") else ""
    whole, _, frac = text.lstrip("-").partition(".")
    shown = f"{sign}{int(whole):,}" + (f".{frac}" if frac else "")
    label = m.dim.label()
    return f"{shown} {label}" if label else shown


def money_text(raw: Any, currency: Any) -> str | None:
    """An amount as stated to a reader: ``1,234.50 CAD``, at least two places and every
    place the amount holds (``0.125 CAD`` is never shown as ``0.13``), a binary float's
    noise dropped (``float_residue``). None when the amount is not a readable number or its
    currency is not known (``currency_code``): a figure without its currency is never
    stated."""
    m = measure(raw, "amount", currency=currency)
    if not m.known():
        return None
    text = plain(m.amount)
    text = float_residue(text) or text
    sign = "-" if text.startswith("-") else ""
    whole, _, frac = text.lstrip("-").partition(".")
    return f"{sign}{int(whole):,}.{frac.ljust(2, '0')} {m.dim.currency}"


_RESIDUE = re.compile(r"-?\d+\.\d+")


def float_residue(text: str) -> str | None:
    """The figure a binary float's noise stands for (``"116.25000000000001"`` ->
    ``"116.25"``, ``"0.30000000000000004"`` -> ``"0.3"``), else None. Only text that is
    exactly the shortest repr of a double with at least 15 significant digits counts, and
    only a figure with fewer digits within 4 units in the last place of it: a real figure
    with many places, or more digits than a double holds, is not noise."""
    if not _RESIDUE.fullmatch(text):
        return None
    digits = text.lstrip("-").replace(".", "").lstrip("0")
    if len(digits) < 15:
        return None
    number = float(text)
    if not math.isfinite(number) or repr(number) != text:
        return None
    slack = 4 * math.ulp(number)
    with localcontext() as ctx:
        ctx.prec = 60
        exact = Decimal(text)
        places = len(text.partition(".")[2])
        for keep in range(0, places):
            try:
                near = exact.quantize(Decimal(1).scaleb(-keep))
            except InvalidOperation:
                return None
            if abs(float(near) - number) <= slack:
                return format(near, "f")
    return None


__all__ = ["KINDS", "Dimension", "Measure", "Total", "comparable", "decimal_of", "dimension",
           "extremes", "float_residue", "format_measure", "measure", "money_text", "plain",
           "total"]
