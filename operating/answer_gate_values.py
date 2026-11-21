"""How the answer gate compares a value it kept with one shown: white space and number
forms aside (split out of ``answer_gate``, module size cap)."""
from __future__ import annotations

from typing import Any

from . import claim_checks


def _text_or_none(value: Any) -> str | None:
    """A string with its runs of white space as one space, or None (none, or empty)."""
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _verbatim(value: Any) -> str | None:
    """A quote as the gate checked it, characters kept (a reader checks it verbatim)."""
    return value.strip() or None if isinstance(value, str) else None


def _same(one: Any, other: Any) -> bool:
    a, b = claim_checks.to_decimal(one), claim_checks.to_decimal(other)
    if a is not None and b is not None:
        return a == b
    return " ".join(str(one).split()) == " ".join(str(other).split())
