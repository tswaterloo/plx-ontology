"""Pure text helpers for the mail rules. Kept out of the ontology step modules so the
served steps digest does not move when they change."""

from __future__ import annotations

from typing import Iterable

from ontology.mentions import QuotedRanges
from ontology.text_index import chunk_text


def unquoted(ranges: QuotedRanges, text: str) -> str:
    """``text`` without its quoted characters (the sender's own text): the same string as
    keeping each character ``ranges.contains`` refuses. Unquoted stretches are located by
    a binary search over ``overlaps`` and copied whole; only quoted characters are walked."""
    n = len(text)
    if not ranges:
        return text
    parts: list[str] = []
    at = 0
    while at < n:
        if ranges.contains(at):
            at += 1
            continue
        # smallest end in (at, n] with [at, end) overlapping a quoted range
        lo, hi = at + 1, n + 1
        if not ranges.overlaps(at, n):
            parts.append(text[at:])
            break
        while lo < hi:
            mid = (lo + hi) // 2
            if ranges.overlaps(at, mid):
                hi = mid
            else:
                lo = mid + 1
        stop = lo - 1  # first quoted position
        parts.append(text[at:stop])
        at = stop
    return "".join(parts)


def joined_chunks(rows: Iterable[tuple[int, bytes]]) -> str:
    """One field's text from its stored ``(start, body)`` chunks in start order: each
    chunk laid at its own offset, so the overlap two neighbours share appears once."""
    out = ""
    for start, body in rows:
        out = out[:int(start)] + chunk_text(body)
    return out
