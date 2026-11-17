"""Figures and quotes in prose: structured text, figures and their units and currencies,
verbatim quotes, labels, page locators, asked tokens and written dates."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
import bisect
import json
import re
import unicodedata
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, NamedTuple, Sequence

from ontology.units import ISO_4217, SIGN_CURRENCIES, WORD_CODES

from .claim_checks_grammar import (
    _ANSWERED_NO, _AT_LEAST_AFTER, _AT_LEAST_BEFORE, _BOUND_AFTER, _BOUND_BEFORE, _CLAUSE_END,
    _CODE_NAME, _COMPOUND, _CURRENCY_AFTER, _CURRENCY_BEFORE, _CURRENCY_SIGNS, _DATE_WORDS,
    _DIGIT_JOINERS, _DIGITS_ONLY_ID, _DOZEN, _EXCLUDED_BEFORE, _GLUED, HANDLE, _HEADING_LABEL,
    _ID_LABEL_BEFORE, IDENTIFIER, ISO_DATE, _ISO_DAY, _JOINERS, KNOWN_ID_PREFIXES,
    _KNOWN_RATES, _KNOWN_UNITS, LABEL, _LABEL_PREFIX, _LAST_WORD, _LETTERS, _MONTH,
    _MONTH_DATE, _MONTHS, _NAME_FILLER, _NEGATED_AFTER, _NEGATOR, _NOT_PLURAL,
    _NOT_RECORD_NOUNS, NUMBER, _NUMBER_LABEL, _NUMBER_WORDS, _ORDINAL, _PAGE_CITATION,
    _PAGE_PHRASE, PAGE_TEXT, _page_unit, _PAGE_UNITS, _PERCENT_AFTER, _PERCENT_UNITS,
    _PERCENT_WORDS, _PHONE, _PROSE_MOMENT, _QUANTITY_LABELS, QUANTITY_ROLES, _RANGE_AFTER,
    _RANGE_BEFORE, _RECORD_NOUNS, _SCALE_AFTER, _SCALE_WORDS, _SCALES, _SCALES_OF_SUFFIX,
    SET_HANDLE, _SMALL, _STATE_BEFORE, _STREET_AFTER, _TENS, TEXT_FIELD_NAMES, _TIME,
    _TOKEN_JOINER, _UNIT_AFTER, _WORD_CHAR, _WORD_RUN,
)
from .claim_checks_parse import dig, number_value, _plain, rounded_from, to_decimal
from .claim_checks_cells import (
    _as_stored, _collection, _covers, _fail, _fold, _moment, _moment_of, _not_in_quote,
    _record, stamp_zone,
)

if TYPE_CHECKING:
    from .claim_checks_cells import _Dated
    from .claim_checks import _Evidence


def structured_values(cell: Any) -> list[tuple[str | None, str]] | None:
    """The values a structured cell holds (a JSON object or array, or a string holding
    one), each with its key, in order; None for any other cell."""
    if isinstance(cell, str):
        held = cell.strip()
        if held[:1] not in ("{", "[") or held[-1:] not in ("}", "]"):
            return None
        try:
            cell = json.loads(held)
        except ValueError:
            return None
    if not isinstance(cell, (Mapping, list)):
        return None
    out: list[tuple[str | None, str]] = []

    def walk(value: Any, key: str | None) -> None:
        if isinstance(value, Mapping):
            for name, inner in value.items():
                walk(inner, str(name))
        elif isinstance(value, list):
            for inner in value:
                walk(inner, key)
        elif value is not None and not isinstance(value, bool):
            out.append((key, str(value)))
    walk(cell, None)
    return out


def structured_text(cell: Any) -> str | None:
    """The text of a structured cell (``structured_values``): each value it holds as
    ``key: value``, one per line, in order; None for any other cell, or one holding no
    value. What it says is checked as a record's free text is (a quote of it, a figure
    of it), and a value it holds whole as that value, never the whole cell as one."""
    held = structured_values(cell)
    if not held:
        return None
    return "\n".join(f"{key}: {value}" if key else value for key, value in held)


def text_field(field: str) -> bool:
    """Whether a claim's ``field`` is a record's free text (``row.content_text``,
    ``row.description``, ``row.body``; ``TEXT_FIELD_NAMES``), whatever the record."""
    return isinstance(field, str) and field.startswith("row.") \
        and field.rsplit(".", 1)[-1] in TEXT_FIELD_NAMES


class _Figure(NamedTuple):
    """One number of prose as ``_text_figures`` reads it."""
    start: int
    end: int
    value: Decimal          # at its written scale and scale word: 1.5 million, 25% = 0.25
    percent: bool
    supports: bool          # not negated, bounded, a range end or a label's number
    word: str | None        # the word printed right after it (its unit or noun)
    currency: str | None    # printed beside it: an ISO code, or "$" for a bare dollar
    meaning: str | None     # its currency and stemmed word: two meanings are ambiguous


def _joined(text: str, start: int, end: int) -> bool:
    """Whether the number at ``text[start:end]`` is a list marker (``(5)``) or part of a
    longer token: glued to a word character, or joined by ``-``, ``/``, ``.``, ``:`` and
    the like to one
    (``ABC12345``, ``10/14/2026``, ``905-555-0187``, ``v2.1.3``, ``1st``); a glued unit
    (``500ml``, ``10kg``) keeps it a figure."""
    before = text[start - 1] if start else ""
    if before == "(" and text[end:end + 1] == ")":
        return True        # "(5)": a list marker
    if before == "," and start > 1 and text[start - 2].isdigit() or \
            re.match(r",\d", text[end:end + 2]):
        return True        # "1,2": a decimal comma, or a list of numbers glued by commas
    if before.isalnum() or before == "_" or before in _TOKEN_JOINER and (
            before in "#@_" or start > 1 and text[start - 2].isalnum()):
        return True
    if _COMPOUND.match(text, end):
        return False       # "30-day", "12-pack": a figure of a compound word
    if _rate_unit(text, end) is not None:
        return False       # "5.02/kg", "45.70/dz": a figure per a unit of measure
    after = text[end:end + 2]
    if after[:1].isdigit() or after[:1] == "_" or after[:1] in _TOKEN_JOINER \
            and after[1:2].isalnum():
        return True
    glued = re.match(r"[A-Za-z]+", text[end:])
    if glued is not None and _GLUED_CODE.fullmatch(glued.group(0)) \
            and glued.group(0) in ISO_4217 and glued.group(0) not in WORD_CODES:
        return False       # "14,074.15CAD": a currency code written against its amount
    return glued is not None and (_ORDINAL.fullmatch(glued.group(0)) is not None
                                  or glued.group(0).casefold() not in _PAGE_UNITS)


# The unit of a rate written against its figure: ``/`` and a unit of measure of two
# letters or more the page-unit table knows (``5.02/kg``, ``45.70/dz``), ending the token
# (``5/02``, ``14/Aug``, ``1/2``, ``kg/m3`` stay parts of a longer token).
_RATE_AFTER = re.compile(r"/([A-Za-z]{2,})(?![\w'\u2019]|[-/\\.:@#][\w])")
_GLUED_CODE = re.compile(r"[A-Z]{3}")


def _rate_unit(text: str, end: int) -> str | None:
    """The canonical rate unit (``/kg``) written right after the figure ending at
    ``end`` (``_RATE_AFTER``), or None: never a percent or the count words unit(s)."""
    match = _RATE_AFTER.match(text, end)
    if match is None:
        return None
    word = match.group(1).casefold()
    unit = _PAGE_UNITS.get(word)
    if unit is None or unit == "percent" or word in ("unit", "units"):
        return None
    return "/" + unit


def rate_unit(unit: Any) -> str | None:
    """A claim's ``unit`` as a rate (``1/kg``, ``USD/kg``, ``/kg``, ``per kg``,
    ``per kilogram``: ``/kg``), or None when it names no rate of a known unit."""
    if not isinstance(unit, str):
        return None
    match = re.fullmatch(r"\s*(?:(?:1|[A-Z]{3})?\s*/|per\s+)\s*([A-Za-z]{2,})\s*", unit)
    if match is None:
        return None
    return _rate_unit("/" + match.group(1), 0)


@lru_cache(maxsize=8)
def _text_figures(text: str, breaks: tuple[int, ...] = ()) -> tuple[_Figure, ...]:
    """Every digit number of a normalised prose ``text`` with what it states
    (``_Figure``), outside handles, ISO and month-name dates, times, identifiers and
    phone numbers (blanked in place). A number supports no claim of its value when a
    negator reverses it (``not 12``, ``excluding 30``, ``30 cases? No.``), a bound or
    approximation qualifies it (``max 30``, ``30 or fewer``, ``about 30``), it ends a
    range (``20-30``, ``between 20 and 30``), it is a label's number (``SKU 4410``,
    ``Invoice #4521``, ``PO 4521``, ``page 3``) or part of a longer token
    (``_joined``). A heading label's number (``_HEADING_LABEL``: ``Release 2:`` at a
    clause start) is no figure at all. A scale word scales it (``1.5 million``). ``breaks``:
    where the normalised text joined two lines (``_line_breaks``): the words that give a
    figure a meaning (its label, unit, currency, the word after it) come from its own line
    only, so "... 5,984" ending a line never means the "STAND" opening the next; the words
    that refuse or bound it (a negator, exclusion, bound, at least or range before it; a
    range, bound or negating answer after it) reach across one line break each way, as
    wrapped text goes on. Cached: one cited text serves many
    claims."""
    clean = text
    for pattern in (HANDLE, _PROSE_MOMENT, ISO_DATE, _DATE_WORDS, _TIME, IDENTIFIER, _PHONE):
        clean = pattern.sub(lambda match: " " * len(match.group(0)), clean)
    from ontology import identifiers   # the one grammar's labelled and shaped mentions
    for mention in identifiers.find_identifiers(text):
        clean = clean[:mention.start] + " " * (mention.end - mention.start) + clean[mention.end:]
    for heading in _HEADING_LABEL.finditer(clean):
        start, end = heading.span("number")
        clean = clean[:start] + " " * (end - start) + clean[end:]
    out = []
    for match in NUMBER.finditer(clean):
        start, end = match.start(), match.end()
        if match.group("sign") and start and not text[start - 1].isspace():
            start += 1     # "20-30": the dash joins, it is no sign
        at = bisect.bisect_left(breaks, start)
        line = breaks[at - 1] + 1 if at else 0
        after = bisect.bisect_left(breaks, end)
        stop = breaks[after] if after < len(breaks) else len(text)
        # and after it, across one line break too ("20\nto 30", "900\nmax")
        beyond = breaks[after + 1] if after + 1 < len(breaks) else len(text)
        window = max(line, start - 48)
        # Words that refuse or bound a figure reach across one line break before it:
        # wrapped text goes on there ("is not\n900"); line scope only narrows ambiguity.
        reach = max(breaks[at - 2] + 1 if at > 1 else 0, start - 48)
        value = number_value(match) if start == match.start() else -number_value(match)
        scale = (match.group("scale") or "").strip("  ").lower()
        tail = end
        scaled = _SCALE_AFTER.match(text, end, stop) if not scale else None
        if scaled is not None:
            value *= _SCALE_WORDS[scaled.group(1).lower()]
            tail = scaled.end()
        label = _LAST_WORD.search(text, window, start)
        quantity_label = label is not None and label.group(1).casefold() in _QUANTITY_LABELS
        after = _COMPOUND.match(text, tail, stop) or _UNIT_AFTER.match(text, tail, stop)
        word = " ".join(after.group(1).casefold().split()) if after else None
        rate = _rate_unit(text, tail)
        if rate is not None:
            word = rate      # "5.02/kg" states a figure per kg, never 5.02 kg
        digits = match.group("int") or ""
        plain = digits.isdigit() and not match.group("frac") and not scale and tail == end
        before = _CURRENCY_BEFORE.search(text, max(line, start - 8), start)
        trailing = _CURRENCY_AFTER.match(text, tail, stop)
        currency = None
        if before is not None:
            currency = before.group("code") if before.group("code") in ISO_4217 \
                else _CURRENCY_SIGNS.get(before.group("sign") or "")
        if currency is None and trailing is not None and trailing.group("code") in ISO_4217 \
                and trailing.group("code") not in WORD_CODES:   # "12 KGS" is kilograms
            currency = trailing.group("code")
        counted = quantity_label or currency is not None or word is not None and (
            rate is not None or word in _PAGE_UNITS or word in _QUANTITY_LABELS or _stem(word) in _QUANTITY_LABELS)
        supports = not (
            _joined(text, start, end)
            or _NEGATOR.search(text, reach, start) or _EXCLUDED_BEFORE.search(text, reach, start)
            or _NEGATED_AFTER.match(text, tail, beyond) or _ANSWERED_NO.match(text, tail, beyond)
            or _BOUND_BEFORE.search(text, reach, start) or _BOUND_AFTER.match(text, tail, beyond)
            or _AT_LEAST_BEFORE.search(text, reach, start) or _AT_LEAST_AFTER.match(text, tail, beyond)
            or _RANGE_BEFORE.search(text, reach, start) or _RANGE_AFTER.match(text, tail, beyond)
            or not quantity_label and _ID_LABEL_BEFORE.search(text, window, start)
            or _STREET_AFTER.match(text, tail, stop)
            # A bare year (1900-2099) or a zip after a state code, unless counted.
            or plain and not counted and len(digits) == 4 and 1900 <= int(digits) <= 2099
            or plain and not counted and len(digits) == 5
            and _STATE_BEFORE.search(text, window, start))
        if currency is not None and word is not None and word.upper() == currency:
            word = None
        percent = scale in _PERCENT_WORDS
        meaning = "%" if percent else _stem(word) if word else None
        out.append(_Figure(start, end, value, percent, supports, word, currency,
                           f"{currency or ''} {meaning or ''}".strip() or None))
    return tuple(out)


def figure_words(text: str) -> list[tuple[Decimal, int, int, str]]:
    """``(value, start, end, word)`` of each word prose ``text`` prints right after a
    figure (``_text_figures``' word: its unit or noun, case-folded), ``text[start:end]``
    the word with the space before it; never a percent."""
    out = []
    for figure in _text_figures(text):
        after = _UNIT_AFTER.match(text, figure.end)
        if figure.word is None or figure.percent or after is None \
                or " ".join(after.group(1).casefold().split()) != figure.word:
            continue
        out.append((figure.value, figure.end, after.end(), figure.word))
    return out


def unit_word(word: str) -> str | None:
    """The unit of measure one written word means by the claim checks' unit table
    (``_PAGE_UNITS``: "kilo" is kg, "lbs" lb), None for any other word, a percent or the
    count words unit(s). A fold of spellings only: no unit is ever converted to another."""
    if not isinstance(word, str) or word.casefold() in ("unit", "units"):
        return None
    unit = _PAGE_UNITS.get(" ".join(word.casefold().split()))
    return None if unit == "percent" else unit


def unit_words(text: str) -> list[tuple[Decimal, int, int, str]]:
    """``(value, start, end, unit)`` of each unit word prose ``text`` prints right after a
    figure (``figure_words``; ``_PAGE_UNITS``: "68,160 grams" is g), ``text[start:end]``
    the word with the space before it; never a percent or the count word unit(s), which
    names no unit of measure."""
    return [(value, start, end, unit) for value, start, end, word in figure_words(text)
            if (unit := _PAGE_UNITS.get(word)) not in (None, "percent")
            and word not in ("unit", "units")]


def currency_words(text: str) -> list[tuple[Decimal, int, int, str]]:
    """``(value, start, end, currency)`` of each currency prose ``text`` prints beside a
    figure (``_text_figures``: an ISO code or a sign before it, a code after it),
    ``text[start:end]`` the code or sign with the space between it and the figure;
    ``currency`` the ISO code, or the sign (``$``, ``¥``) where it names no one currency."""
    return [(value, start, end, currency)
            for value, _figure, start, end, currency in _figure_currencies(text)]


def _figure_currencies(text: str) -> list[tuple[Decimal, tuple[int, int], int, int, str]]:
    """``currency_words`` with each figure's own span (``_text_figures``)."""
    out = []
    for figure in _text_figures(text):
        if figure.currency is None:
            continue
        before = _CURRENCY_BEFORE.search(text, max(0, figure.start - 8), figure.start)
        after = _CURRENCY_AFTER.match(text, figure.end)
        named = None
        if before is not None:
            named = before.group("code") if before.group("code") in ISO_4217 \
                else _CURRENCY_SIGNS.get(before.group("sign") or "")
        span = (figure.start, figure.end)
        if named is not None and named == figure.currency:
            out.append((figure.value, span, before.start(), before.end(), figure.currency))
        elif after is not None and after.group("code") == figure.currency:
            out.append((figure.value, span, after.start(), after.end(), figure.currency))
    return out


def same_currency(written: str, checked: str) -> bool:
    """Whether a currency written beside a figure (``currency_words``) agrees with a
    checked ISO code: the same code, or a sign that writes it (``$`` a dollar, ``¥`` the
    yen or yuan; ``ontology.units.SIGN_CURRENCIES``), the code in any case. Anything else
    contradicts it."""
    checked = checked.strip().upper()
    return written.upper() == checked or checked in SIGN_CURRENCIES.get(written, ())


def _check_text_figure(claim: Mapping[str, Any], out: dict[str, Any], cell: str,
                       where: str, unit_basis: str = "text") -> dict[str, Any]:
    """A number claimed on prose: a record's free text (``text_field``) or an original
    page's machine-read text (``_check_page_claim``), one rule. The value must be a
    figure of the text as re-read now that supports it (``_text_figures``: by value at
    its written scale, ``25%`` is 25 for a claim in percent; not negated, bounded, a
    range end, a label's number or inside a token), with the claim's unit and currency
    where it states them; a ``quote`` pins the occurrence (only figures inside it count).
    Two supporting occurrences with different meanings (``30 cases``, ``30 days``) are
    ambiguous: the claim must quote the passage it means. A figure of prose grounds no
    quantity type: untyped, a quantity role is ``claim_role_unverified``."""
    text = _normalised_text(cell)
    value = to_decimal(claim["value"])
    claimed = (rate_unit(claim["unit"]) or _page_unit(claim["unit"])) if "unit" in claim \
        else None
    percent = claimed == "percent"
    found = [figure for figure in _text_figures(text, _line_breaks(cell))
             if (figure.value * 100 if figure.percent and percent else figure.value) == value]
    if "quote" in claim:
        want = _normalised(claim["quote"])
        spans = [(at, at + len(want)) for at in _spans(want, text)]
        if not spans:
            return _fail(out, "claim_value_mismatch",
                         f"{where} does not hold the quote verbatim on token boundaries")
        found = [figure for figure in found
                 if any(start <= figure.start and figure.end <= end for start, end in spans)]
        if not found:
            return _fail(out, "claim_value_mismatch",
                         f"{_not_in_quote(claim['value'])} (as a figure of {where})")
    if not found:
        return _fail(out, "claim_value_mismatch", f"{where} does not hold {claim['value']}")
    found = [figure for figure in found if figure.supports]
    if not found:
        return _fail(out, "claim_value_mismatch", f"{where} holds {claim['value']} only "
                     "negated, as a bound or range (not, max, at least, 20-30 ...), as a "
                     "label's number (SKU: 4410), a year, a street number or zip, a "
                     "decimal comma (1,2, ambiguous) or part of a code, date or phone number")
    if claimed is not None:
        def printed(figure: _Figure) -> str | None:
            if figure.percent:
                return "percent"
            if figure.word is None:
                return None
            if figure.word.startswith("/") or claimed.startswith("/"):
                # A rate (``/kg``) is verified only by the same rate, and contradicts any
                # other unit: 5.02/kg is no 5.02 kg, and 120 kg no 120/kg.
                return figure.word if figure.word in _KNOWN_RATES or claimed in _KNOWN_RATES \
                    else None
            if _stem(figure.word) == _stem(claimed):
                return claimed
            # A known page unit contradicts only a claimed unit the table knows too; a
            # claim's spelling it does not know ("grs") is left unverified, never refused.
            known = _PAGE_UNITS.get(figure.word)
            return known if claimed in _KNOWN_UNITS else None
        units = [printed(figure) for figure in found]
        if claimed in units:
            found = [figure for figure, unit in zip(found, units) if unit == claimed]
            out["unit_basis"] = unit_basis
        elif None in units:
            found = [figure for figure, unit in zip(found, units) if unit is None]
            out["unit_basis"] = "unverified"
        else:
            return _fail(out, "claim_unit_mismatch", f"{where} prints {claim['value']} in "
                         f"{', '.join(sorted(set(map(str, units))))}, the claim says "
                         f"{claim['unit']}")
    if "currency" in claim:
        stated = claim["currency"]
        printed_in = sorted({figure.currency or "no currency" for figure in found
                             if figure.currency != stated})
        found = [figure for figure in found if figure.currency == stated]
        if not found:
            return _fail(out, "claim_currency_mismatch", f"{where} prints {claim['value']} "
                         f"with {' and '.join(printed_in)} (a bare $ names no single "
                         f"currency), the claim says {stated}")
    meanings = sorted({figure.meaning or "(no word after it)" for figure in found})
    if len(meanings) > 1:
        return _fail(out, "claim_value_mismatch", f"{claim['value']} is ambiguous: {where} "
                     f"holds it {len(found)} times with different meanings "
                     f"({'; '.join(meanings)[:200]}); quote the passage that holds the one "
                     "meant")
    if claim.get("role") in QUANTITY_ROLES:
        return _fail(out, "claim_role_unverified", f"a figure in {where} has no role "
                     f"contract, so the claimed {claim['role']} cannot be verified")
    return {**out, "status": "passed", "role_basis": "untyped", "quantity_type": None,
            "value_basis": "text_figure"}


# A run of spaces and pipes holding at least one pipe: a table's cell break, however many
# empty cells or spaces it spans ("|  |  |" of one row, "|" of another, as an agent copies
# or a page renders it).
_CELL_BREAK = re.compile(r"\s*\|[\s|]*")


_LINE_ENDS = frozenset("\n\r\f\v\x1c\x1d\x1e\x85\u2028\u2029")


@lru_cache(maxsize=8)
def _line_breaks(text: str) -> tuple[int, ...]:
    """The positions of ``_normalised(text)`` where it joined two lines of ``text``: the one
    space a white-space run holding a line end became (a run inside a table cell break is
    the cell break)."""
    text = unicodedata.normalize("NFKC", text)
    if "|" in text:
        text = _CELL_BREAK.sub(" | ", text)
    out, length, ended, started = [], 0, False, False
    for token in re.split(r"(\s+)", text):
        if not token:
            continue
        if token.isspace():
            ended = ended or any(char in _LINE_ENDS for char in token)
            continue
        if started:
            if ended:
                out.append(length)
            length += 1
        length += len(token)
        started, ended = True, False
    return tuple(out)


def _normalised(text: str) -> str:
    """``text`` NFKC-normalised with every white-space run one space and every table cell
    break (``_CELL_BREAK``: a run of spaces, tabs and pipes) one `` | ``, so a table-row
    quote is verbatim whatever empty cells or spacing it was copied with."""
    text = unicodedata.normalize("NFKC", text)
    if "|" in text:
        text = _CELL_BREAK.sub(" | ", text)
    return " ".join(text.split())


def verbatim_quote(quote: str, text: str) -> str | None:
    """The passage of ``text`` as written that a ``quote`` matched (``_normalised``: its
    white space and table cell breaks may differ, "Total |  | 1,200" for the row's "Total |
    1,200"), the first occurrence on token boundaries; None when it holds none."""
    want = _normalised(quote)
    if not want:
        return None
    parts = []
    for piece in re.split(r"( ?\| ?| )", want):
        if "|" in piece:
            parts.append(r"\s*?\|[\s|]*?")
        elif piece == " ":
            parts.append(r"\s+")
        elif piece:
            parts.append(re.escape(piece))
    pattern = re.compile("".join(parts))
    for source in (text, unicodedata.normalize("NFKC", text)):
        for found in pattern.finditer(source):
            span = found.group(0).strip()
            if _normalised(span) != want:
                continue
            # The shortest such passage: no cell break of the row before or after it.
            while span[:1] == "|" and _normalised(span[1:].lstrip()) == want:
                span = span[1:].lstrip()
            while span[-1:] == "|" and _normalised(span[:-1].rstrip()) == want:
                span = span[:-1].rstrip()
            return span
    return None


# One cited text is quoted by many claims of a case: normalised once.
_normalised_text = lru_cache(maxsize=4)(_normalised)


def _splits_token(text: str, at: int) -> bool:
    """Whether position ``at`` of ``text`` lies inside one token: between two word
    characters, or next to a joiner between two (``re|-quote``, ``1,|509``)."""
    def word(index: int) -> bool:
        return 0 <= index < len(text) and _WORD_CHAR.match(text[index]) is not None

    def joins(index: int) -> bool:
        """Whether ``text[index]`` joins the characters on either side into one token."""
        if not (0 < index < len(text) - 1 and text[index] in _JOINERS
                and word(index - 1) and word(index + 1)):
            return False
        return text[index] not in _DIGIT_JOINERS or (
            text[index - 1].isdigit() and text[index + 1].isdigit())
    return (word(at - 1) and word(at)) or joins(at) or joins(at - 1)


def _spans(want: str, text: str) -> list[int]:
    found, at = [], text.find(want) if want else -1
    while at >= 0:
        if not _splits_token(text, at) and not _splits_token(text, at + len(want)):
            found.append(at)
        at = text.find(want, at + 1)
    return found


def quoted_spans(value: str, text: str) -> int:
    """How often ``value`` occurs verbatim in ``text``: both NFKC-normalised with every
    whitespace run one space, starting and ending on token boundaries (``moved it to Jan 9.``
    is in ``...moved it to Jan 9.``; ``quote`` is not in ``re-quote``, ``Jan 9`` not in
    ``Jan 9,2026``, ``supplier`` not in ``suppliers``). Case and punctuation are kept."""
    return len(_spans(_normalised(value), _normalised_text(text)))


def _quotable(want: str) -> bool:
    """Whether a normalised quote is long enough to tell one passage from another: two
    word tokens (whitespace-separated, each with a letter or digit: ``Jan 9``), one token
    of at least eight characters (``approved``), or an identifier-shaped token of at least
    four with a digit or capital: letters with digits or two joined word runs (``PO5447``,
    ``LOT-FXA``, ``2026-07``); never ``PO``, ``USD``, ``e.g.``, ``1`` or ``.``."""
    words = sum(_WORD_CHAR.search(token) is not None for token in want.split(" "))
    if words != 1:
        return words >= 2
    return len(want) >= 8 or _identifier_shaped(want)


def _identifier_shaped(token: str) -> bool:
    """At least four characters with a digit or capital: letters with digits or two
    joined word runs (``PO5447``, ``LOT-FXA``, ``2026-07``)."""
    digit, alpha = any(c.isdigit() for c in token), any(c.isalpha() for c in token)
    return len(token) >= 4 and (digit or any(c.isupper() for c in token)) \
        and (len(_WORD_RUN.findall(token)) >= 2 or digit and alpha)


def _negated(want: str, text: str, at: int) -> bool:
    """Whether the occurrence of ``want`` at ``at`` of ``text`` is negated: a negator
    directly before it (not the label abbreviation ``Lot No`` before an identifier) or a
    negating answer directly after it (``QC passed? No``)."""
    start = max(0, at - 40)
    if _NEGATOR.search(text, start, at) and not (
            _NUMBER_LABEL.search(text, start, at) and _identifier_shaped(want.split(" ")[0])):
        return True
    return _NEGATED_AFTER.match(text, at + len(want)) is not None


def supported_quotes(value: str, text: str) -> int:
    """How many occurrences of ``value`` in ``text`` support it as a quote: its
    ``quoted_spans`` that no negator reverses (``_negated``: ``approved`` in ``not
    approved`` or ``approved? No``; a quote that includes the negator, ``not approved``,
    is supported; ``No`` after a label before an identifier, ``Lot No LOT-FXA``, is the
    abbreviation of number), and none at all for a quote too short to tell passages apart
    (``_quotable``)."""
    want, text = _normalised(value), _normalised_text(text)
    if not _quotable(want):
        return 0
    return sum(not _negated(want, text, at) for at in _spans(want, text))


def _quote_problem(value: str, text: str, where: str) -> str | None:
    if not _quotable(_normalised(value)):
        return (f"{value} is too short to verify as a quote: quote at least two words or "
                "eight characters of the passage")
    if not supported_quotes(value, text):
        return (f"{where} does not hold {value} verbatim on token boundaries without a "
                "negator directly before or after it")
    return None


def _number_words(text: str) -> list[tuple[str, Decimal]]:
    return [(written, value) for _start, written, value in _number_word_spans(text)]


def _number_word_spans(text: str) -> list[tuple[int, str, Decimal]]:
    """(start, as written, value) of each number in words of ``text`` from ten up."""
    found = []
    for match in _NUMBER_WORDS.finditer(text):
        total = current = 0
        for word in re.split(r"[- ]+", match.group(0).lower().strip(" -")):
            if word in _SMALL:
                current += _SMALL[word]
            elif word in _TENS:
                current += _TENS[word]
            elif word == "hundred":
                current = max(current, 1) * 100
            elif word in _SCALES:
                total += max(current, 1) * _SCALES[word]
                current = 0
        if total + current >= 10:
            written = match.group(0).rstrip(" -")
            percent = _PERCENT_AFTER.match(text, match.start() + len(written))
            value = Decimal(total + current)
            found.append((match.start(), (written + percent.group(0)).strip(),
                          value * Decimal("0.01")) if percent
                         else (match.start(), written.strip(), value))
    return found


def _stem(word: str) -> str:
    """A word without its plural ending (``receipts``, ``boxes``, ``deliveries``)."""
    word = word.lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 3 \
        else word


def _count_nouns(claim: Mapping[str, Any], evidence: _Evidence) -> frozenset[str]:
    """The words that name what a count claim counts: its role's (``receipt_count``), its
    cited records' collections' (``public.app_receipt``, a BC ``purchaseReceipts``) and
    any records' (``_RECORD_NOUNS``), as stems."""
    names = [str(claim.get("role") or "")]
    for given in claim["of"]:
        resolved = evidence.row(given["citation"]) or {}
        names.append(" ".join(_collection(_record(given["citation"], resolved))[1:]))
    words = {_stem(part) for name in names
             for part in _LETTERS.findall(re.sub(r"([a-z])([A-Z])", r"\1 \2", name))
             if len(part) > 2}
    return frozenset(words - _NAME_FILLER) | _RECORD_NOUNS


def _quantifies_other(text: str, end: int, nouns: frozenset[str]) -> bool:
    """Whether the number in words ending at ``end`` counts something that is no record:
    the next words of its clause (a hyphen compound's too: ``three-receipt``) name none of
    ``nouns`` and their first plural noun is in ``_NOT_RECORD_NOUNS`` (``held in two
    systems``). A number bound to no noun (``Receipts: three.``, ``Three arrived``) or to
    any other plural noun (``Three shipments arrived``) may state the count."""
    window = _CLAUSE_END.split(text[end:end + 60], 1)[0]
    words = [word.lower() for word in _LETTERS.findall(window)][:3]
    if any(_stem(word) in nouns for word in words):
        return False
    plural = next((word for word in words if len(word) > 3 and word.endswith("s")
                   and not word.endswith("ss") and word not in _NOT_PLURAL), None)
    return plural is not None and _stem(plural) in _NOT_RECORD_NOUNS


def _word_spans(text: str, value: Decimal) -> list[tuple[int, str, Decimal]]:
    """(start, as written, value) of each number in words of ``text`` that may be ``value``:
    from ten up (``_number_word_spans``), the value's own word below ten (``Three``, the
    ``three`` of ``three-receipt``; not ``one`` of ``twenty-one``) and dozens (``a dozen``)."""
    spans = [span for span in _number_word_spans(text) if span[2] == value]
    word = next((name for name, small in _SMALL.items() if 0 < small < 10 and small == value),
                None)
    if word is not None:
        spans += [(match.start(), match.group(0), value) for match in re.finditer(
            r"(?<![\w-])" + word + r"(?=-[A-Za-z]|(?![\w-]))(?![ -]dozen\b)", text, re.I)]
    for match in _DOZEN.finditer(text):
        times = match.group("times")
        factor = Decimal(1) / 2 if match.group("half") else Decimal(
            times if times.isdigit() else _SMALL.get(times.lower(), _TENS.get(times.lower(), 0))
        ) if times else Decimal(1)
        if factor and factor * 12 == value:
            spans.append((match.start(), match.group(0), value))
    return spans


def _stated_exactly(text: str, value: Decimal, pages: frozenset[int],
                    nouns: frozenset[str]) -> list[str]:
    """Each number of ``text`` (as written, in text order) that is ``value`` written as an
    exact number, not as a lower bound (``_AT_LEAST_BEFORE`` / ``_AT_LEAST_AFTER``),
    outside handles, identifiers, dates and page locators (``prose_numbers``): in digits
    always; in words too, unless they count something that is no record
    (``_quantifies_other``) while another number of the text states ``value``."""
    clean = _strip(_page_locators(text, pages, [value]),
                   lambda match: " " * len(match.group(0)))
    spans = [(match.start(), match.group(0), number_value(match), False)
             for match in NUMBER.finditer(clean)]
    spans += [(start, written, number, True) for start, written, number
              in _word_spans(clean, value)]
    spans = [(start, written, words and _quantifies_other(clean, start + len(written), nouns))
             for start, written, number, words in sorted(spans)
             if number == value and not _percent_written(written)]
    stated = not all(other for _start, _written, other in spans)
    return [written for start, written, other in spans
            if not (other and stated)
            and not _AT_LEAST_BEFORE.search(clean, max(0, start - 40), start)
            and not _AT_LEAST_AFTER.match(clean, start + len(written))]


def _names(match: re.Match[str]) -> str:
    """A known id prefix with digits is a name (blanked); otherwise keep the digits apart."""
    prefix = match.group(1)
    # Case item ids are glued (F2, P1); a single letter with a hyphen is a code (P-02458).
    if prefix in KNOWN_ID_PREFIXES and (len(prefix) > 1 or "-" not in match.group(0)):
        return " " * len(match.group(0))
    return match.group(1) + " " + match.group(2)


def _identifier(blank: Callable[[re.Match[str]], str]) -> Callable[[re.Match[str]], str]:
    """An identifier is blanked, except capitals + digits alone (``SKU-1509``,
    ``ABC12345``) under a prefix that is not a known id prefix: those digits stay numbers.
    Letters elsewhere (``AO-26005B``, ``DVS-5003-3.78L-A``) keep it an identifier.
    ``label_numbers`` reads a label's digits by it."""
    def replace(match: re.Match[str]) -> str:
        digits_only = _DIGITS_ONLY_ID.fullmatch(match.group(0))
        if digits_only and digits_only.group(1) not in KNOWN_ID_PREFIXES:
            return digits_only.group(1) + " " + digits_only.group(2).replace("-", " ")
        return blank(match)
    return replace


# Prefixes that make a figure of the digits after them, never a label: quantity labels,
# unit words, terms and lead-time words (``QTY-12``, ``KG-25``, ``NET-30``, ``LT-14``,
# ``MOQ-500``), money and period words (``PRICE-500``, ``AMT-500``, ``COST-500``,
# ``DUE-30``, ``ETA-14``, ``MONTHS-3``) and currency codes (``USD-1200``, ``CAD500``:
# amounts). ``R-`` is no money word: a raw material's code is ``R-1696`` (``R500`` glued
# is a figure anyway). ``MO-`` is no period word: the Portal writes a manufacturing order
# ``mo-10718``.
_FIGURE_PREFIXES = frozenset({"moq", "ctn", "ctns", "lt", "lead", "net", "terms", "fy",
                              "day", "days", "wk", "wks", "w", "hr", "hrs", "min", "max",
                              "no", "nr", "num", "tot",
                              "price", "prices", "amt", "cost", "costs", "fee", "fees",
                              "rate", "charge", "due", "eta", "etd", "week", "weeks",
                              "mos", "month", "months", "yr", "yrs", "year", "years"})


def _figure_prefix(prefix: str) -> bool:
    folded = prefix.casefold()
    return (prefix in ISO_4217 or folded in _FIGURE_PREFIXES or folded in _QUANTITY_LABELS
            or folded in _PAGE_UNITS)


def _is_label(match: re.Match[str]) -> bool:
    """Whether a ``LABEL`` match is a label at all: not a currency code glued to digits
    (``USD1200``, ``CAD-500``: an amount, left to the number rules)."""
    prefix = _LABEL_PREFIX.match(match.group(0))
    return not (prefix and prefix.group(0) in ISO_4217)


def labels(text: str) -> list[re.Match[str]]:
    """The labels of ``text`` (``LABEL``, ``_is_label``), in order: the final reply
    screen's tokens (``answer_gate.unbacked``), bare ones (``Q3``) included."""
    return [match for match in LABEL.finditer(text) if _is_label(match)]


def _is_gate_label(match: re.Match[str]) -> bool:
    """Whether a ``LABEL`` match is a label for the gate's answer-text check:
    hyphenated after a prefix that is no quantity, unit, term or
    currency word (``P-02458``, ``SKU-1509``, ``F-26-0001``, ``QRM-26037``; never
    ``QTY-12``, ``KG-25``, ``NET-30``), or a whole token of an identifier kind the
    identifier grammar finds by its shape (``ontology/schema/identifiers.json``:
    ``PO5447``, ``OR1000000001``). A bare letters+digits token (``NET30``, ``KG25``,
    ``Q3``, ``W30``, ``FY2026``) stays a figure."""
    token = match.group(0)
    prefix = _LABEL_PREFIX.match(token)
    if prefix is None or _figure_prefix(prefix.group(0)):
        return False
    if token[len(prefix.group(0)):].startswith("-"):
        return True
    from ontology import identifiers
    return any(mention.start == 0 and mention.end == len(token) and mention.form == "shape"
               for mention in identifiers.find_identifiers(token))


def _label(blank: Callable[[re.Match[str]], str]) -> Callable[[re.Match[str]], str]:
    """A gate label (``_is_gate_label``) is blanked: it names something and states no
    figure."""
    def replace(match: re.Match[str]) -> str:
        return blank(match) if _is_gate_label(match) else match.group(0)
    return replace


def _strip(text: str, blank: Callable[[re.Match[str]], str], with_labels: bool = False
           ) -> str:
    """``text`` without handles, dates, times, identifiers and names (``blank``); with
    ``with_labels`` (the answer text: ``prose_numbers(labels=True)``) without gate labels
    too (``_is_gate_label``). A claim's own text keeps them: a code it states is claimed
    as text (``Line P-02458.``)."""
    for pattern in (HANDLE, SET_HANDLE, _PROSE_MOMENT, ISO_DATE, _DATE_WORDS, _TIME):
        text = pattern.sub(blank, text)
    if with_labels:
        text = LABEL.sub(_label(blank), text)
    text = IDENTIFIER.sub(_identifier(blank), text)
    return _GLUED.sub(_names, _CODE_NAME.sub(_names, text))


def cited_pages(handles: Iterable[str]) -> frozenset[int]:
    """Page numbers of the original page citations among ``handles``."""
    return frozenset(int(match.group(1)) for handle in handles
                     if isinstance(handle, str) and (match := _PAGE_CITATION.match(handle)))


def _page_locators(text: str, pages: frozenset[int],
                   claimed: Iterable[Any] = ()) -> str:
    """``text`` with page locators blanked: in a "page(s) N, M ..." phrase, the leading
    numbers that are pages the item cites. The first number that is not a cited page ends
    the run and it and everything after stay (``pages 3 and 4, 1600 units`` keeps 1600;
    with no page citation nothing is a locator). Singular ``page`` takes one number, and a
    repeated number ends the run. Only ``page``/``pages`` are page words (``p. 3`` is a
    quantity needing a claim). Fail-closed backstop: a number equal to a claimed value of the item (``claimed``) is
    never a locator, so it stays a quantity for the number and the wording checks and its
    claim must fit (``Pages 4 and 3 units were produced`` with 3 claimed damaged).
    Blanking keeps the text's length."""
    if not pages:
        return text
    values = {to_decimal(value) for value in claimed} - {None}

    def blank(match: re.Match[str]) -> str:
        run, start = match.group(2), match.start(2) - match.start(0)
        out = list(match.group(0))
        single = match.group("word").lower() != "pages"
        seen: set[int] = set()
        for number in re.finditer(r"\d+", run):
            value = int(number.group(0))
            # "page" takes one number; a run ends at the first number that is not a cited
            # page or that repeats one ("Page 3, 3 cartons": the second 3 is a quantity).
            if value not in pages or value in seen or (single and seen) or \
                    Decimal(value) in values:
                break
            seen.add(value)
            for index in range(start + number.start(), start + number.end()):
                out[index] = " "
        return "".join(out)
    return _PAGE_PHRASE.sub(blank, text)


def prose_numbers(text: str, covered_texts: Iterable[str] = (),
                  pages: frozenset[int] = frozenset(),
                  claimed: Iterable[Any] = (), labels: bool = False
                  ) -> list[tuple[str, Decimal]]:
    """(as written, value) of every number in ``text`` that is not part of a handle,
    identifier, date, time, known id name, one of ``covered_texts`` (text values a
    claim covers) or a locator of one of ``pages`` (pages the item cites; never a number
    in ``claimed``, the item's claimed values); with ``labels`` (an answer text), nor of
    a gate label (``_is_gate_label``)."""
    text = _page_locators(text, pages, claimed)
    for covered in sorted({item for item in covered_texts if item}, key=len, reverse=True):
        text = text.replace(covered, " ")
    text = _strip(text, lambda match: " ", with_labels=labels)
    return _numbers_in(text) + _number_words(text)


# A whole token of a text: letters and digits, joined inside by ``-``, ``/`` or ``.``
# (``Q9``, ``MIDAS2``, ``PO-00349``, ``F-26-0001``; ``Q9.`` ends at the full stop).
_WHOLE_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)*"
                          r"(?![A-Za-z0-9])")


