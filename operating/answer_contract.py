"""The answer contract an agent session ends with (``plx-eval-answer/v1``): its parse and
shape checks, and what makes a tool an action. Product code: the app's Ask reads a
session's answer with these exactly as the scorer does (``evals.score`` uses them)."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


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


CALCULATION_OPS = ("sum", "count", "equal", "expression")


MAX_CALCULATION_INPUTS = 100  # the oracle's chain cap (evals/oracle/families.py MAX_CHAIN)


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
            2 if calc["op"] == "equal" else 1) <= len(inputs) <= MAX_CALCULATION_INPUTS:
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
                (calc["op"] != "count" and not isinstance(entry.get("field_path"), str)):
            return "input"
    return None
