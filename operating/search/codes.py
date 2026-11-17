"""The spellings of a code a question names, so that ``PO-106001``, ``PO 106001``,
``PO106001`` and ``106001`` find each other (query side only: the index is unchanged).

Postgres's ``simple`` parser indexes each written form under different lexemes
(``PO-106001``: ``po`` + ``-106001``; ``PO 106001``: ``po`` + ``106001``; ``PO106001``:
``po106001``; ``106001``: ``106001``), and a chunk's ``linked_ids`` hold
``<kind>:<the kind's normal form>`` (``petra_po:PO106001``). A question's code is read
here as its prefix and number and written back in every one of those forms:

* ``lexemes`` (BM25): the ``simple`` lexemes of each spelling;
* ``linked_forms`` (exact id): ``<kind>:<normal form>`` of each spelling for every kind
  of the identifier grammar (``ontology.identifiers``) that accepts it.

A bare number gains a prefix only from the grammar: a prefix one of its kinds requires
(``BC-`` of a batch code), and only when that kind accepts the prefixed number, so
``106001`` reads as ``PO106001`` but never as a batch code. Year-shaped and short bare
numbers (four digits or fewer) gain no prefix: "orders in 2024" is not PO2024.
"""
from __future__ import annotations

import functools
import re
from typing import Iterable, Sequence

# A code as written: an optional short letter prefix (``PO``, ``P.O.``), an optional
# separator (space, ``#``, ``:``, ``-``, ``/``, ``.``), then a number and what follows it
# (``106001``, ``16134A``, ``0003-A``).
_CODE = re.compile(
    r"(?<![\w.])(?:(?P<prefix>[A-Za-z]{1,5}|[A-Za-z]\.[A-Za-z]\.?)"
    r"(?:\s?[#:]\s?|\s?[-_/.]\s?|\s)?)?"
    r"(?P<number>\d[\w\-/]*)", re.ASCII)
# Prefix words that are English, not a code's letters ("for 5394", "box 12").
_NOT_PREFIX = frozenset("""a an and are as at be by for from in into is it no of on or
order orders our the to up was we with box boxes qty pcs per page item items line lines
case cases unit units""".split())
MIN_BARE_PREFIXED = 5      # digits a bare number needs before a prefix is tried
MAX_SPELLINGS = 48
MAX_FORMS = 512


def _clean(text: str) -> str:
    return text.strip(".-/_")


def code_tokens(text: str) -> list[str]:
    """The codes of ``text`` as written and, for a prefix written apart from its number
    (``PO 106001``, ``PO# 106001``, ``P.O. 106001``), also joined (``PO106001``)."""
    out: list[str] = []
    for match in _CODE.finditer(text or ""):
        number = _clean(match.group("number"))
        if not number or not any(ch.isdigit() for ch in number):
            continue
        prefix = (match.group("prefix") or "").replace(".", "")
        whole = _clean(match.group(0))
        if prefix and prefix.lower() in _NOT_PREFIX:
            prefix, whole = "", number
        # a prefixed code keeps its prefix: its bare number is one of its spellings
        # (``spellings``), never a bare number open to every other prefix
        for token in ((whole, prefix + number) if prefix else (number,)):
            if len(token) >= 3 and token not in out:
                out.append(token)
    return out


@functools.lru_cache(maxsize=1)
def _grammar() -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    """(every identifier kind, (prefix, kinds that require it) pairs) of the grammar."""
    from ontology import identifiers
    kinds = identifiers._compiled().kinds
    required: dict[str, list[str]] = {}
    for kind, spec in kinds.items():
        pattern = spec.get("value_pattern")
        if not isinstance(pattern, str):
            continue
        flags = re.IGNORECASE if "i" in str(spec.get("flags") or "") else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error:
            continue
        for form in spec.get("forms") or ():
            example = form.get("example") if isinstance(form, dict) else None
            if not isinstance(example, str):
                continue
            got = re.match(r"([A-Za-z]{1,5})-?\d", example)
            if not got or not compiled.fullmatch(example):
                continue
            swapped = "QZX" + example[len(got.group(1)):]
            if compiled.fullmatch(swapped):
                continue      # the kind takes any letters: not a prefix it requires
            prefix = got.group(1).upper()
            if kind not in required.setdefault(prefix, []):
                required[prefix].append(kind)
    return (tuple(sorted(kinds)),
            tuple(sorted((p, tuple(ks)) for p, ks in required.items())))