# A token that is one quantity glued to its unit or term: digits then letters (``12kg``),
# or letters, an optional ``-``, then digits (``NET30``, ``QTY-12``).
_GLUED_QUANTITY = re.compile(r"(?P<n>[0-9]+(?:\.[0-9]+)?)(?P<unit>[A-Za-z]+)"
                             r"|(?P<term>[A-Za-z]+)-?[0-9]+(?:\.[0-9]+)?")


def _glued_quantity(token: str) -> bool:
    """Whether a token is a quantity glued to a unit (``_PAGE_UNITS``: ``12kg``, ``500ML``)
    or after a figure prefix (``_figure_prefix``: ``NET30``, ``QTY-12``, ``PRICE-500``;
    one letter only for a ``_FIGURE_PREFIXES`` word: ``W30``, never ``L9`` or ``R-1696``):
    a figure, never a restated question token."""
    match = _GLUED_QUANTITY.fullmatch(token)
    if match is None:
        return False
    if match.group("unit") is not None:
        return match.group("unit").casefold() in _PAGE_UNITS
    term = match.group("term")
    # a one-letter unit (``L9``, ``G7``) names a thing; ``W30`` is a figure
    return (len(term) > 1 or term.casefold() in _FIGURE_PREFIXES) \
        and _figure_prefix(term.upper())


