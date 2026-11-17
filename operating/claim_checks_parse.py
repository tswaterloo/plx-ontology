"""Parsing value claims (``operating.claim_checks``): numbers and decimals as written, value
claims and their calculation inputs, expressions and their monomials, and ``dig``."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from ontology.core import ValidationError
from ontology.measure import exact_normal

from .answer_contract import NUMERIC_OPS, PAIR_OPS, calendar_day
from .cases import parse_unit

from .claim_checks_grammar import (
    ADD_DAYS_OP, _CALC_ID, _CLAIM_KEYS, COUNT_OPS, _CURRENCY, DAY_OPS, _EXPRESSION_FIGURE,
    EXPRESSION_INPUT, _EXPRESSION_INPUT_KEYS, _FIELD, _ISO_DAY, MAX_CLAIMS,
    MAX_EXPRESSION_CHARS, MAX_OP_INPUTS, MAX_QUOTE_CHARS, MAX_VALUE_CHARS, _OP_INPUT_KEYS, OPS,
    PAGE_TEXT, _page_unit, _PLAIN_NUMBER, _ROLE_SHAPE, ROLES, ROUND_TO_CENTS, ROUNDED_PLACES,
    _SCALES_OF_SUFFIX, _THOUSANDS,
)


def rounded_from(number: Decimal, values: Iterable[Any]) -> bool:
    """Whether ``number`` as written is a value of ``values`` rounded to its own 2-4
    decimal places (``ROUNDED_PLACES``), never the other way round: the value carries
    more places than the written figure and rounds half up to exactly it."""
    if not isinstance(number, Decimal) or not number.is_finite():
        return False
    places = -number.as_tuple().exponent
    if not ROUNDED_PLACES[0] <= places <= ROUNDED_PLACES[1]:
        return False
    target = abs(number)
    step = Decimal(1).scaleb(-places)
    for value in values:
        if not isinstance(value, Decimal) or not value.is_finite() \
                or -value.as_tuple().exponent <= places:
            continue
        if abs(value).quantize(step, rounding=ROUND_HALF_UP) == target:
            return True
    return False


def to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = repr(value)
    text = str(value).strip()
    if "," in text:
        # "1,509" is 1509; "1,5" (a decimal comma) or "12,34" is no number, never 15.
        if not _THOUSANDS.fullmatch(text):
            return None
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def number_value(match: re.Match[str]) -> Decimal:
    """The value of a ``NUMBER`` match at its written scale: ``1,509`` is 1509, ``-6``
    is -6, ``0.25%`` is 0.0025, ``10k`` is 10000 and ``2.5M`` 2500000."""
    body = (match.group("int") or "0").replace(",", "") + (match.group("frac")
                                                        or match.group("lead") or "")
    number = Decimal(body)
    if match.group("sign") in ("-", "−") and number != 0:
        number = -number
    scale = (match.group("scale") or "").strip(" \u00a0").lower()
    return number * _SCALES_OF_SUFFIX[scale] if scale else number


def _plain(number: Decimal) -> str:
    """``number`` as plain decimal text, every significant digit kept (QUERY-003)."""
    text = format(exact_normal(number), "f")
    return text[:-2] if text.endswith(".0") else text


def parse_value_claims(raw: Any, handles: Sequence[str],
                       calculations: Sequence[str]) -> list[dict[str, Any]]:
    """Validate and normalise structured value claims (shape only; checked on read).

    Each claim cites one of ``handles`` (the item's own citations) with a ``field``,
    one recorded ``calculation`` id, or computes its value over cited rows (``op``
    ``count``, ``sum``, ``avg``, ``min``, ``max``, ``difference`` or ``ratio`` ``of``
    inputs, each one of ``handles``; any but a count's with a ``field``, a difference's or
    ratio's exactly two;
    an ``expression``'s each a figure ``value`` read on its ``field``, the expression
    naming them ``x0``, ``x1``, ..., every one, and holding no number of its own).
    Raises ``ValidationError`` with a stable code.
    """
    if isinstance(raw, (str, Mapping)) or not isinstance(raw, Sequence):
        raise ValidationError("invalid_value_claims")
    if len(raw) > MAX_CLAIMS:
        raise ValidationError("too_many_value_claims")
    out = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or set(item) - _CLAIM_KEYS or "value" not in item:
            raise ValidationError(f"invalid_value_claim:{index}")
        value = item["value"]
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValidationError(f"value_claim_value_must_be_integer_or_string:{index}")
        text = str(value).strip()
        if not text:
            raise ValidationError(f"invalid_value_claim_value:{index}")
        if len(text) > MAX_VALUE_CHARS:
            raise ValidationError(f"value_claim_value_too_long:{index}")
        if "expression" in item and item.get("op") != "expression":
            raise ValidationError(f"invalid_value_claim:{index}")
        if "day" in item and (item.get("op") not in DAY_OPS or not isinstance(item["day"], str)
                              or not _ISO_DAY.fullmatch(item["day"])
                              or calendar_day(item["day"]) is None):
            raise ValidationError(f"invalid_value_claim_day:{index}")
        claim: dict[str, Any] = {"value": text,
                                 "kind": "number" if _PLAIN_NUMBER.match(text) else "text"}
        if sum(key in item for key in ("citation", "calculation", "op")) != 1 \
                or ("op" in item) != ("of" in item):
            raise ValidationError(f"value_claim_needs_citation_or_calculation:{index}")
        if "quote" in item and "citation" not in item:
            # A computed value is no passage of one record.
            raise ValidationError(f"invalid_value_claim_quote:{index}")
        if "op" in item:
            if "field" in item:
                raise ValidationError(f"invalid_value_claim_field:{index}")
            claim.update(op=item["op"], of=_op_inputs(item, handles, index))
            if item["op"] == ADD_DAYS_OP:
                # a calendar day, written as one (YYYY-MM-DD)
                if not _ISO_DAY.fullmatch(text) or calendar_day(text) is None:
                    raise ValidationError(f"invalid_value_claim_value:{index}")
            elif claim["kind"] != "number":
                raise ValidationError(f"invalid_value_claim_value:{index}")
            if "day" in item:
                claim["day"] = item["day"]
            if item["op"] == "expression":
                claim["expression"] = _expression_shape(
                    item, len(claim["of"]), index, share=_percent_unit(item.get("unit")))
        elif "citation" in item:
            if item["citation"] not in handles:
                raise ValidationError(f"value_claim_citation_not_cited:{index}")
            field = item.get("field")
            if not isinstance(field, str) or not (_FIELD.match(field) or field == PAGE_TEXT):
                raise ValidationError(f"invalid_value_claim_field:{index}")
            claim.update(citation=item["citation"], field=field)
            if "quote" in item:
                quote = item["quote"]
                if not isinstance(quote, str) or not quote.strip() \
                        or len(quote) > MAX_QUOTE_CHARS:
                    raise ValidationError(f"invalid_value_claim_quote:{index}")
                claim["quote"] = quote.strip()
        else:
            if "field" in item:
                raise ValidationError(f"invalid_value_claim_field:{index}")
            calc = item["calculation"]
            if not isinstance(calc, str) or not _CALC_ID.match(calc) or calc not in calculations:
                raise ValidationError(f"value_claim_calculation_not_found:{index}")
            claim["calculation"] = calc
        role = item.get("role")
        if role is not None:
            if not isinstance(role, str) or not _ROLE_SHAPE.match(role) or (
                    "citation" in claim and role not in ROLES) or (
                    claim.get("op") in COUNT_OPS and role in ROLES):
                raise ValidationError(f"invalid_value_claim_role:{index}")
            claim["role"] = role
        unit = item.get("unit")
        if unit is not None:
            try:
                if not isinstance(unit, str) or len(unit) > 100:
                    raise ValidationError("unit")
                parse_unit(unit)
            except ValidationError:
                raise ValidationError(f"invalid_value_claim_unit:{index}") from None
            claim["unit"] = unit.strip()
        currency = item.get("currency")
        if currency is not None:
            # A sum (an average, a min, a max, a difference), or one cited number (not a
            # page figure), states a currency; a ratio of money is none.
            # An expression over money states its currency too (``_check_expression``);
            # a percent is no money.
            money = claim.get("op") in NUMERIC_OPS and claim["op"] != "ratio" or (
                claim.get("op") == "expression" and not _percent_unit(claim.get("unit"))) or (
                claim["kind"] == "number" and claim.get("field") not in (None, PAGE_TEXT))
            if not money or not isinstance(currency, str) or not _CURRENCY.match(currency):
                raise ValidationError(f"invalid_value_claim_currency:{index}")
            claim["currency"] = currency
        out.append(claim)
    return out


def _op_inputs(item: Mapping[str, Any], handles: Sequence[str], index: int
               ) -> list[dict[str, str]]:
    """The cited inputs of an ``op`` claim, shape only: 1 to ``MAX_OP_INPUTS``, each one of
    ``handles`` (a ``difference``'s or ``ratio``'s exactly two), a ``NUMERIC_OPS`` op's or a
    count's none twice (an expression's figure may repeat: ``_check_expression`` judges
    it); a ``NUMERIC_OPS`` op's each with a ``field`` (a row column) and an optional
    ``unit``, a count's a citation alone, an expression's a figure (``_expression_input``)."""
    op, inputs = item["op"], item["of"]
    if op not in OPS or isinstance(inputs, (str, Mapping)) or not isinstance(
            inputs, Sequence) or not 1 <= len(inputs) <= MAX_OP_INPUTS \
            or (op in PAIR_OPS and len(inputs) != 2) \
            or (op in DAY_OPS and len(inputs) != (1 if "day" in item else 2)):
        raise ValidationError(f"invalid_value_claim_op:{index}")
    keys = {"count": frozenset({"citation"}), "count_distinct": frozenset({"citation"}),
            "expression": _EXPRESSION_INPUT_KEYS}.get(op, _OP_INPUT_KEYS)
    out, seen = [], set()
    for given in inputs:
        if not isinstance(given, Mapping) or set(given) - keys:
            raise ValidationError(f"invalid_value_claim_op:{index}")
        if given.get("citation") not in handles:
            raise ValidationError(f"value_claim_citation_not_cited:{index}")
        entry = {"citation": given["citation"]}
        if op == "expression":
            # An input listed twice as written is one printed figure used in two roles
            # (A12: one pack size dividing two kit lines): it reaches the check as that
            # figure again (``_check_expression``'s aliasing by place), which still refuses
            # a figure counted twice once multiplied out, never here by its copy.
            out.append(_expression_input(given, index))
            continue
        if op in DAY_OPS:
            field = given.get("field")
            if not isinstance(field, str) or not _FIELD.match(field) or "unit" in given:
                raise ValidationError(f"invalid_value_claim_field:{index}")
            entry["field"] = field
        if op in NUMERIC_OPS:
            field, unit = given.get("field"), given.get("unit")
            if not isinstance(field, str) or not _FIELD.match(field):
                raise ValidationError(f"invalid_value_claim_field:{index}")
            entry["field"] = field
            if unit is not None:
                try:
                    if not isinstance(unit, str) or len(unit) > 100:
                        raise ValidationError("unit")
                    parse_unit(unit)
                except ValidationError:
                    raise ValidationError(f"invalid_value_claim_unit:{index}") from None
                entry["unit"] = unit.strip()
        if (entry["citation"], entry.get("field")) in seen:
            raise ValidationError(f"value_claim_duplicate_input:{index}")
        seen.add((entry["citation"], entry.get("field")))
        out.append(entry)
    return out


def _expression_input(given: Mapping[str, Any], index: int) -> dict[str, str]:
    """One expression input, shape only: its ``citation``, a ``field`` (a row column or a
    page's ``text``), the figure as written (``value``: digits, an optional thousands comma
    grouping and fraction, ``%`` for a printed percent), an optional ``quote`` and an
    optional ``unit`` (``%`` alone beside a percent)."""
    field, figure = given.get("field"), given.get("value")
    if not isinstance(field, str) or not (_FIELD.match(field) or field == PAGE_TEXT):
        raise ValidationError(f"invalid_value_claim_field:{index}")
    if isinstance(figure, bool) or not isinstance(figure, (int, str)) \
            or not _EXPRESSION_FIGURE.match(str(figure).strip()):
        raise ValidationError(f"value_claim_expression_input_value:{index}")
    entry = {"citation": given["citation"], "field": field, "value": str(figure).strip()}
    if "quote" in given:
        quote = given["quote"]
        if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
            raise ValidationError(f"invalid_value_claim_quote:{index}")
        entry["quote"] = quote.strip()
    unit = given.get("unit")
    if unit is not None:
        percent = isinstance(unit, str) and _page_unit(unit) == "percent"
        try:
            if not isinstance(unit, str) or len(unit) > 100:
                raise ValidationError("unit")
            if not percent:
                parse_unit(unit)
        except ValidationError:
            raise ValidationError(f"invalid_value_claim_unit:{index}") from None
        if entry["value"].endswith("%") and not percent:
            raise ValidationError(f"invalid_value_claim_unit:{index}")
        entry["unit"] = unit.strip()
    return entry


# An expression expanded into monomials (``_expanded``) holds at most this many terms;
# a longer expansion is unreadable (fail closed).
_MAX_MONOMIALS = 512


def _expanded(node: Any, quotients: list[dict]) -> dict[tuple, int] | None:
    """An expression's AST as a sum of monomials: {monomial: integer coefficient}, a
    monomial the sorted multiset of its factors (``(input, +1)`` multiplied, ``(input,
    -1)`` divided by), terms that cancel kept with coefficient 0. A divisor that is no
    single monomial is one opaque factor named by its own expansion, which is appended to
    ``quotients`` to be judged too. None for anything else (a call, a number, a power) or
    an expansion past ``_MAX_MONOMIALS``."""
    import ast
    if isinstance(node, ast.Name):
        return {((node.id, 1),): 1}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _expanded(node.operand, quotients)
        if inner is None or isinstance(node.op, ast.UAdd):
            return inner
        return {mono: -coef for mono, coef in inner.items()}
    if not isinstance(node, ast.BinOp):
        return None
    left = _expanded(node.left, quotients)
    right = _expanded(node.right, quotients)
    if left is None or right is None:
        return None
    out: dict[tuple, int] = dict(left)
    if isinstance(node.op, (ast.Add, ast.Sub)):
        sign = 1 if isinstance(node.op, ast.Add) else -1
        for mono, coef in right.items():
            out[mono] = out.get(mono, 0) + sign * coef
        return out if len(out) <= _MAX_MONOMIALS else None
    if isinstance(node.op, ast.Div):
        if len(right) == 1 and abs(next(iter(right.values()))) == 1:
            (mono, coef), = right.items()
            right = {tuple((factor, -power) for factor, power in mono): coef}
        else:
            quotients.append(right)
            right = {((("quotient", tuple(sorted(right.items(), key=repr))), -1),): 1}
    elif not isinstance(node.op, ast.Mult):
        return None
    if len(left) * len(right) > _MAX_MONOMIALS:
        return None
    out = {}
    for mono_l, coef_l in left.items():
        for mono_r, coef_r in right.items():
            mono = tuple(sorted(mono_l + mono_r, key=repr))
            out[mono] = out.get(mono, 0) + coef_l * coef_r
    return out


def _monomial_inputs(mono: tuple) -> list[str]:
    """Every input a monomial names, inside an opaque divisor too."""
    names: list[str] = []
    for factor, _power in mono:
        if isinstance(factor, str):
            names.append(factor)
        else:
            for inner, _coef in factor[1]:
                names += _monomial_inputs(inner)
    return names


def _repeated_in_term(body: str) -> bool:
    """Whether an expression counts an input more than once where it may not, judged on
    its expansion into a sum of monomials (``_expanded``), so no factoring hides a double
    count: ``(x0 + x1) * x2 + x0 * x2`` is 2·x0·x2 + x1·x2, kit A twice. Refused when a
    monomial's coefficient is not +1 or -1 (``x0 + x0``, ``x0 * x1 + x0 * (x1 + x2)``; a
    0 is a term that cancels itself, ``(x0 + x1) - x0 - x1``), when one monomial names an
    input twice (``x0 / x0``, ``x0 * (x1 - x0)``, ``x0 / (x0 + x1)``), when an input
    standing in several monomials stands in one with nothing else (``x0 + x0 * x1``), or
    when the expression cannot be read as such a sum (fail closed). One input in several
    monomials with different other inputs is one figure applied to several lines
    (``x0 / x1 + x2 / x1``: one rate, two quantities; ``(x0 + x1) * x2``)."""
    import ast
    try:
        tree = ast.parse(body.strip(), mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return True
    quotients: list[dict] = []
    try:
        total = _expanded(tree, quotients)
    except RecursionError:
        return True
    if total is None:
        return True
    for poly in (total, *quotients):
        for mono, coef in poly.items():
            named = _monomial_inputs(mono)
            if abs(coef) != 1 or len(named) != len(set(named)):
                return True
    rests: dict[str, list[frozenset[str]]] = {}
    for mono in total:
        named = _monomial_inputs(mono)
        for name in named:
            rests.setdefault(name, []).append(frozenset(named) - {name})
    return any(len(seen) > 1 and not all(seen) for seen in rests.values())


def _percent_unit(unit: Any) -> bool:
    """Whether a claim's ``unit`` names a percent (``percent``, ``pct``)."""
    return isinstance(unit, str) and _page_unit(unit) == "percent"


