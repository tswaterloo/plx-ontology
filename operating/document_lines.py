"""Document lines: the item lines of an order-type document (a customer's PO, a quote, an
order list in a mail body) read from its text, never coded per sender or layout.

``read_lines(text, form)`` returns candidate lines, each ``{"tokens", "quantity", "unit",
"unit_price", "extended", "cite", "basis"}``: ``tokens`` are the code-like words of the
line (the caller resolves them to items through the store's identifier table and keeps a
line only when exactly one item answers), amounts are ``Decimal`` text, ``cite`` is the
cell or text line (``Sheet!B7``, ``L12``) the line was read from. Three readers, in order:

* ``table``: a spreadsheet, CSV or PDF text table found by its header row
  (``integrations.sheet_readings``' grid and column roles, plus unit price and extended
  price columns): one line per body row with a quantity;
* ``priced``: a text line ending in ``quantity [unit] unit-price extended`` whose product
  checks (quantity x unit price = extended, to the cent or 0.5%): the PDF text layer of a
  PO, where columns run together. A line whose numbers sit on their own line takes its
  code words from the line above;
* ``listed``: a line naming a code and a counted quantity (``6 cases``, ``qty 12``,
  ``x 4``), the shape of an order typed into a mail body; read only when no priced or
  table line was found (a priced document's description words are no order).
  Its ``unit_price`` is set only by a "$" or currency-coded amount that ends the line
  (``@ $4.50``, ``$4.50/cs``, ``@ CAD 4.50``), with ``price_per`` (the unit it names)
  and ``price_currency`` (the code it names); any other money on the line sets
  ``price_unknown``. Consumers take a line's price through ``stated_unit_price``.

Nothing here decides which item, customer or price a line means.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

_NUM = r"\(?-?\$?\s*[0-9][0-9,]*(?:\.[0-9]+)?\)?"
_UNIT_WORDS = (r"ea|each|eaches|cs|case|cases|ctn|carton|cartons|bx|box|boxes|pk|pack|packs|"
               r"pc|pcs|piece|pieces|unit|units|btl|bottle|bottles|gal|gallon|gallons|dz|"
               r"dozen|bag|bags|pail|pails|drum|drums|tote|totes|plt|pallet|pallets|kg|lb|lbs|"
               r"l|litre|liter|litres|liters|roll|rolls|set|sets")
_PRICED = re.compile(rf"(?:^|\s)(?P<qty>{_NUM})\)?,?\s*(?:(?P<unit>[A-Za-z]{{1,8}})\s+)?"
                     rf"\$?(?P<price>{_NUM}),?\s+\$?(?P<ext>{_NUM})\s*$")
_UNIT_FIRST = re.compile(rf"(?:^|\s)(?P<unit>[A-Za-z]{{1,8}})\s+(?P<qty>{_NUM})\)?,?\s+"
                         rf"\$?(?P<price>{_NUM}),?\s+\$?(?P<ext>{_NUM})\s*$")
_LISTED = re.compile(rf"(?:\b(?:qty|quantity)\s*[:=]?\s*(?P<q1>[0-9]{{1,6}})\b|"
                     rf"(?<![\w.\-/])(?P<q2>[0-9]{{1,6}}(?:\.[0-9]+)?)\s*"
                     rf"(?P<unit>{_UNIT_WORDS})\b\.?|\bx\s*(?P<q3>[0-9]{{1,5}})\b)", re.I)
# A price a listed line states per unit, and only that: "@ $4.50", "$4.50/cs", "$4.50 each",
# "$4.50 per case", "@ CAD 4.50", "@ US$4.50", "@ $4.50 USD". Read from its anchor ("@" or
# "$") to the END of the line: anything but punctuation after it ("for 10", "/100",
# "per 100", "less 10%", "for the first 2") makes it no unit price (review 11-17: each was
# cited wrong), and an amount with neither "$" nor a currency code ("@ 12", "@ 24/cs") is a
# pack size as often as a price.
_PRICE_ANCHOR = re.compile(r"(?<![\w.])@|(?:(?<![A-Za-z])(?P<pf>[A-Za-z]{1,4})\s?)?\$")
_PRICE_TAIL = re.compile(
    r"\s*(?:(?P<pre>[A-Za-z]{1,4})?\s?(?P<sym>\$)|(?P<code0>[A-Za-z]{3})\s)?\s*"
    r"(?P<p>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,4})?|[0-9]+(?:\.[0-9]{1,4})?)"
    r"(?![\w,]|\.[0-9])"
    r"(?:\s*(?P<code1>(?!(?:per|for|ea)(?![A-Za-z]))[A-Za-z]{3})(?![A-Za-z]))?"
    r"(?:\s*(?:/|\bper\b)\s*(?P<u>[A-Za-z]{1,8})(?![\w])\.?"
    r"|\s*(?P<ue>each|ea)(?![A-Za-z])\.?)?"
    r"(?:\s*(?P<code2>[A-Za-z]{3})(?![A-Za-z]))?"
    r"(?P<rest>.*)$", re.S)
# a comma only in a thousands group of exactly three digits ("$1,234", "$12,345.67"): any
# other comma ("$4,50", "4,5", "$1,23") may be a decimal comma, so the price is unknown
# (review 11-17: "@ $4,50" was cited as 450 and written as unitPrice 450)
# "CDN" written alone is the Canadian dollar in Canadian mail ("@ CDN 4.50")
_CODE_ALIASES = {"CDN": "CAD"}
# money another way: a foreign sign, or an amount in cents beside an ISO code ("4.50 EUR")
_FOREIGN_SIGN = re.compile("[€£¥]")
_CODE_MONEY = re.compile(r"(?<![A-Za-z])(?P<a>[A-Za-z]{3})\s?[0-9][0-9,]*\.[0-9]{2}(?![0-9])|"
                         r"[0-9]\.[0-9]{2}\s?(?P<b>[A-Za-z]{3})(?![A-Za-z])")
_MAIL = re.compile(r"\S@[\w-]+\.\w")
_CODE = re.compile(r"(?<![\w-])([A-Z0-9][A-Z0-9]*(?:[-./][A-Z0-9]+)*)(?![\w-])", re.I)
_TOTAL = re.compile(r"\b(?:sub\s*-?total|total|tax|freight|shipping|discount|balance|"
                    r"amount due|deposit)\b", re.I)
_PHONE = re.compile(r"\b\d{3}[-. )]\d{3}[-. ]\d{4}\b")
_EXTRA_ROLES = (
    ("unit_price", re.compile(r"^(unit|net|list)? ?(price|cost|rate)( each| per unit| ea)?$")),
    ("extended", re.compile(r"^(ext|extended|line|net)( price| amount| amt| total| value)?$|"
                            r"^(total|amount)( price)?$")),
    ("quantity", re.compile(r"^(qty|quantity)( ordered| order| req| requested)?\.?$|"
                            r"^(order|ordered) (qty|quantity)$")),
)
MAX_LINES = 60


def decimal(raw: Any) -> Decimal | None:
    text = str(raw or "").strip().replace("$", "").replace(",", "").strip("()").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def code_tokens(text: str) -> list[str]:
    """The code-like words of a line: letters and digits together (``GAL10-EBSH``,
    ``MC6005``), or six or more digits (a UPC or a numeric item code); never a date, a
    phone number or a plain amount."""
    out = []
    clean = _PHONE.sub(" ", text)
    for m in _CODE.finditer(clean):
        tok = m.group(1).strip(".-/")
        digits = sum(c.isdigit() for c in tok)
        letters = sum(c.isalpha() for c in tok)
        if len(tok) < 3 or len(tok) > 30 or digits == 0:
            continue
        if letters == 0 and (digits < 6 or "." in tok or "," in tok):
            continue
        if re.fullmatch(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|(?:19|20)\d{2}-\d{2}-\d{2}", tok):
            continue
        if tok not in out:
            out.append(tok)
    return out[:8]


def _checks(qty: Decimal, price: Decimal, ext: Decimal) -> bool:
    if qty <= 0 or price < 0 or ext <= 0:
        return False
    product = qty * price
    return abs(product - ext) <= max(Decimal("0.011"), ext * Decimal("0.005"))


def _priced(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines):
        text = line.strip()
        if not text or _TOTAL.search(text):
            continue
        m = _PRICED.search(text) or _UNIT_FIRST.search(text)
        if not m:
            continue
        qty, price, ext = decimal(m["qty"]), decimal(m["price"]), decimal(m["ext"])
        if qty is None or price is None or ext is None or not _checks(qty, price, ext):
            continue
        head = text[:m.start()].strip()
        tokens = code_tokens(head)
        back = n - 1
        while not tokens and back >= max(0, n - 3):
            above = lines[back].strip()
            if _PRICED.search(above) or _UNIT_FIRST.search(above):
                break                     # the line above is a line of its own
            tokens = [t for t in code_tokens(above) if any(c.isalpha() for c in t)]
            head = above
            back -= 1
        if not tokens:
            continue
        out.append({"tokens": tokens, "quantity": str(qty), "unit": (m["unit"] or "").upper()
                    or None, "unit_price": str(price), "extended": str(ext),
                    "cite": f"L{n + 1}", "basis": "priced", "text": head[:160]})
    return out


def _per_unit(text: str) -> dict[str, Any] | None | bool:
    """``{"price", "per", "currency", "at", "rest"}`` when the line states exactly one money
    amount, it ends the line (nothing but punctuation after its unit or currency code) and
    it carries "$" or a currency code: ``per`` is the unit it names (None: none named),
    ``currency`` the ISO code it names (None: a bare "$"), ``at`` whether "@" marks it,
    ``rest`` the line before it. None when the line states no money; False when it states
    money that is not one such price: the line's price is then unknown, never guessed."""
    from ontology.units import ISO_4217, currency_code
    anchors = list(_PRICE_ANCHOR.finditer(text))
    coded = any((m["a"] or m["b"]).upper() in ISO_4217 for m in _CODE_MONEY.finditer(text))
    if _FOREIGN_SIGN.search(text) or (coded and not anchors):
        return False                     # "€4.50", "4.50 EUR": money, never read as a price
    if not anchors:
        return None
    first = anchors[0]
    # "@ $4.50" is one price: an "@" and the "$" right after it
    if len(anchors) > 2 or (len(anchors) == 2 and not (
            first.group() == "@" and not text[first.end():anchors[1].start()].strip())):
        return False
    at = first.group() == "@"
    start = first.end() if at else first.start()
    if not at and first["pf"] and currency_code(first["pf"] + "$")[0] is None and \
            first["pf"].upper() not in ISO_4217:
        start = first.end() - 1          # "6 cs $4.50/cs": the word before "$" is no sign
    tail = text[start:]
    m = _PRICE_TAIL.fullmatch(tail)
    if m is None or m["rest"].strip(" \t.,;:!)]-*") or (not at and not m["sym"]):
        return False
    codes = set()
    if m["pre"]:
        code = currency_code(m["pre"] + "$")[0] or (m["pre"].upper() if m["pre"].upper()
                                                    in ISO_4217 else None)
        if code is None:
            return False                 # "xx$4.50": a sign we cannot read
        codes.add(code)
    for name in ("code0", "code1", "code2"):
        if m[name]:
            code = _CODE_ALIASES.get(m[name].upper(), m[name].upper())
            if code not in ISO_4217:
                return False             # "@ $4.50 lbs ...": not a currency, not a unit
            codes.add(code)
    if len(codes) > 1 or not (m["sym"] or codes):
        return False                     # "@ 12", "@ 24/cs": a pack size as often as a price
    unit = m["u"] or m["ue"] or None
    if not at and unit is None:
        return False                     # a bare "$45" may be the line's total
    price = decimal(m["p"])
    if price is None or price <= 0:
        return False
    return {"price": str(price), "per": unit, "currency": next(iter(codes), None),
            "at": at, "rest": text[:start if not at else first.start()]}