def asked_tokens_blanked(text: str, question: Any, keep: Iterable[Any] = ()) -> str:
    """``text`` with each whole token (``_WHOLE_TOKEN``) that holds a letter and a digit
    and that the ``question`` holds as a whole token, equal but for case, blanked (same
    length): the question restated (``Q9``, ``MIDAS2``, ``PO-00349``), never a figure the
    answer asserts. A word without a digit is no figure
    anyway and stays, so a label word keeps its number (``PO 4521``). A bare number, a
    number and a unit apart (``12 kg``) and a token the question writes otherwise
    (``Q9-10`` for ``Q9``) stay, and so does a token inside a text of ``keep`` (a text
    claim's value, which covers its exact text whole). A token that is a quantity glued
    to its unit or term (``_glued_quantity``: ``12kg``, ``NET30``, ``QTY-12``) is a figure
    even when the question holds it."""
    if not isinstance(question, str) or not question or not text:
        return text
    kept = [(found.start(), found.end()) for value in keep
            if isinstance(value, str) and value
            for found in re.finditer(re.escape(value), text)]

    def free(match: re.Match) -> bool:
        return not any(start <= match.start() and match.end() <= end for start, end in kept)
    years = _asked_years(question)
    if years:
        # A year the question names is the period it asks about, restated: the answer
        # names it as a year (2025, "2024-2026"), never a figure it asserts (11-13: "24,
        # 25, 26" asked, a reply naming 2025 lost its sentence, so replies said "last
        # year"). A year read as a quantity ("2025 orders") stays a figure.
        text = _YEAR_RANGE.sub(lambda match: " " * len(match.group(0))
                               if free(match) and int(match.group(1)) in years
                               and int(match.group(2)) in years else match.group(0), text)
        text = _ALONE_YEAR.sub(lambda match: " " * len(match.group(0))
                               if free(match) and int(match.group(0)) in years
                               else match.group(0), text)
    asked = {token.casefold() for token in _WHOLE_TOKEN.findall(question)
             if any(char.isalpha() for char in token)
             and any(char.isdigit() for char in token) and not _glued_quantity(token)}
    if not asked:
        return text
    return _WHOLE_TOKEN.sub(lambda match: " " * len(match.group(0))
                            if match.group(0).casefold() in asked and not any(
                                start <= match.start() and match.end() <= end
                                for start, end in kept) else match.group(0), text)


