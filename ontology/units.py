"""Unit spelling and money tax basis: the one fold every Workbench reader uses.

Units (``unit_token``) mirror the Portal's ``normalizeProductUom`` exactly
(plx-customer-portal ``portal/src/lib/mrp/product-uom.ts@fa46f2d``: ``PRODUCT_UOM_OPTIONS``
values lines 30-49, ``ALIASES`` lines 70-98, ``normalizeProductUom`` lines 105-110):
trim and lower-case; a canonical option value stays itself; a listed alias maps to its
option; any other token is returned trimmed and lower-cased, never rewritten to ``ea``.
The trim is JavaScript's ``String.prototype.trim`` (``JS_WHITESPACE``), not Python's
``str.strip``: a BOM is trimmed, U+001C-U+001F and U+0085 are not. Receiving adds one
more Portal fold: ``unitsCompatible`` (``portal/src/lib/mrp/apply-po-line-receive.ts@6e76a5b``
lines 41-52) treats ``bags``/``bag`` and ``drums``/``drum`` as one unit, so those two
plurals fold too (``RECEIVE_ALIASES``; its third set, ``unit``/``units``/``ea``/``each``/
``pcs``/``pc``, is already one option above). Two spellings are one unit when either
Portal fold says so (a ``drm`` and a ``drums`` line both fold to ``drum``); any other
plural (``pails``, ``rolls``, ``bottles``) stays itself, a different unit.
It is a spelling fold, never a conversion (``kg`` and ``g`` stay two units). The Portal's
BC item sync codes (``bcCode``) are an outbound mapping the Portal never folds back, so a
BC code folds only where the Portal's own table happens to spell it: ``PCS`` is the alias
``pcs`` (``ea``) and ``CSE`` the option ``cse``, while ``GR`` stays ``gr``, not ``g``.
The projection's conflict key (``ontology/project.py``), ``plx.process``, ``plx.aggregate``
and ``plx.compare`` use this fold; the console's ``operator_ui/workspace.js`` carries the
same two tables (``UOM_CANONICAL``/``UOM_ALIASES``), and ``ontology/tests/test_units.py``
fails when they differ.

Money tax basis (``money_basis``): every money source of the curated schema declares
``tax_basis`` (``incl_tax``, ``excl_tax``, ``tax_only`` for a tax amount itself, or
``unknown`` when no upstream code grounds it), optionally refined per record lineage with
``tax_basis_by_lineage`` (one Portal column written with and without tax by different
writers). Amounts on two bases are never summed or compared; an ``unknown`` basis equals
only itself from the same source column, and a sum over it carries a visible
``tax_basis_unknown`` gap (Portal-native order totals add tax only when one is entered,
service.ts:346@fa46f2d, and the FM bridge may re-total them without it, so that
lineage is not declared incl_tax). A source whose records state their own basis
declares ``tax_basis_from`` instead: a cell (on the record or one hop away, a line reading
its header's) that says whether the record's prices include tax; ``true`` is ``incl_tax``,
``false`` is ``excl_tax`` and anything else (null, absent, not a boolean) is ``unknown``. The
projection stores that per-record basis with the value (``object_values.tax_basis``), and
``value_basis`` reads it there before the declaration.

Price denominators (``is_unit_price``, ``same_denominator``): a money property declares
``money_measure``: ``amount`` (additive; the default) or ``unit_price`` (a rate: money per
one unit of a denominator). Every source of a unit price declares the unit its price is
per (``per_unit``, ``per_unit_from`` a UoM cell, or ``per_unit_unknown``, optionally per
lineage), and the projection stores that unit as the money value's ``unit``. A unit price
is never summed; two unit prices compare (or meet in a min/max) only when both
denominators are known and equal after the spelling fold above (``EA`` and ``ea`` agree,
``ea`` and ``kg`` do not), never converted.

Currency codes (``currency_code``): a currency cell is trimmed (``js_trim``) and
upper-cased; three letters are a code, as the projection read them before any rule
(``cad``, ``CDN``: no rule counted). A cell that is not is read only in two written forms
whose currency is not in doubt, never guessed: an ISO 4217 code
(``ISO_4217``) with one currency sign attached at either end, where the sign is that
currency's (``CAD$``, ``$USD``, ``EUR€``: ``SIGN_CURRENCIES``), and the sign-qualified
dollar abbreviations ``CURRENCY_SIGN_FORMS`` (``US$``, ``C$``, ``CA$``, ``CAN$``, ``CDN$``,
the sign also leading: ``$US``, ``$CAN``, ``$CDN``; ``US$``/``C$``/``CA$`` are
the written forms ``ontology/schema/document_fields.json`` already reads on documents).
A bare sign (``$`` is several currencies), a sign that is not the code's (``CAD€``), a
non-ISO core (``XYZ$``) or any other text stays unknown: ``currency_invalid`` in the
projection, with its masked shape counted (``currency_shape``) so a later rule is chosen
on the measured shapes, never on a guess.

This is the one currency table: documents read it too (``currency_reading``,
``currency_token_pattern``; ``ontology/document_fields.py`` and
``integrations/document_understanding.py``), with two differences only a document has:
three letters are a currency only when ISO 4217 lists them (``RMB`` is unknown, never
mapped), and a bare sign is its one currency (``€``, ``£``) or a stated ambiguity
(``$``, ``¥``: ``SIGN_CURRENCIES``) that a declared currency the sign writes settles.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# PRODUCT_UOM_OPTIONS[].value, in the Portal's order.
UOM_CANONICAL = ("ea", "cse", "dz", "box", "bag", "bottle", "drum", "pail", "roll", "pack",
                 "kg", "g", "mg", "lb", "oz", "l", "ml", "gal")

# ALIASES, verbatim.
UOM_ALIASES = {
    "each": "ea", "eac": "ea", "unit": "ea", "units": "ea", "pc": "ea", "pcs": "ea",
    "ech": "ea", "case": "cse", "cases": "cse", "cs": "cse", "dozen": "dz", "doz": "dz",
    "boxes": "box", "packs": "pack", "btl": "bottle", "drm": "drum", "kgs": "kg",
    "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g", "lbs": "lb",
    "pound": "lb", "pounds": "lb", "litre": "l", "liter": "l", "lt": "l"}

# unitsCompatible's plural sets not already folded by ALIASES (apply-po-line-receive.ts@6e76a5b:46-48).
RECEIVE_ALIASES = {"bags": "bag", "drums": "drum"}

# String.prototype.trim's WhiteSpace and LineTerminator code points (ECMA-262).
JS_WHITESPACE = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
                 "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")

# ISO 4217 alphabetic codes (active plus common funds/metal codes). A unit is a currency
# only if it is in this set; three capital letters alone (BOX, PCS, CTN, PLT, GAL, SET)
# are quantity units. operating/landed_cost.py reads it from here.
ISO_4217 = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN
BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD
FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY
KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP
MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR
RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT
TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW
ZWG XAU XAG XPT XPD
""".split())

