"""Machine-checked action preconditions over a proposal's cited evidence.

A case proposal (``operating.cases.CaseStore.propose``) carries a list of
preconditions. Prose items (strings) are things a person must confirm; they are
carried into the MC approval request so the deciding human sees them, and they
are never treated as checked. Mapping items are checked here, deterministically,
against the content the proposal cites, and only while that content is still
exactly what was cited (same digest)::

    {"check": "field_empty",  "citation": <handle>, "field": "<path>"}
    {"check": "field_equals", "citation": <handle>, "field": "<path>", "value": <str|int>}

The handle must be one of the proposal's own citations. Any other shape, an
unknown check, a citation that no longer resolves or changed, or a field that
cannot be inspected fails closed. Field paths and value matching are those of
``operating.case_verification.field_status`` (the read-back uses the same).

Evidence label: whatever the cited source is; nothing here reads a source
itself (``CaseStore.proposal_evidence`` does, through the case's resolver).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .case_verification import field_status

CHECKS = ("field_empty", "field_equals")
_KEYS = {"field_empty": {"check", "citation", "field"},
         "field_equals": {"check", "citation", "field", "value"}}


def split(preconditions: Sequence[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """(structured checks, prose items). A malformed structured item raises."""
    checks, prose = [], []
    for index, item in enumerate(preconditions or ()):
        if isinstance(item, str):
            prose.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"precondition_unrecognized:{index}")
        check = item.get("check")
        if check not in _KEYS or set(item) != _KEYS[check]:
            raise ValueError(f"precondition_unrecognized:{index}")
        if not isinstance(item["citation"], str) or not isinstance(item["field"], str) \
                or not item["field"].strip():
            raise ValueError(f"precondition_unrecognized:{index}")
        if check == "field_equals" and (isinstance(item["value"], bool) or
                                        not isinstance(item["value"], (str, int))):
            raise ValueError(f"precondition_unrecognized:{index}")
        checks.append(dict(item))
    return checks, prose


def evaluate(checks: Sequence[Mapping[str, Any]],
             evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate structured checks against ``CaseStore.proposal_evidence`` output.

    Returns ``{"holds": bool, "reason": str | None, "results": [...]}``; the
    first failing check names the reason (``precondition_citation_changed``,
    ``precondition_not_cited``, ``precondition_field_not_inspectable`` or
    ``precondition_not_met``). Each result records the handle, the cited
    digest it was evaluated on and the field status (values are withheld).
    """
    by_handle = {item["handle"]: item for item in evidence}
    results: list[dict[str, Any]] = []
    reason = None
    for index, check in enumerate(checks):
        cited = by_handle.get(check["citation"])
        if cited is None:
            status, failure = None, "precondition_not_cited"
        elif cited.get("content") is None or cited.get("current_digest") != cited.get(
                "cited_digest"):
            status, failure = None, "precondition_citation_changed"
        else:
            row = cited["content"].get("row") if isinstance(cited["content"], Mapping) else None
            if not isinstance(row, Mapping):
                status, failure = "not_inspectable", "precondition_field_not_inspectable"
            else:
                expected = check.get("value", "")
                status = field_status(row, check["field"], expected)["status"]
                wanted = "empty" if check["check"] == "field_empty" else "equals_expected"
                failure = (None if status == wanted else
                           "precondition_field_not_inspectable" if status == "not_inspectable"
                           else "precondition_not_met")
        results.append({"index": index, "check": check["check"], "citation": check["citation"],
                        "field": check["field"],
                        "evaluated_digest": cited.get("cited_digest") if cited else None,
                        "status": status, "holds": failure is None})
        if failure and reason is None:
            reason = failure
    return {"holds": reason is None and bool(checks), "reason": reason, "results": results}


__all__ = ["CHECKS", "evaluate", "split"]