# A four-digit year standing alone in a text: never part of a date, code or number, and
# never read as a quantity (a word right after it: "2025 orders").
_ALONE_YEAR = re.compile(r"(?<![\w.,/:-])20[1-3]\d(?![\w]|[.,/:-]\d|\s+[A-Za-z])")
# Two years written as a range ("2024-2026", "2024 to 2026").
_YEAR_RANGE = re.compile(r"(?<![\w.,/:-])(20[1-3]\d)\s*(?:-|–|to|through)\s*(20[1-3]\d)"
                         r"(?![\w]|[.,/:-]\d)")
# The years a question names: four digits standing alone ("2024-2026" or "between 2024
# and 2026" is every year of the range), or a run of two-digit numbers counting up by one
# ("24, 25, 26", "24-26", "24 to 26") in a question about years.
_QUESTION_YEAR = re.compile(r"(?<![\w.,/-])(20[1-3]\d)(?:\s*(?:-|–|\bto\b|\bthrough\b|"
                            r"\buntil\b|\band\b)\s*(20[1-3]\d))?(?![\w]|[.,/]\d)")
_SHORT_YEAR_RUN = re.compile(r"(?<![\w.,/-])(\d{2})((?:\s*(?:,|-|–|\band\b|&|\bto\b|"
                             r"\bthrough\b)\s*\d{2}(?![\w.,/]\d|\d))+)")
