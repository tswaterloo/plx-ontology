"""Composition lines: what a batch sheet says a product is made of, read from its text.

A formula sheet or sample card (FileMaker's printed card, kept as a scan in the R&D drive)
states a header (``FORMULA: F-YY-NNNN``, ``NAME:``, ``CODE:`` the product code,
``CUSTOMER:``, ``SIZE: <n> g|kg``) and one line per component::

    #   Part#  | Description                       %      QTY (g)
    1A  R-0587 | Fractionated Coconut Oil ...     48.41   72.611

``read_sheet(text)`` returns the header values it found and one entry per line that names a
part code and a share: ``part`` (the code as printed, ``R-NNNN`` folded to its dash form),
``share`` (Decimal text, percent of the batch), ``quantity`` (Decimal text in the size's
unit, or None), ``line`` (the 1-based text line, the citation) and ``check``:

* ``checked``: the quantity equals share x size / 100 within rounding (0.01 of the unit or
  0.5%): two printed numbers agree, so the OCR read both right;
* ``checked_column_rule``: they agree once a leading ``1`` of the quantity is read as the
  column rule OCR glued to it (``|72.611`` read ``172.611``); the quantity given is the one
  without it;
* ``quantity_disagrees``: both read, they do not agree (an OCR misread of either); the
  share is kept as printed, never corrected;
* ``no_quantity`` / ``no_size``: nothing to check against;
* ``share_unread``: a numbered row naming a part code whose numbers OCR did not read
  (``share`` None): the part is in the formula, its share unknown.

The sheet's ``status`` is ``complete`` only when the line numbers run 1..n with none
missing, every line is checked (either form) and the shares sum to 100 (within 0.1); otherwise
``partial`` with what is missing (``missing_lines``, ``share_total``). A line OCR lost is a
missing line, never filled. Nothing here decides which item a part code is (the caller
resolves codes through the store's identifiers) and nothing names a product or customer.
Stdlib only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

FORMAT = "plx-composition-lines/v1"

_PART = r"R-?\d{3,4}(?:/\d{1,3})?"
# a component line: optional line number (1, 1A, 12C), a column rule or OCR'd one, the
# part code, the description, then the share and the quantity as the first two numbers
# after it (OCR may glue a column rule to the quantity: "|59.880", "{0.120")
_LINE = re.compile(
    r"^[^\w]{0,16}(?:(?P<no>\d{1,2})(?P<phase>[A-Z])?[^\w]{0,8})?"
    r"(?P<part>" + _PART + r")\b\s*[|{(\[!]?\s*"
    r"(?P<desc>.*?)\s+"
    r"(?P<share>\d{1,3}[.,]\d{1,4}|100|\d{1,2})\s+"
    r"[|{(\[!l]?(?P<qty>\d{1,6}[.,]\d{1,4}|\d{1,6})(?=\s|$)")
# a numbered row naming a part code whose share and quantity were not read
_NUMBERED_PART = re.compile(r"^[^\w]{0,16}(?P<no>\d{1,2})(?P<phase>[A-Z])?[^\w]{0,8}"
                            r"(?P<part>" + _PART + r")\b(?P<desc>.*)$")
_HEADER = {
    "formula_code": re.compile(r"(?i)\bFORMULA\s*[:#]\s*(F-\d{2}-\d{4}(?:-[A-Z])?)\b"),
    "product_code": re.compile(r"(?i)(?:^|\s)CODE\s*[:#]\s*([A-Z0-9][A-Z0-9-]{1,30}[A-Z0-9])\b"),
    "name": re.compile(r"(?i)(?:^|\s)NAME\s*[:#]\s*(.{3,120}?)(?:\s{3,}|\||$)"),
    "customer": re.compile(r"(?i)(?:^|\s)CUSTOMER\s*[:#]\s*(.{2,80}?)(?:\s{3,}|\||$)"),
}
_SIZE = re.compile(r"(?i)\bSIZE\s*[:#]\s*(\d{1,6}(?:[.,]\d{1,3})?)\s*(kg|g)\b")
SHARE_TOTAL_TOLERANCE = Decimal("0.1")
CHECKED = frozenset({"checked", "checked_column_rule"})


def _number(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def _part(raw: str) -> str:
    code = raw.upper()
    return code if code.startswith("R-") else "R-" + code[1:]


def _clean(desc: str) -> str:
    return re.sub(r"^[|{(\[!\s]+|[|{(\[!\s]+$", "", desc)[:120]


def _agrees(share: Decimal, quantity: Decimal, size: Decimal) -> bool:
    expected = share * size / Decimal(100)
    return abs(quantity - expected) <= max(Decimal("0.01"), expected * Decimal("0.005"))


def read_sheet(text: str) -> dict[str, Any]:
    """The header and component lines of one sheet's text (module doc)."""
    header: dict[str, Any] = {}
    lines: list[dict[str, Any]] = []
    size: Decimal | None = None
    unit: str | None = None
    for number, raw in enumerate((text or "").splitlines(), start=1):
        for key, pattern in _HEADER.items():
            if key not in header:
                m = pattern.search(raw)
                if m and m.group(1).strip():
                    header[key] = {"value": m.group(1).strip()[:120], "line": number}
        if size is None:
            m = _SIZE.search(raw)
            if m:
                size, unit = _number(m.group(1)), m.group(2).lower()
                header["size"] = {"value": f"{m.group(1).replace(',', '.')} {unit}",
                                  "line": number}
        m = _LINE.match(raw)
        share = quantity = None
        if m is not None:
            share = _number(m.group("share"))
            quantity = _number(m.group("qty"))
            if share is None or not Decimal(0) < share <= Decimal(100):
                m = share = None
        if m is None:
            m = _NUMBERED_PART.match(raw)
            if m is not None:   # its row, the numbers lost (OCR split or blurred them)
                lines.append({"no": int(m.group("no")), "part": _part(m.group("part")),
                              "description": _clean(m.group("desc")), "share": None,
                              "quantity": None, "unit": unit, "line": number,
                              "check": "share_unread"})
            continue
        if quantity is None:
            check = "no_quantity"
        elif size is None or size <= 0:
            check = "no_size"
        else:
            check = "checked" if _agrees(share, quantity, size) else "quantity_disagrees"
            rule = m.group("qty")
            if check == "quantity_disagrees" and rule[:1] == "1" and rule[1:2].isdigit():
                # OCR reads a column rule glued to the quantity as a leading 1 ("|72.6"
                # -> "172.6"): without it the two printed numbers agree, so the share is
                # corroborated; the quantity is the corrected one, labelled so
                bare = _number(rule[1:])
                if bare is not None and _agrees(share, bare, size):
                    quantity, check = bare, "checked_column_rule"
        lines.append({"no": int(m.group("no")) if m.group("no") else None,
                      "part": _part(m.group("part")),
                      "description": _clean(m.group("desc")), "share": str(share), "quantity": None if quantity is None else
                      str(quantity), "unit": unit, "line": number, "check": check})
    total = sum((Decimal(x["share"]) for x in lines if x["share"] is not None), Decimal(0))
    numbers = sorted({x["no"] for x in lines if x["no"] is not None})
    missing = ([n for n in range(1, numbers[-1] + 1) if n not in numbers]
               if numbers else [])
    complete = bool(lines) and not missing and len(numbers) == len(lines) \
        and all(x["check"] in CHECKED for x in lines) \
        and abs(total - Decimal(100)) <= SHARE_TOTAL_TOLERANCE
    return {"format": FORMAT, "header": header, "lines": lines,
            "share_total": str(total), "missing_lines": missing,
            "status": "complete" if complete else ("partial" if lines else "no_lines")}


__all__ = ["CHECKED", "FORMAT", "SHARE_TOTAL_TOLERANCE", "read_sheet"]
