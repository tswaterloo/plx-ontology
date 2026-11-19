"""The written forms of one sender/recipient value, for gates that must decide every one.

Read-only, pure functions (no I/O): the GPU OCR gate (``integrations.image_ocr_gpu``)
normalizes each value a captured message names before it asks the people exclusion about
it, and refuses any value it cannot bring to one SMTP address or one Exchange X.500
identity. Kept out of ``integrations.people_exclusion`` on purpose: that module is step
code (``tools/digest_gate.py --files``), and these forms only narrow what the OCR gate
lets out of the box; the exclusion's own matching is unchanged.
"""

from __future__ import annotations

import re
from typing import Any

_EDGES = " \t\r\n\"'`<>;,"
_ANGLE = re.compile(r"<([^<>]*)>")
_PREFIXES = ("smtp:", "x500:", "mailto:")
_IMCEAEX = "imceaex-"
_HEX = frozenset("0123456789abcdef")
X500_PREFIX = "/o="


def _trim(text: str) -> str:
    """Strip the edge characters, a wrapping ``(...)``, an unpaired leading ``(`` or
    trailing ``)`` and a trailing ``.`` (sentence punctuation around an address), until
    nothing changes."""
    while True:
        before = text
        text = text.strip(_EDGES)
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        elif text.startswith("(") and ")" not in text:
            text = text[1:]
        elif text.endswith(")") and "(" not in text:
            text = text[:-1]
        text = text.rstrip(".")
        if text == before:
            return text


def decode_imceaex(value: Any) -> str | None:
    """The Exchange X.500 identity an ``IMCEAEX-<encoded>@<host>`` value encapsulates
    (Exchange's form for an internal identity it could not resolve to SMTP): ``_`` stands
    for ``/`` and ``+XX`` for the character with hex code XX; lowercased. None when the
    value is not exactly one such encoding of a ``/o=...`` identity (a ``+`` without two hex
    digits, a non-printable escape, whitespace, no host, a second ``@``)."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if not lowered.startswith(_IMCEAEX) or lowered.count("@") != 1:
        return None
    encoded, host = lowered[len(_IMCEAEX):].split("@")
    if not encoded or not host or not host.strip(".") or any(c.isspace() for c in lowered):
        return None
    out: list[str] = []
    i = 0
    while i < len(encoded):
        c = encoded[i]
        if c == "_":
            out.append("/")
            i += 1
        elif c == "+":
            code = encoded[i + 1:i + 3]
            if len(code) != 2 or not set(code) <= _HEX:
                return None
            char = chr(int(code, 16))
            if not " " <= char <= "~":
                return None
            out.append(char)
            i += 3
        elif " " < c <= "~":
            out.append(c)
            i += 1
        else:
            return None
    decoded = "".join(out).lower()
    return decoded if decoded.startswith(X500_PREFIX) else None


def normalize_address(value: Any) -> str:
    """One sender/recipient value as a directory keys it: the part in ``<...>`` when it
    holds exactly one, trimmed (``_trim``: whitespace, quotes, angle brackets, ``;``/``,``,
    a trailing ``.``, wrapping parentheses), without an ``smtp:``/``x500:``/``mailto:``
    prefix, lowercased; "" for a non-string or an empty value. An X.500 identity keeps its
    ``/o=...`` form; an ``IMCEAEX-...@host`` one is decoded to that form and kept as given
    (still ``imceaex-...``) when it does not decode, so the caller refuses it."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    inner = _ANGLE.findall(text)
    if len(inner) == 1 and inner[0].strip():
        text = inner[0]
    lowered = _trim(text).lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            lowered = _trim(lowered[len(prefix):])
    if lowered.startswith(_IMCEAEX):
        return decode_imceaex(lowered) or lowered
    return lowered


def is_x500(value: Any) -> bool:
    """An Exchange X.500 identity (legacyExchangeDN, an ``X500:`` proxy address, or its
    ``IMCEAEX`` encoding): a tenant person's or list's, never an outside address."""
    return normalize_address(value).startswith(X500_PREFIX)


def split_addresses(value: str) -> list[str]:
    """The parts of one value that holds several addresses (``;`` or ``,`` between them,
    more than one ``@``); the value itself otherwise (an X.500 identity is never split: it
    may hold ``,`` or ``@``). A part may still hold two addresses (no separator): the
    caller refuses that."""
    if X500_PREFIX not in value.lower() and value.count("@") > 1:
        return [p for p in re.split(r"[;,]", value) if p.strip()]
    return [value]
