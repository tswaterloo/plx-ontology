"""Order submissions that cover other orders by a PO range (a non-merging ``covers`` link).

A customer may submit one order for several of its purchase orders and number it by
their range: ``PO8735-43`` (PO8735 to PO8743), ``8811-8812``, ``PO-008672/73`` (PO-008672
and PO-008673), ``1SPO-000035/36/37``, ``8769/8771``. The system of record then holds one
order per PO, each with that PO as its customer PO and nothing else naming the
submission. The submission COVERS those orders: it is a different record (never merged
with them, never one of their copies), and what it states for them (the date the
customer asked for) can be carried to one only when that is unambiguous.

The rules (each fails closed: no link):

* range notation (``covered_numbers``): an optional prefix, a first number, then one or
  more parts each joined by ``-`` (a range from the part before) or ``/``, ``,``, ``&``
  (one more number). A shorter part replaces the first number's last digits (``8735-43``
  is 8743); a part longer than the first number, a range that does not rise, or more than
  ``MAX_COVERED`` numbers is no range notation;
* a covering order holds no customer PO of its own and names two or more numbers;
* it covers an order of the SAME customer (one customer each, by settled links) whose
  customer PO is one of the numbers, with a compatible prefix (the same letters, or one
  side has none: ``8740`` and ``PO8740``), and that order is the only one of the customer
  holding that number (two make it ambiguous: neither is covered);
* the cover is never an identity: both orders stay separate rows with their own status,
  value and lines.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

MAX_COVERED = 20
_RANGE = re.compile(r"^(?P<prefix>(?:[0-9]*[A-Za-z][A-Za-z0-9]*?)?)[\s#._-]*"
                    r"(?P<first>[0-9]+)(?P<rest>(?:\s*[-/,&–]\s*[0-9]+)+)$")
_PART = re.compile(r"\s*([-/,&–])\s*([0-9]+)")
_SINGLE = re.compile(r"^(?P<prefix>(?:[0-9]*[A-Za-z][A-Za-z0-9]*?)?)[\s#._-]*"
                     r"(?P<number>[0-9]+)$")


def _prefix(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def covered_numbers(text: Any) -> tuple[str, tuple[int, ...]] | None:
    """(prefix, the numbers) a range-notation order number names, in order; None for any
    other text (one number, a revision suffix that does not rise, a part too long)."""
    if not isinstance(text, str):
        return None
    m = _RANGE.match(text.strip())
    if not m:
        return None
    first = m.group("first")
    numbers = [int(first)]
    for sep, part in _PART.findall(m.group("rest")):
        if len(part) > len(first):
            return None
        whole = int(first[:len(first) - len(part)] + part)
        if sep in "-–":
            if whole <= numbers[-1] or whole - numbers[-1] >= MAX_COVERED:
                return None
            numbers += range(numbers[-1] + 1, whole + 1)
        else:
            numbers.append(whole)
        if len(numbers) > MAX_COVERED:
            return None
    if len(set(numbers)) != len(numbers):
        return None
    return _prefix(m.group("prefix")), tuple(numbers)


def po_number(text: Any) -> tuple[str, int] | None:
    """(prefix, number) of a customer PO that is one number (``PO-008672``, ``8740``)."""
    if not isinstance(text, str):
        return None
    m = _SINGLE.match(text.strip())
    if not m:
        return None
    return _prefix(m.group("prefix")), int(m.group("number"))


def _compatible(a: str, b: str) -> bool:
    return a == b or not a or not b


def covers(orders: Iterable[tuple[str, Any, Any, Any]]) -> dict[str, list[str]]:
    """Covering order -> the orders it covers, from (order key, order number, customer
    PO, customer key) per order; a customer key of None (no customer, or two) covers and
    is covered by nothing."""
    held: dict[tuple[Any, int], list[tuple[str, str]]] = {}
    ranged: list[tuple[str, Any, str, Sequence[int]]] = []
    for key, number, po, customer in orders:
        if customer is None:
            continue
        own = po_number(po)
        if own is not None:
            held.setdefault((customer, own[1]), []).append((key, own[0]))
        named = covered_numbers(number)
        if named is not None and not (isinstance(po, str) and po.strip()):
            ranged.append((key, customer, *named))
    out: dict[str, list[str]] = {}
    for key, customer, prefix, numbers in ranged:
        got = []
        for n in numbers:
            them = [k for k, p in held.get((customer, n), ()) if k != key]
            if len(held.get((customer, n), ())) == 1 and len(them) == 1 and _compatible(
                    prefix, held[(customer, n)][0][1]):
                got.append(them[0])
        if got:
            out[key] = got
    return out


def covered_by(cover: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Covered order -> the orders covering it (sorted)."""
    out: dict[str, list[str]] = {}
    for key, them in cover.items():
        for t in them:
            out.setdefault(t, []).append(key)
    return {k: sorted(v) for k, v in out.items()}