def stated_unit_price(line: dict[str, Any], currency: str | None) -> tuple[str | None, str | None]:
    """The unit price ``line`` (from ``read_lines``) states for its own unit, in the
    customer's ``currency`` (an ISO code or None: unknown): ``(price, None)`` when it may be
    used; ``(None, reason)`` when the line states money that is no such price (per another
    unit, in another or an unknown currency, or not plainly a unit price): never cited, never
    written; ``(None, None)`` when it states none."""
    from ontology.units import unit_token
    if line.get("price_unknown"):
        return None, ("the line states an amount that is not plainly a price per its unit "
                      "(a total, a price per several, a discount or a pack size): confirm it")
    price = line.get("unit_price")
    if not price:
        return None, None
    per = line.get("price_per")
    if per and unit_token(per) != unit_token(line.get("unit") or ""):
        return None, "the PO's price is per another unit than the quantity: convert it"
    named = line.get("price_currency")
    if named and named != currency:
        return None, (f"the PO's price is in {named}, the customer's currency is "
                      f"{currency or 'not known'}: confirm it")
    return price, None


def _listed(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines):
        text = line.strip(" |\t")
        if not text or len(text) > 200 or _TOTAL.search(text) or _MAIL.search(text):
            continue
        priced = _per_unit(text)
        rest = priced["rest"] if priced else text
        tokens = code_tokens(rest)
        if not tokens:
            continue
        found = list(_LISTED.finditer(rest))
        if len(found) != 1:
            continue                      # no quantity, or several: not one line's order
        m = found[0]
        raw = m["q1"] or m["q2"] or m["q3"]
        qty = decimal(raw)
        tokens = [t for t in tokens if t != raw]
        if qty is None or qty <= 0 or not tokens:
            continue
        out.append({"tokens": tokens, "quantity": str(qty),
                    "unit": (m["unit"] or "").upper() or None,
                    "unit_price": priced["price"] if priced else None,
                    "price_per": priced["per"].upper() if priced and priced["per"] else None,
                    "price_currency": priced["currency"] if priced else None,
                    "price_unknown": priced is False,
                    "extended": None, "cite": f"L{n + 1}", "basis": "listed",
                    "text": text[:160]})
    return out


