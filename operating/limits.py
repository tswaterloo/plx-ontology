"""The one place a text or a list is cut for display, and the note that says so.

Guardrail 1 (owner, 11-23): no silent limit. A cap may exist for a stated reason (a model window,
a host limit, safety), never for speed or tidiness; where it cuts it says so, with how much was
left out; and the rest can be reached (a next page, 'show all', a next pass). A bare
``text[:200]`` or ``rows[:20]`` in operating/ or operator_api/ fails ``tests.test_guardrails``;
use these instead and pass the note on to the person or the model.

    c = cut(body, 4000, say="model window")
    c.text      # the kept part (the whole text when nothing was cut)
    c.note      # "" when nothing was cut, else "4,000 of 9,120 characters shown (model window); 5,120 more not shown"
    c.cut       # True when something was left out
"""
from __future__ import annotations

from typing import NamedTuple, Sequence, TypeVar

T = TypeVar("T")


class Cut(NamedTuple):
    """What a cut kept, whether it cut, how much was left out and the sentence that says so."""
    text: str
    cut: bool
    total: int
    left_out: int
    note: str


class CutList(NamedTuple):
    items: list
    cut: bool
    total: int
    left_out: int
    note: str


def _why(say: str) -> str:
    return f" ({say})" if say else ""


def cut(text: str, limit: int, *, say: str = "") -> Cut:
    """Keep the first ``limit`` characters of ``text``; say how many were left out and why."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    total = len(text)
    if total <= limit:
        return Cut(text, False, total, 0, "")
    left = total - limit
    note = f"{limit:,} of {total:,} characters shown{_why(say)}; {left:,} more not shown"
    return Cut(text[:limit], True, total, left, note)


def cut_list(items: Sequence[T], limit: int, *, say: str = "", noun: str = "items") -> CutList:
    """Keep the first ``limit`` of ``items``; say how many were left out and why."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    total = len(items)
    if total <= limit:
        return CutList(list(items), False, total, 0, "")
    left = total - limit
    note = f"{limit:,} of {total:,} {noun} shown{_why(say)}; {left:,} more not shown"
    return CutList(list(items[:limit]), True, total, left, note)