def _share_parts(body: str) -> tuple[str, str] | None:
    """(numerator, denominator) of an expression that is one quotient at its top
    (``(x0 + x1) / x0``), or None."""
    import ast
    try:
        tree = ast.parse(body.strip(), mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    if not isinstance(tree, ast.BinOp) or not isinstance(tree.op, ast.Div):
        return None
    return ast.unparse(tree.left), ast.unparse(tree.right)


def _expression_shape(item: Mapping[str, Any], count: int, index: int,
                      share: bool = False) -> str:
    """An expression claim's ``expression``, shape only: at most ``MAX_EXPRESSION_CHARS``,
    optionally ``round_to_cents(...)`` around the whole, naming each of its ``count``
    inputs ``x0`` ... at least once and nothing else, with no number of its own (every
    operand is a figure a cited text holds). An input may stand in several additive
    terms (one rate applied to several quantities, ``x0 / x1 + x2 / x1``) but never twice
    in one term (``_repeated_in_term``: no ``x0 / x0`` builds a constant); the result
    must then move with every repeated input (``_check_expression``), so no ``x0 - x0``
    cancels one away. A figure printed twice is still two inputs, each quoted where it
    is printed. The evaluator decides the rest when the claim is checked."""
    expression = item.get("expression")
    if not isinstance(expression, str) or not expression.strip() \
            or len(expression) > MAX_EXPRESSION_CHARS:
        raise ValidationError(f"invalid_value_claim_expression:{index}")
    rounded = ROUND_TO_CENTS.match(expression)
    body = rounded.group(1) if rounded else expression
    rest = EXPRESSION_INPUT.sub(" ", body)
    if re.search(r"\d", rest):
        raise ValidationError(f"value_claim_expression_literal:{index}")
    named = EXPRESSION_INPUT.findall(body)
    names = set(named)
    if re.search(r"[A-Za-z_]", rest) or names - {f"x{n}" for n in range(count)}:
        raise ValidationError(f"value_claim_expression_name:{index}")
    parts = _share_parts(body) if share else None
    if share and parts is None:
        # A percent states a share: a figure over a whole, never a sum scaled by 100.
        raise ValidationError(f"value_claim_expression_percent:{index}")
    if parts is not None:
        # A percent claim states a share, ``N / D`` x 100: the part over its whole, where
        # the whole may hold figures the part holds too (margin over revenue: (x0 + x1) /
        # x0). Each side is judged on its own, and the part names a figure the whole
        # does not, so no x0 / x0 builds a constant 100.
        top, bottom = (EXPRESSION_INPUT.findall(part) for part in parts)
        if any(len(side) != len(set(side)) and _repeated_in_term(part)
               for side, part in zip((top, bottom), parts)) or not set(top) - set(bottom):
            raise ValidationError(f"value_claim_expression_repeated_input:{index}")
    elif len(named) != len(names) and _repeated_in_term(body):
        raise ValidationError(f"value_claim_expression_repeated_input:{index}")
    if len(names) != count:
        raise ValidationError(f"value_claim_expression_unused_input:{index}")
    return expression.strip()


def expression_inputs(claim: Mapping[str, Any]) -> list[dict[str, Any]]:
    """An expression claim's inputs as the cited value claims the gate checks each by
    (``_check_value_claim``): the figure without its ``%`` and, for a percent, unit
    ``percent`` (the page must print it as one); none for any other claim."""
    if claim.get("op") != "expression":
        return []
    out = []
    for given in claim.get("of") or ():
        figure = given["value"]
        percent = figure.endswith("%") or _page_unit(given.get("unit") or "") == "percent"
        sub = {"value": figure.rstrip("%"), "kind": "number", "citation": given["citation"],
               "field": given["field"]}
        if "quote" in given:
            sub["quote"] = given["quote"]
        if percent:
            sub["unit"] = "percent"
        elif given.get("unit"):
            sub["unit"] = given["unit"]
        out.append(sub)
    return out


def _withheld(resolved: Any, field: str) -> bool:
    """Whether ``field`` (or the column it is in) is one a marking withheld from the
    reader's row (its ``withheld_fields``)."""
    withheld = resolved.get("withheld_fields") if isinstance(resolved, Mapping) else None
    return bool(withheld) and any(field == path or field.startswith(path + ".")
                                  for path in withheld if isinstance(path, str))


def dig(resolved: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    value: Any = resolved
    for part in field.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value
