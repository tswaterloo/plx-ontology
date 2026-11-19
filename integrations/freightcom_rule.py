"""The ONE Freightcom detection rule (No Freightcom data reaches an AI model).

A text names Freightcom when, after Unicode NFKD normalisation (accents come apart and are
dropped), removal of invisible format characters and combining marks, and folding of look-alike
Cyrillic/Greek letters and the Turkish dotless i, it holds "freight", then any run of non-letter
characters (none, space, dot, dash, underscore, slash, digits, NBSP, a JSON escape such as
``\\n`` or ``\\u200b``, a URL escape such as ``%2D``, an HTML entity such as ``&#160;``), then
"com", in any case. Each letter may itself be written as an HTML entity (``&#102;``, ``&#x66;``,
``&amp;#102;``), a URL escape (``%66``, ``%2566``) or a JSON escape (``\\u0066``): the pattern
matches those forms, the text is not decoded. So "Freightcom", "freight-com", "Freight.com",
"Freightc\u00f3m", full-width forms, ``x@freightcom.ca`` and "freightcomplete" all match. It also
matches "freight company" and "freight comments": over-matching withholds, which is the safe
side. "freight" and "com" written together match anywhere
(xfreightcom.com); with a separator between them "freight" must not follow a letter, so other
companies' domains such as herculesfreight.com and yrcfreight.com are not withheld.

Python (``names``, ``FREIGHTCOM``) and Postgres (``sql_match``: ``normalize(.., NFKD)``,
``translate``, ``regexp_replace`` and ``~*`` with the same pattern) apply the same steps with
the same constants, so they cannot drift; ``tests/test_freightcom_rule.py`` runs one table
of cases through both, and times both on long runs of every escape kind. The separator
alternatives do not overlap, so matching is linear. Not covered: look-alikes beyond
``_LOOKALIKES``. Callers hand over decoded values (``names`` walks dicts and lists), not their
JSON dumps.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable



def _letter(ch: str) -> str:
    """One letter of the name, plain or written as an HTML entity (``&#102;``, ``&#x66;``,
    ``&amp;#102;``), a URL escape (``%66``, ``%2566``) or a JSON escape (``\\u0066``), in
    either case. Matching the encodings in the pattern (rather than decoding the text) keeps
    Python and Postgres on the one expression."""
    lo, up = ord(ch), ord(ch.upper())
    dec, hx = f"(?:{lo}|{up})", f"(?:{lo:x}|{up:x})"
    return (f"(?:{ch}|&(?:amp;)*#(?:0*{dec}|[xX]0*{hx});|%(?:25)*{hx}|\\\\u00{hx})")


_NAME = lambda word: "".join(_letter(c) for c in word)  # noqa: E731
# between the two words: a non-letter, a JSON escape, a URL escape (%2D) or an entity. The
# alternatives never overlap (a lone ``%``, ``&`` or ``\\`` counts as a plain character only
# when no escape starts there), so every text has one way to match and the matcher cannot
# backtrack exponentially on runs of escapes.
_ESC = r"(?:\\u[0-9a-f]{4}|\\[nrtbf]|%[0-9a-f]{2}|&#?[a-z0-9]{1,12};)"
_SEP = (r"(?:[^a-z%&\\]|%(?![0-9a-f]{2})|&(?!#?[a-z0-9]{1,12};)"
        r"|\\(?!u[0-9a-f]{4}|[nrtbf])|" + _ESC + r")")
# what may come before the separated form: the text start, a non-letter, or an escape that ends in
# a letter (\\n, %2F, \\u002f). Consumed rather than a lookbehind: Postgres runs a multi-character
# or alternated lookbehind about 3x slower on long texts
_START = (r"(?:^|[^a-z]|\\[nrtbf]|%[0-9a-f]{2}|\\u[0-9a-f]{4})")
# two alternatives: "freight" directly followed by "com" matches anywhere (MyFreightcomAccount,
# xfreightcom.com); with one or more separators between, only when "freight" does not follow a
# letter (herculesfreight.com is another company)
PATTERN = ("(?:" + _NAME("freight") + _NAME("com") + "|(" + _START + ")" + _NAME("freight")
           + _SEP + "+" + _NAME("com") + ")")
# invisible format characters and combining marks: dropped before matching, so "fre​ight"
# and a combining accent do not hide the name
_STRIP_RANGES = ((0x00AD, 0x00AD), (0x0300, 0x036F), (0x061C, 0x061C), (0x115F, 0x1160),
                 (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
                 (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
                 (0xFFA0, 0xFFA0), (0xE0000, 0xE007F))
STRIP_CLASS = "[" + "".join(chr(a) if a == b else f"{chr(a)}-{chr(b)}"
                            for a, b in _STRIP_RANGES) + "]"
# look-alike letters of "freightcom" (Cyrillic, Greek), both cases, folded to ASCII
_LOOKALIKES = {"е": "e", "Е": "E", "о": "o", "О": "O", "с": "c",
               "С": "C", "і": "i", "І": "I", "г": "r", "һ": "h",
               "Һ": "H", "ɡ": "g", "ı": "i", "İ": "I", "ο": "o", "Ο": "O", "ι": "i",
               "Ι": "I", "ε": "e", "Ε": "E", "т": "t", "Т": "T",
               "м": "m", "М": "M", "ѕ": "s", "х": "x"}
_FROM = "".join(_LOOKALIKES)
_TO = "".join(_LOOKALIKES.values())
_STRIP = re.compile(STRIP_CLASS)
_MAP = str.maketrans(_LOOKALIKES)
_RX = re.compile(PATTERN, re.I)


def fold(text: str) -> str:
    """``text`` as the rule reads it (NFKD, so accents come apart and are dropped with the
    invisibles; look-alikes and Turkish dotless i folded)."""
    return _STRIP.sub("", unicodedata.normalize("NFKD", text)).translate(_MAP)


class Rule:
    """``FREIGHTCOM``: a drop-in for the old compiled pattern (``search``, ``sub``,
    ``pattern``) that reads the folded text."""

    pattern = PATTERN

    def search(self, text: str) -> "re.Match[str] | None":
        return _RX.search(fold(text))

    def sub(self, repl: str, text: str) -> str:
        """``text`` with every name replaced; text that names nothing comes back unchanged
        (the fold is only for detecting)."""
        folded = fold(text)
        if not _RX.search(folded):
            return text
        # the leading context character(s) of the separated form are kept, not replaced
        return _RX.sub(lambda m: (m.group(1) or "") + m.expand(repl), folded)


FREIGHTCOM = Rule()


def _strings(value: Any):
    if value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, (bytes, bytearray)):
        yield bytes(value).decode("utf-8", "replace")
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _strings(v)
    else:
        yield str(value)


def names(*values: Any) -> bool:
    """Whether any string in ``values`` (strings, or dicts and lists of them) names Freightcom."""
    return any(_RX.search(fold(s)) for v in values for s in _strings(v))


def sql_match(expr: str, bind: Callable[[Any], str]) -> str:
    """A SQL boolean (NULL on NULL ``expr``) that is true when the text ``expr`` names
    Freightcom: the same fold and pattern, ``bind(value)`` giving each parameter's placeholder.
    Needs PostgreSQL 13+ on a UTF8 database (``normalize``)."""
    return (f"(regexp_replace(translate(normalize(({expr})::text, NFKD), {bind(_FROM)}, "
            f"{bind(_TO)}), {bind(STRIP_CLASS)}, '', 'g') ~* {bind(PATTERN)})")