# The currencies each sign writes (a sign attached to any other code is a contradiction,
# never folded).
SIGN_CURRENCIES = {
    "$": frozenset({"USD", "CAD", "AUD", "NZD", "HKD", "SGD", "MXN", "TWD"}),
    "\u20ac": frozenset({"EUR"}), "\u00a3": frozenset({"GBP"}),
    "\u00a5": frozenset({"JPY", "CNY"})}

# Sign-qualified dollar abbreviations (upper-cased): one currency each. Assumption, not
# measured: C$ is the Canadian dollar (its common use; Nicaragua's cordoba is also
# written C$, a currency no PLX source is known to trade in).
CURRENCY_SIGN_FORMS = {"US$": "USD", "C$": "CAD", "CA$": "CAD", "CAN$": "CAD",
                       "CDN$": "CAD", "$US": "USD", "$CAN": "CAD", "$CDN": "CAD"}

# ISO 4217 codes that are also English words ("all-in", "top", "try") or shipping
# abbreviations ("KGS" kilograms, "SHP" shipment): in document text such a code names a
# currency only with its own sign attached or through a currency declaration, never as
# a bare word next to an amount.
WORD_CODES = frozenset({"ALL", "BAM", "BOB", "CUP", "GEL", "KGS", "MAD", "MOP", "PEN",
                        "SHP", "SOS", "TOP", "TRY"})