def _table(text: str, form: str) -> list[dict[str, Any]]:
    from integrations import sheet_grid as sr
    out: list[dict[str, Any]] = []
    for sheet in sr.grid(text, form):
        rows = sheet.rows
        for r in sorted(rows)[:200]:
            roles: dict[str, int] = {}
            for col, _addr, value in rows[r]:
                head = sr.norm_header(value)
                role = next((name for name, rx in _EXTRA_ROLES if rx.match(head)), None) \
                    or sr.role_of(value)
                if role and role not in roles:
                    roles[role] = col
            if not ({"item", "customer_item"} & set(roles)) or "quantity" not in roles:
                continue
            for body in sorted(k for k in rows if k > r)[:MAX_LINES]:
                cells = {c: (v, a) for c, a, v in rows[body]}
                if any(_TOTAL.search(v) for v, _a in cells.values()):
                    break
                qty = decimal((cells.get(roles["quantity"]) or ("", ""))[0])
                codes = [cells[roles[k]][0] for k in ("customer_item", "item")
                         if k in roles and roles[k] in cells]
                tokens = [t for c in codes for t in (code_tokens(c) or [c.strip()]) if t]
                if qty is None or qty <= 0 or not tokens:
                    continue
                price = decimal((cells.get(roles.get("unit_price", -1)) or ("", ""))[0])
                ext = decimal((cells.get(roles.get("extended", -1)) or ("", ""))[0])
                if price is not None and ext is not None and not _checks(qty, price, ext):
                    continue                  # the row's own figures disagree: not read
                unit = (cells.get(roles.get("unit", -1)) or ("", ""))[0]
                cite = (cells.get(roles["quantity"]) or ("", ""))[1]
                out.append({"tokens": tokens[:8], "quantity": str(qty),
                            "unit": unit.upper()[:12] or None,
                            "unit_price": str(price) if price is not None else None,
                            "extended": str(ext) if ext is not None else None,
                            "cite": f"{sheet.name}!{cite}" if sheet.name else cite,
                            "basis": "table", "text": " ".join(codes)[:160]})
            if out:
                return out
    return out


