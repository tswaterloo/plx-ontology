"""Absence claims of the in-process case drivers, through the one gap guard.

A driver records every gap through ``plx.case_record_gap``, whose guard
(``capability_guards.check_gap``, under the host's schema) decides an
``absent_in_source`` claim. Two things stay with the drivers:

* ``traversal_problem``: the one rule for every absence a read or an answer infers from
  rows a source did not return (the drivers here, ``plx.aggregate``/``plx.compare``'s
  members the source no longer returns): only a current capture of a complete traversal
  shows it; a stale capture, a failed refresh or a partial or unknown traversal is
  ``unknown`` with the reason, never ``absent_in_source``.
* ``record_gap``: an absence the guard refuses is recorded as ``unknown`` with the
  guard's reason, never dropped and never aborting the case.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

Call = Callable[[str, dict[str, Any]], dict[str, Any]]

ABSENT = "absent_in_source"
GUARD_REFUSED = "gap_guard_failed:"
MAX_WHY = 4000  # plx.case_record_gap's why


def traversal_problem(freshness: Sequence[Mapping[str, Any]]) -> str | None:
    """Why the sources behind an inferred absence (``freshness``: one object per source or
    page read, the mirror's page freshness or the ontology tools' contract freshness) do
    not show it; None when every one is a current capture of a complete traversal (a page
    that states no ``completeness`` is one read of the mirror's whole relation)."""
    if not freshness:
        return ("no source behind the answer states its capture, so a record not returned "
                "may still exist")
    if any(item.get("stale") or item.get("refresh_error") for item in freshness):
        return ("the source's capture was not refreshed (stale or a failed refresh), so a "
                "record it did not return may still exist")
    if any(item.get("completeness", "complete") != "complete" for item in freshness):
        return ("the source's latest traversal is not known to be complete, so a record it "
                "did not return may still exist")
    return None


def record_gap(call: Call, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """``plx.case_record_gap``; an ``absent_in_source`` gap the gap guard refuses is
    recorded ``unknown`` with the guard's reason (its property kept unless the host does
    not declare it). Returns the tool's result."""
    try:
        return call("plx.case_record_gap", dict(arguments))
    except RuntimeError as error:
        text = str(error)
        if arguments.get("state") != ABSENT or GUARD_REFUSED not in text:
            raise
    reason = text.split(GUARD_REFUSED, 1)[1].strip()
    downgraded = {**arguments, "state": "unknown",
                  "why": (f"{arguments['why']} Absence is not shown, so it is recorded "
                          f"unknown (gap guard: {reason})")[:MAX_WHY]}
    if reason.startswith("gap_property_undeclared"):
        downgraded.pop("property", None)
    return call("plx.case_record_gap", downgraded)


__all__ = ["ABSENT", "record_gap", "traversal_problem"]