_QUANTITY_BEFORE = re.compile(r"\b(?:over|under|above|below|than|top|first|exceeding|"
                              r"least|most|qty|quantity|of)\s*$", re.I)
_YEAR_CUE = re.compile(r"\b(?:years?|yearly|annual(?:ly)?|trend(?:s|ing)?|yoy|ytd|fy)\b",
                       re.I)


def _asked_years(question: str) -> set[int]:
    """The years ``question`` names (``_QUESTION_YEAR``; ``_SHORT_YEAR_RUN`` of 15..39 in
    a question with a year word, ``_YEAR_CUE``)."""
    out: set[int] = {2000 + int(match.group("y")[-2:]) for pattern in _SHORT_YEARS[:2]
                     for match in pattern.finditer(question)
                     if 10 <= int(match.group("y")[-2:]) <= 39}   # '25, FY25, FY2025
    for match in _QUESTION_YEAR.finditer(question):
        if _QUANTITY_BEFORE.search(question[:match.start()]):
            continue   # "over 2025", "top 2025": a quantity, no year
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first <= last <= first + 20:
            out.update(range(first, last + 1))
        else:
            out.update({first, last})
    if not _YEAR_CUE.search(question):
        return out   # two-digit numbers are years only in a question about years
    for match in _SHORT_YEAR_RUN.finditer(question):
        run = [int(match.group(1))] + [int(n) for n in re.findall(r"\d{2}", match.group(2))]
        if not all(15 <= year <= 39 for year in run):
            continue
        dashed = len(run) == 2 and re.search(r"-|–|to|through", match.group(2)) is not None
        if dashed and run[0] < run[1] <= run[0] + 20:
            out.update(2000 + year for year in range(run[0], run[1] + 1))
        elif len(run) >= 2 and all(b == a + 1 for a, b in zip(run, run[1:])):
            out.update(2000 + year for year in run)
    return out


