"""An answer the code states from a question family's plan result, for the agent loop.

``plx.investigate``'s result is already a list of cited entries and typed gaps. The loop's
planner rules tell a model to state each entry as one claim and each gap as an unknown; a
model call for that copying costs 10 to 20 seconds and adds nothing a rule does not
already say. ``from_plan`` makes the same decision in code, so that a planner which
chose the family (``{"action": "answer_plan", ...}``, ``operating.agent_loop``) is called
once, not three times.

Nothing is decided here that the claim gate does not re-check. Every claim carries a
citation and a field the plan's own entry holds; the loop passes the decision through
``answer_gate.gate_answer`` like any planner answer, and a claim the gate refuses is no
answer (the planner then reads the result and goes on). ``from_plan`` fails closed: it
returns ``None`` (the planner answers) unless it can state *every* entry that holds a
value, so a value is never dropped to make the rest fit:

* a result with an error, no ``answer`` list, or more entries than one answer may claim;
* a result the host cut for any reason but fitting it to one response (a step budget, a
  page cap), so that an entry could be missing; one the host fitted by cutting a select
  entry's cells (``inputs_cut``) when that entry holds a value and now no cell of it;
* a ``property`` entry whose check is not ``passed`` or ``unverified``, or whose value is
  not a string or an integer, or that carries no citation;
* a ``select`` entry with a value but no cited cell of that value is not claimed (a
  verdict or a chain no one record holds): it is listed as an unknown;
* an entry of any other kind that holds a value (a count, a computed value).

An entry whose value is null, and every gap, become unknowns (``<state>: <subject>``)
unless the plan says it is answerable; a cut list (``<list>_truncated``) is always one. Evidence: isolated-fixture
(operating/tests/test_plan_answer.py).
"""

from __future__ import annotations

from typing import Any, Mapping

from .answer_gate import MAX_CLAIMS, MAX_UNKNOWN_CHARS, MAX_UNKNOWNS

STATED = ("passed", "unverified")   # the checks of a property entry the plan itself reads back


def _plain(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _claim(number: int, value: Any, cite: Any, role: Any, unit: Any,
           lead: bool) -> dict[str, Any] | None:
    handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
    field = cite.get("field_path") if isinstance(cite, Mapping) else None
    if not _plain(value) or not isinstance(handle, str) or not isinstance(field, str):
        return None
    claim: dict[str, Any] = {"id": f"c{number}", "value": value, "citation": handle,
                             "field": field}
    if isinstance(role, str) and role:
        claim["role"] = role
    if isinstance(unit, str) and unit:
        claim["unit"] = unit
    if lead:
        claim["lead"] = True
    return claim


def from_plan(result: Any) -> dict[str, Any] | None:
    """The answer decision (``claims`` and ``unknowns``) of ``result``, a whole
    ``plx.investigate`` result, or None when code cannot state all of it (module doc)."""
    if not isinstance(result, Mapping) or "error" in result:
        return None
    entries = result.get("answer")
    if not isinstance(entries, list) or not entries:
        return None
    completeness = result.get("completeness")
    if not isinstance(completeness, Mapping):
        return None
    # The host fits a big result to one response by cutting the chain first and then the
    # cells of the largest entries (``result_too_large``): the entries stay, so a cell may be
    # gone from one. Any other cut (a step budget, a page cap) may have dropped an entry.
    fitted = completeness.get("truncated") is True \
        and completeness.get("reason") == "result_too_large"
    if completeness.get("truncated") is not False and not fitted:
        return None
    claims: list[dict[str, Any]] = []
    unknowns: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            return None
        value, kind = entry.get("value"), entry.get("kind")
        if value is None:
            unknowns.append(f"undetermined: {entry['name']} (the plan states no value)")
            continue
        number = len(claims) + 1
        if kind == "property":
            if entry.get("check") not in STATED:
                return None
            claim = _claim(number, value, entry.get("citation"),
                           entry.get("quantity_role") if entry["check"] == "passed" else None,
                           entry.get("unit"), entry.get("lead_basis") is True)
        elif kind == "select":
            cells = [c for c in entry.get("cells") or () if isinstance(c, Mapping)
                     and c.get("value") == value and isinstance(c.get("citation"), Mapping)]
            if not cells and entry.get("inputs_cut"):
                return None   # the cell this value cites may be the one the host cut
            if not cells:
                # A verdict or a chain the plan selected from its primitive (late, why):
                # no one record holds that value, so no claim can cite it. It is listed,
                # never claimed.
                unknowns.append(f"not one record's value: {entry['name']} = {value}"[
                    :MAX_UNKNOWN_CHARS])
                continue
            claim = _claim(number, value, cells[0]["citation"], cells[0].get("quantity_role"),
                           cells[0].get("unit"), False)
        else:
            return None
        if claim is None:
            return None
        claims.append(claim)
    if not claims or len(claims) > MAX_CLAIMS:
        return None
    answerability = result.get("answerability")
    answerable = isinstance(answerability, Mapping) and answerability.get("state") == "answerable"
    for gap in result.get("gaps") or ():
        # a list the host cut (``<list>_truncated``) is stated even on an answerable plan:
        # the claims name the listed elements, never all of them
        if isinstance(gap, Mapping) and isinstance(gap.get("subject"), str) and (
                not answerable or str(gap.get("reason") or "").endswith("_truncated")):
            text = f"{gap.get('state') or 'unknown'}: {gap['subject']}"
            detail = gap.get("detail") or gap.get("reason")
            if isinstance(detail, str) and detail:
                text += f" - {detail}"
            text = text[:MAX_UNKNOWN_CHARS]
            if text not in unknowns:
                unknowns.append(text)
    if len(unknowns) > MAX_UNKNOWNS:
        # An answer holds MAX_UNKNOWNS at most: the last one says how many are not listed,
        # so a cut is never silent.
        rest = len(unknowns) - (MAX_UNKNOWNS - 1)
        unknowns = unknowns[:MAX_UNKNOWNS - 1] + [
            f"unknown: {rest} more gaps of the plan are not listed (answer limit)"]
    return {"action": "answer", "claims": claims, "unknowns": unknowns}