def read_lines(text: str, form: str = "text") -> list[dict[str, Any]]:
    """The document's candidate item lines (module doc), at most ``MAX_LINES``."""
    if not text:
        return []
    found = _table(text, form) if form in ("workbook", "csv") else []
    if not found:
        lines = text.splitlines()
        found = _priced(lines) or (_table(text, form) if form == "text" else []) \
            or _listed(lines)
    return found[:MAX_LINES]


def po_numbers(text: str) -> list[str]:
    return po_scan(text)[0]


def po_scan(text: str) -> tuple[list[str], int]:
    """PO numbers a document states by label (``PO #``, ``Purchase Order Number``,
    ``Order No.``) on the same line or, when the label ends its line, the next. The label
    is a whole word: "po" opening another word ("Poids", "posted", "Point") is no label
    (tuning 02-02..05-03: both wrong cited PO numbers were the next line's code under
    such a word), and a "P.O. Box" is an address. Also the count of marked labels ("PO #",
    "PO number") whose number is not read: the text names a PO this cannot read."""
    out: list[str] = []
    unread = 0
    lines = (text or "").splitlines()
    label = re.compile(r"\b(?:p\.?\s?o\.?|purchase\s+order)(?![^\W\d_])(?!\s*\.?\s*box\b)"
                       r"\s*(?P<mark>#|no\.?|number|num)?(?![^\W\d_])\s*"
                       r"[:.#]?\s*(?P<v>[A-Z0-9][A-Z0-9-]{2,24})?", re.I)
    glued = re.compile(r"(?<![\w-])(P\.?O-?\d[\w-]{2,22})(?![\w-])", re.I)
    # every line, not the first 80 (W4 11-17: 4 of 7 wrong cited PO numbers were threads
    # whose other POs, the one keyed among them, were named after line 80)
    for n, line in enumerate(lines):
        for m in glued.finditer(line):
            if m.group(1) not in out:
                out.append(m.group(1))
        for m in label.finditer(line):
            if glued.match(line, m.start()):
                continue                  # the number is the label's own word
            value = m["v"]
            if not value and not line[m.end():].strip(" \t:#.-|"):
                nxt = lines[n + 1] if n + 1 < len(lines) else ""
                got = [t for t in code_tokens(nxt) if any(c.isdigit() for c in t)]
                value = got[-1] if len(got) == 1 else None
            if not (value and any(c.isdigit() for c in value)):
                value = None
                unread += bool(m["mark"])  # "a new P.O. # for another 20 cs... 3858"
            elif value not in out:
                out.append(value)
            if value:
                # "PO# YMCA4799 for one site and YMCA4800 for the other" (W1-W3 11-15): a
                # number of the same shape later on the label's line is a PO number too
                for sib in _siblings(value, line[m.end():]):
                    if sib not in out:
                        out.append(sib)
    return out[:5], unread


