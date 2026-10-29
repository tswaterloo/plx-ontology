"""Text normalisation before embedding: one spelling for units, currencies, percents
and dates, so "500kgs", "500 kilograms" and "500 kg" read alike to a vector.

Never a conversion and never a rewrite of a value: ``kg`` and ``g`` stay two units (the
fold of ``ontology.units``, the one unit table, plus the page spellings
``operating.claim_checks`` reads), a sign-qualified dollar is spelled with its ISO code
(``C$1,200`` -> ``CAD 1,200``; a bare ``$`` names no single currency and stays), a
percent word becomes ``%`` (``12 pct`` -> ``12%``; a fraction is never turned into a
percent: which one the source meant is its own), and a date is written ISO *beside* the
original only when it is unambiguous (``25/10/2026 (2026-10-25)``; ``10/02/2026`` could be
either and stays as written). Whitespace and HTML entities are tidied, and a link longer
than 40 characters is kept as its site only (``[link example.com]``).
"""

from __future__ import annotations

import html
import re
from datetime import date

from ontology.units import CURRENCY_SIGN_FORMS, ISO_4217, RECEIVE_ALIASES, UOM_ALIASES, UOM_CANONICAL

# Unit spellings after a figure -> the canonical unit (``ontology.units`` plus the page
# spellings ``operating.claim_checks._PAGE_UNITS`` lists). Multi-letter only: a one-letter
# unit (g, l, t) is left as written (too many false readings: "7 l." "4 g2").
_UNIT_WORDS = {**{u: u for u in UOM_CANONICAL if len(u) > 1}, **UOM_ALIASES, **RECEIVE_ALIASES,
               **dict.fromkeys(("kilo", "kilos", "kilogramme", "kilogrammes", "kgm"), "kg"),
               **dict.fromkeys(("gm", "gms"), "g"),
               **dict.fromkeys(("litres", "liters", "ltr", "ltrs", "lts"), "l"),
               **dict.fromkeys(("millilitre", "millilitres", "milliliter", "milliliters",
                                "mls"), "ml"),
               **dict.fromkeys(("tonne", "tonnes", "mt"), "t")}
# Only weight and volume spellings are folded (a count word such as "units" or "cases"
# reads naturally already and folding it to "ea"/"cse" would hurt more than help).
_FOLD = {k: v for k, v in _UNIT_WORDS.items() if v in {"kg", "g", "mg", "lb", "oz", "l", "ml",
                                                       "gal", "t"}}
_UNIT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)[ \t ]?(" +
                   "|".join(sorted(map(re.escape, _FOLD), key=len, reverse=True)) +
                   r")(?![\w])", re.IGNORECASE)
_SIGNED = re.compile("(" + "|".join(sorted(map(re.escape, CURRENCY_SIGN_FORMS), key=len,
                                           reverse=True)) + r")[ \t ]?(?=\d)",
                     re.IGNORECASE)
_PERCENT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)[ \t ]?(?:per[ \t ]?cent|pct)\b",
                      re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_NUMERIC_DATE = re.compile(r"(?<![\w/.-])(\d{1,2})([/.-])(\d{1,2})\2(\d{4})(?![\w/.-])")
_NAMED_DATE = re.compile(
    r"(?<![\w])(?:(\d{1,2})(?:st|nd|rd|th)?[ \t]+([A-Za-z]{3,9})\.?,?[ \t]+(\d{4})|"
    r"([A-Za-z]{3,9})\.?[ \t]+(\d{1,2})(?:st|nd|rd|th)?,?[ \t]+(\d{4}))(?![\w])")
# A long link is noise to a vector and a keyword index: kept as its site only.
_URL = re.compile(r"<?\b(?:https?://|www\.)([A-Za-z0-9.-]+)[^\s<>\]\)\"']*>?")
_SPACES = re.compile(r"[ \t ​]+")
_BLANKS = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _numeric_date(match: re.Match[str]) -> str:
    a, b, year = int(match.group(1)), int(match.group(3)), int(match.group(4))
    # Unambiguous only when one part cannot be a month: 25/10 is day-first, 10/25 is
    # month-first; 10/02 could be either and stays as written.
    if a > 12 and b <= 12:
        iso = _iso(year, b, a)
    elif b > 12 and a <= 12:
        iso = _iso(year, a, b)
    elif a == b:
        iso = _iso(year, a, b)
    else:
        iso = None
    return match.group(0) + (f" ({iso})" if iso else "")


def _named_date(match: re.Match[str]) -> str:
    if match.group(1):
        day, month, year = match.group(1), match.group(2), match.group(3)
    else:
        month, day, year = match.group(4), match.group(5), match.group(6)
    number = _MONTHS.get(month[:3].lower()) if month.lower()[:3] in _MONTHS and (
        len(month) <= 4 or month.lower().startswith(
            ("janu", "febr", "marc", "apri", "may", "june", "july", "augu", "sept", "octo",
             "nove", "dece"))) else None
    iso = _iso(int(year), number, int(day)) if number else None
    return match.group(0) + (f" ({iso})" if iso else "")


def _currency(match: re.Match[str]) -> str:
    code = CURRENCY_SIGN_FORMS[match.group(1).upper()]
    return f"{code} " if code in ISO_4217 else match.group(0)


def normalise(text: str) -> str:
    """The text as it is embedded and keyword-indexed (module docstring)."""
    if not text:
        return ""
    out = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    out = _URL.sub(lambda m: m.group(0) if len(m.group(0)) <= 40 else f"[link {m.group(1)}]",
                   out)
    out = _SIGNED.sub(_currency, out)
    out = _UNIT.sub(lambda m: f"{m.group(1)} {_FOLD[m.group(2).lower()]}", out)
    out = _PERCENT.sub(lambda m: f"{m.group(1)}%", out)
    out = _NUMERIC_DATE.sub(_numeric_date, out)
    out = _NAMED_DATE.sub(_named_date, out)
    out = _SPACES.sub(" ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    return _BLANKS.sub("\n\n", out).strip()


__all__ = ["normalise"]