_CODE = re.compile(r"[A-Z]{3}")
_SIGNED = re.compile(r"([$\u20ac\u00a3\u00a5])?([A-Z]{3})([$\u20ac\u00a3\u00a5])?")
_SIGNS = "".join(SIGN_CURRENCIES)

_CANONICAL = frozenset(UOM_CANONICAL)

TAX_BASES = ("incl_tax", "excl_tax", "tax_only", "unknown")

MONEY_MEASURES = ("amount", "unit_price")


def js_trim(text: str) -> str:
    """``text.trim()`` as JavaScript trims it."""
    return text.strip(JS_WHITESPACE)


def unit_token(raw: Any) -> str | None:
    """``normalizeProductUom(raw)``, then the receiving plural fold: the folded unit,
    None when empty or not text."""
    if not isinstance(raw, str):
        return None
    token = js_trim(raw).lower()
    if not token:
        return None
    if token in _CANONICAL:
        return token
    token = UOM_ALIASES.get(token, token)
    return RECEIVE_ALIASES.get(token, token)


def _written_code(text: str) -> tuple[str | None, str | None]:
    """``(code, rule)`` of upper-cased text that names one ISO 4217 currency: the code
    itself (rule None), a ``CURRENCY_SIGN_FORMS`` spelling or the code with its own sign
    at one end; ``(None, None)`` otherwise."""
    if _CODE.fullmatch(text):
        return (text, None) if text in ISO_4217 else (None, None)
    if text in CURRENCY_SIGN_FORMS:
        return CURRENCY_SIGN_FORMS[text], "sign_form"
    m = _SIGNED.fullmatch(text)
    if m and (m[1] is None) != (m[3] is None) and \
            m[2] in SIGN_CURRENCIES[m[1] or m[3]] and m[2] in ISO_4217:
        return m[2], "sign_attached"
    return None, None


def currency_reading(raw: Any) -> tuple[str | None, frozenset[str]]:
    """``(code, candidates)`` of a currency written on a document, from the one table
    above: an ISO 4217 code in any case, a ``CURRENCY_SIGN_FORMS`` spelling or a code
    with its own sign is that code (candidates ``{code}``); a bare sign is its one
    currency, or ``(None, SIGN_CURRENCIES[sign])`` when it writes several (the ambiguity a
    declaration may settle); anything else, a non-ISO code included, is
    ``(None, frozenset())``: unknown, never guessed."""
    if not isinstance(raw, str):
        return None, frozenset()
    text = js_trim(raw).upper()
    if text in SIGN_CURRENCIES:
        codes = SIGN_CURRENCIES[text]
        return (next(iter(codes)) if len(codes) == 1 else None), codes
    code, _rule = _written_code(text)
    return code, (frozenset({code}) if code else frozenset())


