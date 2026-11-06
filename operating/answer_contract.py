"""The answer contract an agent session ends with (``plx-eval-answer/v1``): its parse and
shape checks, and what makes a tool an action. Product code: the app's Ask reads a
session's answer with these exactly as the scorer does (``evals.score`` uses them)."""

from __future__ import annotations

from decimal import (ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, InvalidOperation,
                     localcontext)
import json
import re
from typing import Any, Mapping, Sequence


ANSWER_FORMAT = "plx-eval-answer/v1"


ANSWERABILITY = ("answerable", "partial", "blocked")


# A tool that proposes, approves, dispatches or executes an effect (``plx.act`` is the
# action door, MC writes included). Combined with the host's read-only annotation: a
# read-only tool is never an action.
ACTION_TOOL = re.compile(r"(?:^|[._])(?:act(?:$|[._])|propose|approve|dispatch|execute|"
                         r"submit|send|action_(?:propose|request|decide|register))", re.I)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


MAX_VALUES = 100


MAX_CITATIONS = 8


# Computed values over one numeric field of each cited cell, recomputed by the claim gate
# (``operating.claim_checks``, a value claim's ``op``) and the eval scorer (``evals.score``,
# an answer value's ``calculation``) through ``computed_value`` / ``computed_claim``, so
# both reach one verdict on every value. ``avg``/``min``/``max`` take 1 to
# ``MAX_CALCULATION_INPUTS`` inputs; ``difference`` (``x0 - x1``) and ``ratio``
# (``x0 / x1``) exactly two, in order.
STATISTIC_OPS = ("avg", "min", "max")
PAIR_OPS = ("difference", "ratio")
NUMERIC_OPS = ("sum", *STATISTIC_OPS, *PAIR_OPS)
# The ops whose value may be written rounded (``computed_claim``).
ROUNDED_OPS = ("avg", "ratio")


CALCULATION_OPS = ("sum", "count", "count_distinct", "equal", "expression", *STATISTIC_OPS,
                   *PAIR_OPS)


MAX_CALCULATION_INPUTS = 100  # the oracle's chain cap (evals/oracle/families.py MAX_CHAIN)


class ZeroDenominator(ArithmeticError):
    """A ``ratio`` whose denominator (its second input) is zero as read."""


def _places(number: Decimal) -> int:
    exponent = number.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def computed_value(op: str, numbers: Sequence[Decimal], percent: bool = False) -> Decimal:
    """The exact value of a ``NUMERIC_OPS`` op over its inputs' numbers (Decimal, in input
    order; at 50 significant digits where a quotient does not terminate): ``sum``,
    ``avg`` (sum / count), ``min``/``max`` (one input's value), ``difference`` (x0 - x1),
    ``ratio`` (x0 / x1; times 100 when stated as a ``percent``). Raises
    ``ZeroDenominator`` for a ratio over zero and ``ValueError`` for anything else it
    cannot compute (no inputs, a pair op without two, an unknown op)."""
    numbers = list(numbers)
    if op not in NUMERIC_OPS or not numbers or (op in PAIR_OPS and len(numbers) != 2) \
            or any(not isinstance(n, Decimal) or not n.is_finite() for n in numbers):
        raise ValueError(f"not_computable:{op}")
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        if op == "sum":
            return sum(numbers, Decimal(0))
        if op == "avg":
            return sum(numbers, Decimal(0)) / len(numbers)
        if op == "min":
            return min(numbers)
        if op == "max":
            return max(numbers)
        if op == "difference":
            return numbers[0] - numbers[1]
        if numbers[1] == 0:
            raise ZeroDenominator("ratio_zero_denominator")
        return numbers[0] / numbers[1] * (100 if percent else 1)