def label_numbers(label: str) -> list[Decimal]:
    """The numbers a label (``LABEL``) holds as digits read apart from its letters
    (``SKU-1509``: 1509; ``Q3``: 3), none for a name (``F2``, ``AO-77``, ``UTF-8``:
    ``KNOWN_ID_PREFIXES``; ``AO-26005B``: an identifier): what a reply must back for an
    unasked label it writes (``answer_gate.unbacked``)."""
    return [number for _written, number in _numbers_in(_strip(label, lambda m: " "))]


def _numbers_in(text: str) -> list[tuple[str, Decimal]]:
    """(as written, value) of the digit numbers of ``text`` (``1,509`` is one number; a
    scale suffix is part of it: ``0.25%`` is 0.0025)."""
    return [(match.group(0), number_value(match)) for match in NUMBER.finditer(text)]


def _percent_written(written: str) -> bool:
    """Whether a prose number is written as a percent ("25%", "25 pct", "twenty-five
    percent")."""
    parsed = NUMBER.fullmatch(written)
    if parsed:
        return (parsed.group("scale") or "").strip(" \u00a0").lower() in _PERCENT_WORDS
    return re.search(r"(?:%|per ?cent|pct)$", written, re.I) is not None


def written_dates(text: str
                  ) -> list[tuple[str, tuple[str, datetime | None] | None, bool]]:
    """(as written, ``_moment`` or None for an ISO-shaped date that names no real day,
    whether ISO) of every date ``text`` writes outside a handle: ISO dates and timestamps
    on token boundaries, month-name dates with a year."""
    for handle in (HANDLE, SET_HANDLE):
        text = handle.sub(lambda match: " " * len(match.group(0)), text)
    out: list[tuple[str, tuple[str, datetime | None] | None, bool]] = [
        (match.group(0), _moment_of(match), True) for match in _PROSE_MOMENT.finditer(text)]
    for match in _MONTH_DATE.finditer(text):
        month = (match["mon"] or match["mon2"])[:3].lower()
        try:
            day = date(int(match["y"] or match["y2"]), _MONTHS.index(month) + 1,
                       int(match["d"] or match["d2"]))
        except ValueError:
            continue
        out.append((match.group(0), (day.isoformat(), None), False))
    return out


