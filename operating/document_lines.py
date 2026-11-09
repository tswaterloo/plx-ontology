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


def _listed(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines):
        text = line.strip(" |\t")
        if not text or len(text) > 200 or _TOTAL.search(text) or "@" in text:
            continue
        tokens = code_tokens(text)
        if not tokens:
            continue
        found = list(_LISTED.finditer(text))
        if len(found) != 1:
            continue                      # no quantity, or several: not one line's order
        m = found[0]
        raw = m["q1"] or m["q2"] or m["q3"]
        qty = decimal(raw)
        tokens = [t for t in tokens if t != raw]
        if qty is None or qty <= 0 or not tokens:
            continue
        out.append({"tokens": tokens, "quantity": str(qty),
                    "unit": (m["unit"] or "").upper() or None, "unit_price": None,
                    "extended": None, "cite": f"L{n + 1}", "basis": "listed",
                    "text": text[:160]})
    return out


def _table(text: str, form: str) -> list[dict[str, Any]]:
    from integrations import sheet_readings as sr
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
    """PO numbers a document states by label (``PO #``, ``Purchase Order Number``,
    ``Order No.``) on the same line or the next."""
    out: list[str] = []
    lines = (text or "").splitlines()
    label = re.compile(r"\b(?:p\.?\s?o\.?|purchase\s+order)\s*(?:#|no\.?|number|num)?\s*"
                       r"[:.#]?\s*(?P<v>[A-Z0-9][A-Z0-9-]{2,24})?", re.I)
    glued = re.compile(r"(?<![\w-])(P\.?O-?\d[\w-]{2,22})(?![\w-])", re.I)
    for n, line in enumerate(lines[:80]):
        for m in glued.finditer(line):
            if m.group(1) not in out:
                out.append(m.group(1))
        for m in label.finditer(line):
            if glued.match(line, m.start()):
                continue                  # the number is the label's own word
            value = m["v"]
            if not value or not any(c.isdigit() for c in value):
                nxt = lines[n + 1] if n + 1 < len(lines) else ""
                got = [t for t in code_tokens(nxt) if any(c.isdigit() for c in t)]
                value = got[-1] if len(got) == 1 else None
            if value and any(c.isdigit() for c in value) and value not in out:
                out.append(value)
    return out[:5]


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