def computed_claim(op: str, claimed: Any, numbers: Sequence[Decimal], percent: bool = False
                   ) -> tuple[bool, Decimal]:
    """(holds, the value as recomputed) of a claimed value (a Decimal, None for no number)
    of a ``NUMERIC_OPS`` op, from the inputs' re-read numbers (``computed_value``; its errors propagate).

    ``sum``, ``min``, ``max`` and ``difference`` hold only exactly. ``avg`` and ``ratio``
    hold exactly, or rounded: the recomputed value rounded half-up (away from zero) to the
    decimals the claim writes is the claimed value, so a written figure never hides more
    than half its last digit, and the claim writes at least the floor's decimals: an
    average at least those of its most precise input, up to 2 (an average of cents is
    written to the cent, of whole units to the unit); a ratio at least 2 (0.33), as a
    percent at least 0 (33). The recomputed value returned is the claimed one when it
    holds, else the exact value rounded half-up to the claim's decimals or the floor,
    whichever is finer (what the claim should have written)."""
    exact = computed_value(op, numbers, percent)
    number = claimed if isinstance(claimed, Decimal) and claimed.is_finite() else None
    if op not in ROUNDED_OPS:
        return number is not None and number == exact, exact
    floor = (0 if percent else 2) if op == "ratio" else min(
        2, max(_places(n) for n in numbers))
    places = max(floor, _places(number) if number is not None else 0)
    try:
        with localcontext(Context(prec=80)):
            shown = exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        shown = exact
    if number is None:
        return False, shown
    if number == exact:
        return True, number
    if _places(number) < floor:
        return False, shown
    return shown == number, number if shown == number else shown


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def answer_candidate(text: Any) -> dict[str, Any] | None:
    """The answer object of the agent's final message, well formed or not: the last
    ```json fenced object (or the whole message) whose ``format`` is ``ANSWER_FORMAT``."""
    if not isinstance(text, str) or not text.strip():
        return None
    candidates = list(_JSON_FENCE.findall(text))[::-1] + [text.strip()]
    for raw in candidates:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, Mapping) and value.get("format") == ANSWER_FORMAT:
            return dict(value)
    return None


def parse_answer(text: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(answer, None) or (None, error code) from the agent's final message
    (``answer_candidate``), refused when ``answer_problems`` finds it malformed."""
    value = answer_candidate(text)
    if value is None:
        return None, "answer_missing"
    problems = answer_problems(value)
    if problems:
        return None, "answer_malformed:" + problems[0]
    return value, None


def answer_problems(answer: Mapping[str, Any]) -> list[str]:
    out = []
    allowed = {"format", "answerability", "text", "values", "gaps"}
    if set(answer) - allowed:
        out.append("unknown_keys")
    if answer.get("answerability") not in ANSWERABILITY:
        out.append("answerability")
    if not isinstance(answer.get("text", ""), str):
        out.append("text")
    values = answer.get("values", [])
    if not isinstance(values, list) or len(values) > MAX_VALUES:
        out.append("values")
        values = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
            out.append(f"values.{index}")
            continue
        calc = value.get("calculation")
        if calc is not None and calculation_problem(calc, value.get("citations")):
            out.append(f"values.{index}.calculation")
        boolean = isinstance(calc, Mapping) and calc.get("op") == "equal"
        if (isinstance(value.get("value"), bool) and not boolean) or not isinstance(
                value.get("value"), (str, int, float, bool, type(None))):
            out.append(f"values.{index}.value")
        cites = value.get("citations", [])
        if not isinstance(cites, list) or len(cites) > MAX_CITATIONS or any(
                not isinstance(cite, Mapping) or not isinstance(cite.get("l1_handle"), str)
                or not isinstance(cite.get("field_path"), str)
                or not isinstance(cite.get("quote", ""), str) for cite in cites):
            out.append(f"values.{index}.citations")
    gaps = answer.get("gaps", [])
    if not isinstance(gaps, list) or any(
            not isinstance(gap, Mapping) or not isinstance(gap.get("state"), str)
            or not isinstance(gap.get("subject"), str)
            or not isinstance(gap.get("concerns", ""), (str, type(None)))
            for gap in gaps):  # null: the gap concerns no value, as if left out
        out.append("gaps")
    return out


def calculation_problem(calc: Any, citations: Any = None) -> str | None:
    """Why an answer value's ``calculation`` is malformed (None: well formed). A value
    states its inputs in the calculation, never also in ``citations``."""
    expression = isinstance(calc, Mapping) and calc.get("op") == "expression"
    if not isinstance(calc, Mapping) or set(calc) - {"op", "inputs"} - (
            {"expression"} if expression else set()) or calc.get("op") not in CALCULATION_OPS:
        return "op"
    if expression and not isinstance(calc.get("expression"), str):
        return "expression"
    if citations:
        return "citations_beside_calculation"
    inputs = calc.get("inputs")
    if not isinstance(inputs, list) or not (
            2 if calc["op"] == "equal" else 1) <= len(inputs) <= MAX_CALCULATION_INPUTS \
            or (calc["op"] in PAIR_OPS and len(inputs) != 2):
        return "inputs"
    for entry in inputs:
        if expression and not (isinstance(entry, Mapping) and isinstance(entry.get("value"),
                                                                          str)):
            return "input"
        if not isinstance(entry, Mapping) or set(entry) - {
                "l1_handle", "field_path", "unit", "currency", "quantity_role"} - (
                    {"value", "quote"} if expression else set()) or \
                not isinstance(entry.get("l1_handle"), str) or any(
                    not isinstance(entry.get(k, ""), str)
                    for k in ("unit", "currency", "quantity_role", "quote")) or \
                (calc["op"] not in ("count", "count_distinct")
                 and not isinstance(entry.get("field_path"), str)):
            return "input"
    return None