def the_po_number(texts: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """The one PO number ``texts`` ((text, where) pairs: a mail's documents and its
    subject) state: ``{"number", "where", "candidates", "unread"}``. ``number`` is set only
    when they label exactly one PO number (``po_scan``, every line), name no other number
    marked "#" or "No." of its shape, and have no marked PO label whose number is not read;
    else None, with every number read in ``candidates``. Order entry cites it and the
    autopilot writes it; a mail naming another PO is never entered under one of them."""
    found: dict[str, tuple[str, str]] = {}
    unread = 0
    texts = [(text or "", where) for text, where in texts]
    for text, where in texts:
        numbers, missed = po_scan(text)
        unread += missed
        for p in numbers:
            found.setdefault(p.upper(), (p, where))
    # "# 065656 revised for ... and ... for ...: # 065660" (W4 11-17): a number marked "#"
    # or "No." in any of the texts, of the shape of a PO number read, is another PO named
    read = [f[0] for f in found.values()]
    for text, where in texts if read else ():
        for m in _MARKED.finditer(text):
            tok = m.group(1)
            if tok.upper() not in found and any(_same_shape(tok, v) for v in read):
                found[tok.upper()] = (tok, where)
    one = len(found) == 1 and not unread
    first = next(iter(found.values()), (None, None))
    return {"number": first[0] if one else None, "where": first[1],
            "candidates": [f[0] for f in found.values()], "unread": unread}


_MARKED = re.compile(r"(?:#|\bno\b\.?|\bnumber\b)\s*[:.]?\s*([A-Za-z]{0,6}-?[0-9]{3,})(?![\w-])",
                     re.I)


def _same_shape(token: str, value: str) -> bool:
    """Whether ``token`` is written as ``value`` is: the same count of digits alone, or the
    same letters then the same count of digits (``_shape``)."""
    if token.isdigit() and value.isdigit():
        return len(token) == len(value)
    shape = _shape(token)
    return shape is not None and shape == _shape(value)


def _shape(token: str) -> tuple[str, int] | None:
    m = re.fullmatch(r"([A-Za-z]{2,})-?([0-9]{3,})", token)
    return (m.group(1).upper(), len(m.group(2))) if m else None


def _siblings(value: str, rest: str) -> list[str]:
    """Words of ``rest`` of the same letters-then-digits shape as ``value`` (its letters,
    the same count of digits): another PO number written beside it."""
    shape = _shape(value)
    if shape is None:
        return []
    return [t for t in re.findall(r"(?<![\w-])[A-Za-z]{2,}-?[0-9]{3,}(?![\w-])", rest)
            if _shape(t) == shape and t.upper() != value.upper()]


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