def _accepts(kind: str, value: str) -> bool:
    from ontology import identifiers
    try:
        return identifiers.normalize(kind, value) is not None
    except Exception:  # noqa: BLE001 - a value the grammar cannot read is no form
        return False


def spellings(tokens: Iterable[str]) -> list[str]:
    """Every spelling of each code token, as typed first: prefixed codes joined, hyphened
    and bare (``PO-106001``: ``PO-106001``, ``PO106001``, ``106001``); a bare number of
    ``MIN_BARE_PREFIXED`` digits or more (not a year) also with each grammar prefix whose
    kind accepts it (``106001``: ``PO106001``, ``PO-106001``)."""
    out: list[str] = []

    def add(value: str) -> None:
        if len(value) >= 3 and value not in out and len(out) < MAX_SPELLINGS:
            out.append(value)

    for token in tokens:
        token = _clean(str(token or "").strip())
        got = re.fullmatch(r"([A-Za-z]{1,5})[\s#:._/-]*(\d[\w\-/.]*)", token, re.ASCII)
        if got:
            prefix, number = got.group(1).upper(), _clean(got.group(2))
            for value in (token, token.upper(), prefix + number, f"{prefix}-{number}", number):
                add(value)
            continue
        add(token)
        add(token.upper())
        digits = re.match(r"[0-9]+", token)
        if not digits or len(digits.group(0)) < MIN_BARE_PREFIXED \
                or re.fullmatch(r"(19|20)\d\d", token):
            continue
        for prefix, kinds in _grammar()[1]:
            for value in (prefix + token.upper(), f"{prefix}-{token.upper()}"):
                if any(_accepts(kind, value) for kind in kinds):
                    add(value)
    return out


def exact_tokens(tokens: Iterable[str]) -> list[str]:
    """The tokens the exact list may read: a bare number of fewer than
    ``MIN_BARE_PREFIXED`` digits (a year, a quantity, "for 5394") is dropped, so it
    never reaches ``linked_ids`` unprefixed (``bc_document_no:2024``); BM25 still reads it
    as a word. A prefixed code (``PO-2024``) is kept."""
    out: list[str] = []
    for token in tokens:
        token = str(token or "")
        if re.fullmatch(r"[0-9]+", token, re.ASCII) and len(token) < MIN_BARE_PREFIXED:
            continue
        if token and token not in out:
            out.append(token)
    return out


def linked_forms(tokens: Sequence[str]) -> list[str]:
    """``<kind>:<normal form>`` of every spelling of ``tokens`` for each identifier kind
    that accepts it: the shape of a chunk's ``linked_ids``. At most ``MAX_FORMS``."""
    from ontology import identifiers
    kinds = _grammar()[0]
    out: dict[str, None] = {}
    for value in spellings(tokens):
        for kind in kinds:
            try:
                normal = identifiers.normalize(kind, value)
            except Exception:  # noqa: BLE001 - unreadable for this kind: no form
                normal = None
            if normal:
                out[f"{kind}:{normal}"] = None
                if len(out) >= MAX_FORMS:
                    return list(out)
    return list(out)


def lexeme_texts(texts: Iterable[str]) -> list[str]:
    """The spellings of the codes ``texts`` name, for their ``simple`` lexemes (the
    caller keeps only lexemes with a digit: a prefix word is the question's own word or
    none, never ``sh`` or ``capa`` added for a bare number)."""
    tokens: list[str] = []
    for text in texts:
        for token in code_tokens(text):
            if token not in tokens:
                tokens.append(token)
    return spellings(tokens)