def currency_token_pattern(codes: str = "[A-Za-z]{3}", bare_sign: bool = True) -> str:
    """A regular expression (no groups) for one written currency token: a
    ``CURRENCY_SIGN_FORMS`` spelling in any case, or ``codes`` with at most one sign at
    either end (a ``WORD_CODES`` code only with a sign attached), or (``bare_sign``) a
    sign alone. ``currency_reading`` decides what the matched text is."""
    forms = "|".join(re.escape(form) for form in
                     sorted(CURRENCY_SIGN_FORMS, key=len, reverse=True))
    words = "|".join(sorted(WORD_CODES))
    code = f"(?:{codes})(?![A-Za-z])"
    return (f"(?i:{forms})(?![A-Za-z])|[{_SIGNS}]{code}[{_SIGNS}]?|{code}[{_SIGNS}]"
            f"|(?!(?i:{words})(?![A-Za-z])){code}" + (f"|[{_SIGNS}]" if bare_sign else ""))


def currency_code(raw: Any) -> tuple[str | None, str | None]:
    """``(code, rule)`` of a currency cell: three letters in any case are the code,
    upper-cased (rule None: the projection read them so before any rule);
    ``sign_attached`` folds an ISO code with its own sign at one end and ``sign_form`` a
    ``CURRENCY_SIGN_FORMS`` spelling: each fires only on a cell that was
    ``currency_invalid`` before. ``(None, None)`` for anything else (empty, not text, a
    bare or foreign sign, a non-ISO core): unknown, never guessed."""
    if not isinstance(raw, str):
        return None, None
    text = js_trim(raw).upper()
    if _CODE.fullmatch(text):
        return text, None
    return _written_code(text)


def currency_shape(raw: Any) -> str:
    """A cell's shape with no value in it: letters ``A``, digits ``9``, a currency sign
    or ``.-/_`` kept, any other character ``?``; at most 12 characters (``+`` marks a
    longer cell), ``empty`` for none. For counting the cells no rule decides."""
    text = js_trim(raw).upper() if isinstance(raw, str) else ""
    if not text:
        return "empty"
    out = "".join("A" if "A" <= c <= "Z" else "9" if "0" <= c <= "9" else
                  c if c in "$\u20ac\u00a3\u00a5.-/_" else "?" for c in text[:12])
    return out + ("+" if len(text) > 12 else "")


def money_basis(prop: Mapping[str, Any] | None, binding_id: str,
                lineage: str | None) -> str:
    """The declared tax basis of a money value read through ``binding_id`` for a record
    of ``lineage``; ``unknown`` when the property declares none for that binding or its
    sources through that binding disagree."""
    found = set()
    for source in (prop or {}).get("sources", ()):
        if source.get("binding") != binding_id:
            continue
        by_lineage = source.get("tax_basis_by_lineage") or {}
        found.add(by_lineage.get(lineage, source.get("tax_basis", "unknown")))
    return found.pop() if len(found) == 1 else "unknown"


def value_basis(prop: Mapping[str, Any] | None, row: Mapping[str, Any]) -> str:
    """The tax basis of one stored money value: the basis the projection stored with it
    (read per record through ``tax_basis_from``), else ``money_basis`` of its binding and
    lineage (a row written without one)."""
    stored = row["tax_basis"] if "tax_basis" in row.keys() else None
    if stored is not None:
        return stored
    return money_basis(prop, row["binding_id"], row["lineage"])


def is_unit_price(prop: Mapping[str, Any] | None) -> bool:
    """True when the property is a money rate (``money_measure: unit_price``)."""
    return (prop or {}).get("money_measure") == "unit_price"


def same_denominator(units: Iterable[Any]) -> bool:
    """True when every unit price is per one known unit after the spelling fold."""
    folded = {unit_token(u) for u in units}
    return len(folded) == 1 and None not in folded


__all__ = ["CURRENCY_SIGN_FORMS", "ISO_4217", "JS_WHITESPACE", "MONEY_MEASURES",
           "RECEIVE_ALIASES", "SIGN_CURRENCIES", "TAX_BASES", "UOM_ALIASES", "UOM_CANONICAL",
           "WORD_CODES",
           "currency_code", "currency_reading", "currency_shape",
           "currency_token_pattern", "is_unit_price", "js_trim", "money_basis",
           "same_denominator", "unit_token", "value_basis"]
