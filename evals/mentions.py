"""Whether free text states an expected value: numbers by value, other values as whole tokens."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

_NUMERIC = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?$")
# A standalone number: not glued to letters/digits or to ``-``, ``/``, ``#``, ``.`` that join it to
# an identifier, date or lot (``AO-26006``, ``1509-A``, ``1509/2026``, ``lot#1509``, ``x1509``).
_NUMBER_IN_TEXT = re.compile(r"(?<![\w.\-/#])\d(?:\d|,(?=\d{3}(?!\d)))*(?:\.\d+)?(?![\w#]|[-/.]\w)")


def _decimal(text: Any) -> Decimal | None:
    if isinstance(text, bool) or text is None:
        return None
    raw = str(text).strip()
    if not _NUMERIC.match(raw) or not any(ch.isdigit() for ch in raw):
        return None
    try:
        number = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def mentions(text: Any, value: str) -> bool:
    """``text`` states ``value``: equal numbers (``1,509`` = ``1509.0``) or the value as a
    whole token, case-insensitive. Digits inside identifiers (``AO-26006``) are not numbers."""
    if not isinstance(text, str) or not text:
        return False
    number = _decimal(value)
    if number is not None:
        return any(_decimal(found.rstrip(",")) == number
                   for found in _NUMBER_IN_TEXT.findall(text))
    pattern = r"(?<![A-Za-z0-9])" + re.escape(value.strip()) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None