# A day printed in words or digits in a text: "31 Aug", "31 August 2026", "Aug 31, 2026",
# "September 2", "31/08/2026" (``_printed_days``).
_DAY_MONTH = re.compile(r"(?<![\w/.-])(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>"
                        + _MONTH + r")\b(?:\.?,?\s+(?P<y>\d{4})\b)?", re.I)
_MONTH_DAY = re.compile(r"(?<![\w-])(?P<mon>" + _MONTH + r")\.?\s+(?P<d>\d{1,2})"
                        r"(?:st|nd|rd|th)?\b(?![.,:/-]\d)(?:,?\s+(?P<y>\d{4})\b)?", re.I)
_DIGIT_DAY = re.compile(r"(?<![\w/.-])(?P<a>\d{1,2})(?P<s>[/.])(?P<b>\d{1,2})(?P=s)(?P<y>\d{4})"
                        r"(?![\w/]|[.-]\w)")
# A year in any form a text prints (``_years``): 19xx/20xx alone or opening an ISO date,
# 'YY, FY2025 or FY25, two digits right after a day and month ("31 Aug 25", "Aug 31, 25")
# or ending a digit date ("31/08/25").
_YEAR = re.compile(r"(?<![\w.,/-])(?:19|20)\d{2}(?![\w]|[.,/]\d)|(?<![\w.])(?:19|20)\d{2}"
                   r"(?=-\d{2}-\d{2})")
# Two digits that open the next day of a list ("25 Aug" of "18 Aug, 25 Aug"): no year.
_NEXT_DAY = r"(?!(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\b)"
_SHORT_YEARS = (
    re.compile(r"(?<![\w'’])['’](?P<y>\d{2})(?!\w)"),
    re.compile(r"\bFY ?(?P<y>\d{4}|\d{2})(?!\d)", re.I),
    re.compile(r"(?<![\w/.-])\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH
               + r"\b\.?,?\s+(?P<y>\d{2})(?![\d:])" + _NEXT_DAY, re.I),
    re.compile(r"(?<![\w-])" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?\b,?\s+(?P<y>\d{2})"
               r"(?![\d:])" + _NEXT_DAY, re.I),
    re.compile(r"(?<![\w/.-])\d{1,2}[/.]\d{1,2}[/.](?P<y>\d{2}|\d{4})(?![\w/]|[.-]\w)"))
# A year right after a printed day and month that names none in four digits: "31 Aug 25",
# "31 Aug '25", "Aug 31, 25" (never a clock time, "Aug 31 10:00", nor the next day of a
# list, "18 Aug, 25 Aug").
_YEAR_AFTER = re.compile(r"\.?,?\s*(?:['’]\d{2}(?!\w)|\d{2}(?![\d:])" + _NEXT_DAY
                         + r"|\d{4}(?!\d))", re.I)


def _years(text: str) -> set[int]:
    """Every year a text prints, in any form (``_YEAR``, ``_SHORT_YEARS``: a two-digit year
    is 20YY)."""
    found = {int(match.group(0)) for match in _YEAR.finditer(text)}
    for pattern in _SHORT_YEARS:
        for match in pattern.finditer(text):
            year = int(match.group("y"))
            found.add(year if year >= 100 else 2000 + year)
    return found


