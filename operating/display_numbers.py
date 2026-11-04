"""Numbers as people read them: the one display rule for answers, cards and the app API.

A source that sends a decimal as a binary float (Business Central's JSON ``unitCost``) can
carry a float artefact: ``116.25000000000001`` for the 116.25 the source holds. A double keeps
15 significant digits of a decimal, so the source's own precision is the value rounded to 15
significant digits. ``display_number`` shows that, and ``display_text`` rewrites such numerals
in a reply. The stored value never changes: tool results, citations and the claim checks keep
the exact value, and the claim checks accept the displayed form only as their own rule allows
(``operating.claim_checks.rounded_from``: a value written to 2-4 decimal places), so a
numeral is rewritten only when its display keeps 2 to 4 decimals (``116.25``,
``0.30`` for ``0.30000000000000004``); any other numeral is shown as it is.

Pure and stdlib-only; never raises on any input.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any

SIGNIFICANT = 15        # decimal digits a binary double keeps
PLACES = (2, 4)         # decimals a displayed form keeps (claim_checks.ROUNDED_PLACES)
# A float artefact: a decimal numeral whose fraction ends in a run of 0s or 9s and 1-3 stray
# digits, standing alone (not part of an identifier, version or longer numeral).
_ARTEFACT = re.compile(r"(?<![\w.])(-?\d+\.\d*?(?:0{5,}|9{5,})\d{1,3})(?![\w]|\.\d)")


def display_number(value: Any) -> str | None:
    """``value`` (a number or a numeral) as shown to a person: a float artefact rounded to
    the source's precision (15 significant digits, then 2-4 decimals kept), any other
    number as the source wrote it. None for no number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = repr(value)
    text = str(value).strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    shown = _cleaned(number)
    return text if shown is None else shown


def _cleaned(number: Decimal) -> str | None:
    """The display of a float artefact, or None when ``number`` is none (or cleaning it
    would leave other than 2-4 decimals)."""
    digits = number.as_tuple().digits
    if len(digits) <= SIGNIFICANT or number.as_tuple().exponent >= 0:
        return None
    rounded = round(number, SIGNIFICANT - number.adjusted() - 1).normalize()
    places = max(-rounded.as_tuple().exponent, 0)
    if places > PLACES[1]:
        return None
    return format(rounded.quantize(Decimal(1).scaleb(-max(places, PLACES[0]))), "f")


def display_text(text: str) -> str:
    """``text`` with every float-artefact numeral shown at the source's precision."""
    if not isinstance(text, str) or "." not in text:
        return text

    def one(match: re.Match[str]) -> str:
        try:
            shown = _cleaned(Decimal(match.group(1)))
        except (InvalidOperation, ValueError):
            shown = None
        return match.group(1) if shown is None else shown

    return _ARTEFACT.sub(one, text)


def for_display(value: Any, _depth: int = 0) -> Any:
    """A JSON value as shown (the app API's response body): every float artefact, as a
    JSON number or a numeral in text, at the source's precision; keys, structure and every
    other value unchanged. Bounded depth: deeper values are returned as they are."""
    if _depth > 64:
        return value
    if isinstance(value, float) and math.isfinite(value):
        shown = _cleaned(Decimal(repr(value)))
        return value if shown is None else float(shown)
    if isinstance(value, str):
        return display_text(value)
    if isinstance(value, dict):
        return {key: for_display(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [for_display(item, _depth + 1) for item in value]
    return value


_ANSWER_BLOCK = re.compile(r"(```(?:json)?\s*)(\{.*?\})(\s*```)", re.S)


def display_reply(text: str) -> str:
    """A final reply as shown: its prose and each answer block's text and gaps through
    ``display_text``, but every answer value exactly as the claim check passed it. The
    values are claims on their cited cells (``116.25000000000001`` on a cell holding that):
    rounding one would make it a claim of another value; the reader sees it rounded where
    the app shows it (``for_display``)."""
    if not isinstance(text, str):
        return text

    def block(match: re.Match[str]) -> str:
        import json
        try:
            value = json.loads(match.group(2))
        except ValueError:
            return display_text(match.group(0))
        if not isinstance(value, dict):
            return display_text(match.group(0))
        shown = {key: item if key == "values" else for_display(item)
                 for key, item in value.items()}
        return match.group(1) + json.dumps(shown, ensure_ascii=False) + match.group(3)

    out, at = [], 0
    for match in _ANSWER_BLOCK.finditer(text):
        out.append(display_text(text[at:match.start()]))
        out.append(block(match))
        at = match.end()
    out.append(display_text(text[at:]))
    return "".join(out)