def _printed_days(value: str, text: str) -> tuple[list[tuple[int, int]], bool]:
    """(the spans of normalised ``text`` that print the ISO day ``value`` (``2026-08-31``)
    in another written form, unnegated; whether a form of that day was refused only for
    its year). Forms: ``31 Aug`` or ``Aug 31`` (``August``, ``31st``, a trailing ``.``; the
    span is the day and month), with ``2026`` stating that year; and ``31/08/2026`` or
    ``08/31/2026`` only where day and month cannot be swapped (one above 12, or both
    equal). A form without its year holds only when no year in any form follows it
    (``_YEAR_AFTER``: ``31 Aug 25``) and the whole cited ``text`` prints exactly one year in
    any form (``_years``), the claimed one; never a year the agent's quote adds. Any other
    value: none."""
    match = _ISO_DAY.fullmatch(value.strip())
    if match is None:
        return [], False
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return [], False
    implied = _years(text) == {day.year}
    found, by_year = [], False
    for pattern in (_DAY_MONTH, _MONTH_DAY):
        for written in pattern.finditer(text):
            month = _MONTHS.index(written.group("mon")[:3].lower()) + 1
            if (month, int(written.group("d"))) != (day.month, day.day):
                continue
            year = written.group("y")
            if year:
                if int(year) == day.year:
                    found.append(written.span())
                else:
                    by_year = True
                continue
            stop = max(written.end("mon"), written.end("d"))
            if implied and not _YEAR_AFTER.match(text, stop):
                found.append((written.start(), stop))
            else:
                by_year = True
    for written in _DIGIT_DAY.finditer(text):
        first, second = int(written.group("a")), int(written.group("b"))
        if int(written.group("y")) != day.year:
            continue
        if (first, second) == (day.day, day.month) and (first > 12 or first == second) \
                or (first, second) == (day.month, day.day) and (second > 12 or first == second):
            found.append(written.span())
    return [(start, end) for start, end in found
            if not _negated(text[start:end], text, start)], by_year


def _printed_date_problem(claim: Mapping[str, Any], text: str, where: str) -> str | None:
    """Why an ISO date value claimed on a cited text (an original page, a record's free
    text) is not a day the text prints in another form (``_printed_days``), or None when it
    is: inside an occurrence of the claim's quote where it gives one."""
    want = _normalised_text(text)
    spans, by_year = _printed_days(claim["value"], want)
    if "quote" in claim and spans:
        quote = _normalised(claim["quote"])
        held = [(at, at + len(quote)) for at in _spans(quote, want)]
        spans = [span for span in spans
                 if any(start <= span[0] and span[1] <= end for start, end in held)]
        if not spans:
            return (f"the quote holds no printed form of the day {claim['value']} (such as "
                    f"31 Aug or Aug 31, 2026) that {where} prints: quote the passage that "
                    "holds the date as printed")
    if spans:
        return None
    if by_year:
        return (f"{where} prints no unambiguous year for the day {claim['value']} as printed "
                "there (no year beside it, or another year, and the text names more or "
                "other years than this one): cite a figure that prints its year")
    return (f"{where} prints no day {claim['value']} (as written, or as a printed date with "
            "its year or in a text that prints only that year, unnegated; a digit date only "
            "where day and month cannot be swapped)")


def _date_reading(claim: Mapping[str, Any], handles: Sequence[str], evidence: _Evidence
                  ) -> _Dated | None:
    """A date claim as the prose checks read it (``_Dated``); None for any other claim.
    A declared zone that does not load is no zone here: the claim itself fails."""
    moment = _moment(claim["value"]) if claim["kind"] == "text" else None
    if moment is None:
        return None
    cell = zone = None
    handle, field = claim.get("citation"), claim.get("field")
    resolved = evidence.row(handle) if handle in handles else None
    if resolved is not None and field != PAGE_TEXT:
        exists, value = dig(resolved, field)
        if exists and isinstance(value, str):
            value = _as_stored(value, stamp_zone(handle, resolved, field))
        held = _moment(value) if exists else None
        cell = held[1] if held else None
        try:
            zone = evidence.zone(handle, resolved, field)
        except ValueError:
            zone = None
    return moment, cell, zone


def _states(text: str, claim: Mapping[str, Any], form: str,
            reading: _Dated | None = None) -> bool:
    """Whether ``text`` states a claim's value in its form (``claim_value_unstated``): a
    name or status word in an occurrence no negator reverses (``Status: not REVISED``
    states no ``REVISED``), a date (``reading``) on a date written it covers
    (``_covers``)."""
    if form == "date":
        return reading is not None and any(
            moment is not None and _covers(reading, moment)
            for _written, moment, _iso in written_dates(text))
    want, text = ((_fold(claim["value"]), _fold(text)) if form == "enum" else
                  (_normalised(claim["value"]), _normalised_text(text)))
    return any(not _negated(want, text, at) for at in _spans(want, text))


def _uncovered(texts: Iterable[str], claims: Sequence[Mapping[str, Any]],
               dated: Sequence[_Dated], pages: frozenset[int] = frozenset(),
               labels: bool = False) -> list[tuple[str, str, str]]:
    """(as written, detail, ``number`` or ``date``) of each number of ``texts`` no claim
    covers by value (with ``labels``, the answer text's: gate labels are no numbers,
    ``prose_numbers``), and of each ISO date or timestamp (the whole token, its time and
    offset too) no date claim (``dated``) covers (``_covers``)."""
    numbers = [claim for claim in claims if claim["kind"] == "number"]
    percents = {to_decimal(claim["value"]) for claim in numbers
                if str(claim.get("unit", "")).strip().lower() in _PERCENT_UNITS}
    values = {to_decimal(claim["value"]) for claim in numbers
              if str(claim.get("unit", "")).strip().lower() not in _PERCENT_UNITS}
    # A date claim covers dates by ``_covers``, never as text: its value removed from a
    # timestamp would leave the timestamp's time unchecked.
    covered = [claim["value"] for claim in claims
               if claim["kind"] == "text" and _moment(claim["value"]) is None]
    out: list[tuple[str, str, str]] = []
    for text in texts:
        # A text claim's value covers the dates it holds (a quote "ETA 2026-01-09").
        rest = text or ""
        for value in sorted({value for value in covered if value}, key=len, reverse=True):
            rest = rest.replace(value, " ")
        for written, moment, iso in written_dates(rest):
            # An ISO-shaped date that names no real day (2026-02-30) is never covered.
            if iso and written not in (seen for seen, _, _ in out) and (moment is None or not any(
                    _covers(reading, moment) for reading in dated)):
                timed = moment is not None and moment[1] is not None and any(
                    reading[0][1] is None for reading in dated)
                out.append((written, f"{written} states a time of day; a date claim covers "
                                     "only the instant of the cell it was read from"
                            if timed else "", "date"))
        for written, number in prose_numbers(text or "", covered, pages, values | percents,
                                             labels=labels):
            if written in (seen for seen, _, _ in out):
                continue
            # The percent contract: a number written as a percent is covered ONLY by a
            # claim whose unit is a percent, compared at percent scale ("25%" by 25
            # percent, never "2500%" or "0.25%"); it never covers a unit-less, currency or
            # physical-unit claim, and a percent claim covers no other number ("25 USD").
            # A signed number is covered by its magnitude ("-6" states the difference 6).
            if _percent_written(written):
                stated = abs(number) * 100
                if stated in percents:
                    continue
                out.append((written, f"{written} is a percent; no claim of {_plain(stated)} "
                                     "with a percent unit covers it" if stated in values
                            or abs(number) in values else "", "number"))
                continue
            if number in values or abs(number) in values or rounded_from(number, values):
                continue
            parsed = NUMBER.fullmatch(written)  # None for a number in words
            suffix = (parsed.group("scale") or "") if parsed else ""
            unscaled = abs(number) / _SCALES_OF_SUFFIX[suffix.lower()] if suffix else None
            if unscaled is not None and unscaled in values:
                out.append((written, f"{written} is {_plain(number)} at its written scale "
                                     f"({suffix}); the claim of {_plain(unscaled)} "
                                     "states another scale", "number"))
            else:
                out.append((written, "", "number"))
    return out
