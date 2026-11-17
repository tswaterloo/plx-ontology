"""How a sheet is read (``integrations.sheet_readings``): column roles from header words,
amounts, quantities and dates as sheets write them, a file's text as grids of cells and
the tables of a grid with their kind (on hand, remittance, statement), and which files are
read at all (``form_of``). Pure functions over text: no capture, store or ontology is
opened here, so the alert rules that read a cell's date (``operating.proactive_sheets``)
and the document line reader (``operating.document_lines``) import this alone. Part of
the sheet reader's version (``sheet_readings.READER``)."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import io
import os
import re
from typing import Any, Iterable, Iterator, Mapping, Sequence

# ----------------------------------------------------------------------------- roles

# header words -> column role (first match wins; a role is claimed by its first column)
# (a space in a pattern is optional: glued headers such as ``ItemCode`` or ``QtyOnHand`` match)
ROLES: tuple[tuple[str, re.Pattern], ...] = tuple((role, re.compile(rx.replace(" ", " ?")))
                                                  for role, rx in (
    ("customer_item", r"^(customer|cust|client)s? (item|part|product|sku|code|article)"
                      r"( no| number| code| id)?$"),
    ("payment_amount", r"^(payment|cheque|check|eft|remittance|deposit|transfer) (amount|amt|"
                       r"total)$|^total (paid|payment)$"),
    ("payment_ref", r"^(payment|cheque|check|eft|remittance|trace|transfer|deposit)"
                    r"( reference| ref| no| number| num| id)$"),
    ("payment_date", r"^(payment|pay|cheque|check|eft|value|remittance|deposit|settlement|"
                     r"transfer) date$"),
    ("invoice_date", r"^(invoice|inv|document|doc|transaction|trans|bill|txn) date$"),
    ("due_date", r"^(due|due date|date due|payment due)$"),
    ("invoice_ref", r"^(your |vendor |supplier |customer )?(invoice|inv|bill)( reference| ref|"
                    r" no| number| num| id)?$|^(document|doc|reference|ref|transaction|trans|"
                    r"txn)( no| number| num| reference| ref)?$|^invoice no reference$"),
    ("discount", r"^(discount|disc)( taken| amount| amt)?$"),
    ("amount_paid", r"^(amount|amt) paid$|^paid( amount| amt)?$|^net( amount| paid| payment)?$"
                    r"|^payment$"),
    ("invoice_amount", r"^(invoice|inv|gross|original|orig|document|doc) (amount|amt|total|"
                       r"value)$"),
    ("balance", r"^(balance|open|remaining|outstanding|amount due|amt due|balance due|"
                r"total due|open amount|open balance|amount outstanding|amount remaining)"
                r"( amount| amt)?$"),
    ("debit", r"^(debit|debits|charges|charge)( amount| amt)?$"),
    ("credit", r"^(credit|credits)( amount| amt)?$"),
    ("amount", r"^(amount|amt|total|total amount|value|invoice total)$"),
    ("lot", r"^(lot|batch)( no| number| nbr| code| id)?$"),
    ("expiry", r"^(exp|expiry|expiration|best before|bb|use by)( date)?$"),
    ("on_hand", r"on hand|^(qty|quantity) (available|in stock|avail|counted)$|^(available|"
                r"in stock|stock|stock level|qoh|soh|current stock|inventory|ending inventory|"
                r"closing stock|physical count|counted|balance qty)( qty| quantity)?$"),
    ("quantity", r"^(qty|quantity|units|cases|pcs|count)$"),
    ("unit", r"^(unit|unit of measure|uom|um|u m|base unit|base uom|stocking unit)$"),
    ("item", r"^(item|items|sku|part|product|material|article|stock|pack|bulk|bv|fg|"
             r"finished good)( no| number| nbr| code| id| num)?$|^code$"),
    ("description", r"^(item |product |part )?(description|desc|name)$"),
    ("date", r"^(date|as of|as at|count date|inventory date|statement date|posting date|"
             r"doc date)$"),
))
KEY_ROLES = frozenset({"item", "customer_item", "invoice_ref"})
REMIT_WORDS = re.compile(r"remit(?!\s*to\b)|payment advice|advice of payment|\beft\b|\bach\b|wire|"
                         r"payment notification|paid invoices|payment detail|cheque|check no",
                         re.I)
STATEMENT_WORDS = re.compile(r"statement|open items?|aged|ageing|aging|account summary|"
                             r"balance forward|\bsoa\b|outstanding invoices", re.I)
STOCK_WORDS = re.compile(r"inventory|on ?hand|stock|count|\bsoh\b|\bqoh\b", re.I)
TOTAL = re.compile(r"\b(sub)?total\b|\bgrand total\b", re.I)
CURRENCY = re.compile(r"\b(CAD|USD|EUR|GBP)\b")
HEADER_SCAN_ROWS = 40          # rows of a sheet searched for a header
EMPTY_RUN = 3                  # consecutive empty rows that end a table
MAX_ROWS = 5000                # readings per table


def norm_header(text: str) -> str:
    text = str(text or "").replace("#", " no ").replace("&", " and ").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def role_of(text: str) -> str | None:
    words = norm_header(text)
    if not words or len(words) > 40 or SENSITIVE.search(words):
        return None
    for role, rx in ROLES:
        if rx.search(words):
            return role
    return None


# ----------------------------------------------------------------------------- values

_NUM = re.compile(r"^\(?-?[$€£]?\s*-?[0-9][0-9,]*(\.[0-9]+)?\)?-?(\s*(CR|DR|CAD|USD))?$", re.I)
_QTY = re.compile(r"^(-?[0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Za-z][A-Za-z./ ]{0,11})?$")


def amount(raw: Any) -> Decimal | None:
    """A money or quantity figure as written: thousands separators, a currency sign,
    parentheses, a trailing minus or ``CR`` make it negative; anything else is None."""
    text = str(raw or "").strip()
    if not text or not _NUM.match(text):
        return None
    negative = text.startswith("(") or text.endswith(")") or "-" in text \
        or text.upper().endswith("CR")
    digits = re.sub(r"[^0-9.]", "", text)
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    if -value.as_tuple().exponent > 6:     # a binary float written out (1307.8800000000001)
        value = value.quantize(Decimal("0.000001")).normalize()
        value = value.quantize(Decimal(1)) if value == value.to_integral() else value
    return -value if negative and value else value


def quantity(raw: Any) -> tuple[Decimal | None, str | None]:
    """A quantity and the unit written beside it (``15 cse``)."""
    text = str(raw or "").strip()
    plain = amount(text)
    if plain is not None:
        return plain, None
    found = _QTY.match(text)
    if not found:
        return None, None
    try:
        value = Decimal(found.group(1).replace(",", ""))
    except InvalidOperation:
        return None, None
    unit = (found.group(2) or "").strip().lower() or None
    return value, unit


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                        "sep", "oct", "nov", "dec"), 1)}


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def parse_date(raw: Any, order: str = "md", *, serial: bool = False) -> date | None:
    """A date as a sheet writes it: ISO, ``YYYYMMDD``, ``d-Mon-yy``, ``Mon d, yyyy``,
    numeric ``a/b/y`` (``order``: ``md`` or ``dm``, the column's), or, with ``serial``, an
    Excel day number. None when it is none of these or not a real day."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if serial and re.fullmatch(r"[0-9]{5}(\.[0-9]+)?", text):
            n = int(float(text))
            return date(1899, 12, 30) + timedelta(days=n) if 30000 <= n <= 60000 else None
        if m := re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$", text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[-/ .]([A-Za-z]{3})[A-Za-z]*\.?[-/ ,.]+(\d{2,4})", text):
            return date(_year(int(m[3])), _MONTHS[m[2].lower()], int(m[1]))
        if m := re.fullmatch(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", text):
            return date(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
        if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})(?:\s.*)?", text):
            a, b, y = int(m[1]), int(m[2]), _year(int(m[3]))
            month, day = (a, b) if order == "md" else (b, a)
            return date(y, month, day)
    except (ValueError, KeyError):
        return None
    return None


def date_order(values: Iterable[str]) -> str:
    """``dm`` when a numeric date of the column can only be day first, else ``md``."""
    for text in values:
        m = re.fullmatch(r"\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}.*", str(text or ""))
        if m and int(m[1]) > 12 >= int(m[2]):
            return "dm"
        if m and int(m[2]) > 12 >= int(m[1]):
            return "md"
    return "md"


def name_date(name: str) -> date | None:
    """A date written into a file name (``Inventory 2026-10-31.xlsx``, ``Oct 31 2026``)."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name or "")
    for rx in (r"(20\d{2})[-_. ]?(\d{2})[-_. ]?(\d{2})", r"(\d{1,2})[-_. ](\d{1,2})[-_. ](20\d{2})",
               r"([A-Za-z]{3})[A-Za-z]*[-_. ]+(\d{1,2})(?:st|nd|rd|th)?,?[-_. ]+(20\d{2})"):
        m = re.search(rx, stem)
        if not m:
            continue
        try:
            if rx.startswith("(20"):
                return date(int(m[1]), int(m[2]), int(m[3]))
            if rx.startswith("(\\d"):
                return date(int(m[3]), int(m[1]), int(m[2]))
            return date(int(m[3]), _MONTHS[m[1].lower()[:3]], int(m[2]))
        except (ValueError, KeyError):
            continue
    return None


# ----------------------------------------------------------------------------- grids

@dataclass
class Sheet:
    name: str
    rows: dict[int, list[tuple[int, str, str]]] = field(default_factory=dict)  # col, cell, v
    # a page laid out by position (``layout_grid``): ``col`` is the cell's first character
    # and ``ends`` its last, so a body cell is placed under the header cell it overlaps
    ends: dict[tuple[int, int], int] | None = None


_SHEET = re.compile(r"^\[Sheet: (.*)\]$")
_CELL_SPLIT = re.compile(r"(?:^|\s\|\s)([A-Z]{1,4})([1-9][0-9]{0,6})=")


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _letters(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def workbook_grid(text: str) -> list[Sheet]:
    """The cells of the workbook extractor's text (``[Sheet: name]`` then ``A1=v | B1=v``)."""
    sheets: list[Sheet] = []
    current = None
    for line in text.splitlines():
        if m := _SHEET.match(line.strip()):
            current = Sheet(m[1])
            sheets.append(current)
            continue
        if current is None or not _CELL_SPLIT.match(line):
            continue
        parts = _CELL_SPLIT.split(line)
        # parts: ['', L, R, value, L, R, value, ...]
        for i in range(1, len(parts) - 2, 3):
            letters, row, value = parts[i], int(parts[i + 1]), parts[i + 2].strip()
            if value:
                current.rows.setdefault(row, []).append(
                    (_col(letters), f"{letters}{row}", value))
    return sheets


def csv_grid(text: str) -> list[Sheet]:
    sheet = Sheet("")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    for n, row in enumerate(csv.reader(io.StringIO(text, newline=""), dialect), 1):
        cells = [(i, f"{_letters(i)}{n}", v.strip()) for i, v in enumerate(row, 1) if v.strip()]
        if cells:
            sheet.rows[n] = cells
        if n >= 100_000:
            break
    return [sheet]


def text_grid(text: str) -> list[Sheet]:
    """A PDF's text layer: one row per line, cells split at two or more spaces or a tab."""
    sheet = Sheet("")
    for n, line in enumerate(text.splitlines(), 1):
        cells = [c.strip() for c in re.split(r"\t|\s{2,}", line) if c.strip()]
        if cells:
            sheet.rows[n] = [(i, f"L{n}", c) for i, c in enumerate(cells, 1)]
    return [sheet]


_GAP = re.compile(r"\S+(?: \S+)*")      # a cell: words one space apart


def layout_grid(text: str) -> list[Sheet]:
    """A PDF page laid out by position (pypdf ``extraction_mode="layout"``): one row per
    line, a cell per run of words one space apart, at its character position; each page
    (``\f``) its own sheet, cited ``p<page>L<line>``."""
    sheets = []
    for page_no, page in enumerate(text.split("\f"), 1):
        sheet = Sheet(f"p{page_no}", ends={})
        for n, line in enumerate(page.splitlines(), 1):
            cells = []
            for m in _GAP.finditer(line):
                cells.append((m.start(), f"p{page_no}L{n}", m.group()))
                sheet.ends[(n, m.start())] = m.end()
            if cells:
                sheet.rows[n] = cells
        if sheet.rows:
            sheets.append(sheet)
    return sheets


def _two_line_header(sheet: Sheet, ordered: Sequence[int], i: int,
                     roles: dict[str, tuple[int, str, str]]) -> dict[str, tuple]:
    """A laid-out header written on two lines (``Amount`` above ``Paid``): each word of
    the line just above or below that holds no figure joins the header cell it overlaps;
    the merge that maps more columns wins, the header row stays the found one."""
    header_row = ordered[i]
    best = roles
    for j in (i - 1, i + 1):
        if not 0 <= j < len(ordered) or abs(ordered[j] - header_row) != 1:
            continue
        other = sheet.rows[ordered[j]]
        if any(re.search(r"\d", v) for _c, _a, v in other) or len(other) > len(
                sheet.rows[header_row]) + 2:
            continue
        merged = []
        for col, addr, value in sheet.rows[header_row]:
            end = sheet.ends.get((header_row, col), col + len(value))
            extra = [v for c, _a, v in other
                     if min(end, sheet.ends.get((ordered[j], c), c + len(v))) - max(col, c) > 0]
            words = [*extra, value] if j < i else [value, *extra]
            merged.append((col, addr, " ".join(words)))
        found = _header(merged)
        if found is not None and len(found) > len(best):
            best = found
    return best


def _placed(sheet: Sheet, r: int, header_row: int,
            roles: Mapping[str, tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    """A laid-out row's cells, each moved to the header column it overlaps most (else the
    nearest by centre): numbers are right-aligned, text left-aligned, under one header."""
    if sheet.ends is None:
        return sheet.rows[r]
    heads = sorted({h[0] for h in roles.values()} | {c for c, _a, _v in
                                                      sheet.rows[header_row]})
    spans = [(h, sheet.ends.get((header_row, h), h + 1)) for h in heads]
    out, taken = [], set()
    for col, addr, value in sheet.rows[r]:
        end = sheet.ends.get((r, col), col + len(value))
        best = max(spans, key=lambda sp: (min(end, sp[1]) - max(col, sp[0]),
                                          -abs((col + end) / 2 - (sp[0] + sp[1]) / 2)))
        if best[0] not in taken:
            taken.add(best[0])
            out.append((best[0], addr, value))
    return out


def grid(text: str, form: str) -> list[Sheet]:
    if form == "layout":
        return layout_grid(text)
    if form == "workbook":
        return workbook_grid(text)
    if form == "csv":
        return csv_grid(text)
    return text_grid(text)


# ----------------------------------------------------------------------------- tables

# the labelled facts kept: what the rules read (dates, currency) and document numbers;
# never a name (a payer or contact may be a person) or an account
LABELS = re.compile(r"^(statement date|date|as of|as at|inventory date|count date|report date|"
                    r"payment date|cheque date|check date|value date|period ending|period end|"
                    r"week ending|payment no|payment number|payment reference|cheque no|"
                    r"check no|eft no|currency)$")
# bank and card details: a cell so labelled, and the value beside it, are never stored, and
# a column so headed takes no role
SENSITIVE = re.compile(r"\b(bank|banking|account|acct|a c|iban|routing|aba|transit|swift|bic|"
                       r"sort code|bsb|institution|branch|card|credit card|cvv|pan)\b")


def _fact_value(label: str, value: str, order: str) -> str | None:
    """A labelled fact as it is kept: a date as written when it reads as one, a currency
    as its code, a document number when it holds a digit and no bank or card word; else
    nothing (a name or free text beside a label is never stored)."""
    if label == "currency":
        code = CURRENCY.search(value)
        return code[1] if code else None
    if label.endswith(("date", "of", "at", "ending", "end")) or label == "date":
        return value[:40] if parse_date(value, order, serial=True) is not None else None
    if re.search(r"\d", value) and not SENSITIVE.search(norm_header(value)) \
            and len(value) <= 40:
        return value
    return None


def _header(cells: Sequence[tuple[int, str, str]]) -> dict[str, tuple[int, str, str]] | None:
    roles: dict[str, tuple[int, str, str]] = {}
    for cell in cells:
        role = role_of(cell[2])
        if role is not None and role not in roles:
            roles[role] = cell
    if len(roles) < 2 or not KEY_ROLES & set(roles):
        return None
    return roles


def _kind(roles: Mapping[str, Any], words: str) -> str | None:
    have = set(roles)
    # an on-hand column itself: a plain quantity beside an item is an order, an invoice or
    # a quote as often as a stock list (measured on the mail capture), so it never counts
    if have & {"item", "customer_item"} and "on_hand" in have:
        return "on_hand"
    if "invoice_ref" not in have:
        return None
    paid = have & {"amount_paid", "payment_amount", "amount", "discount"}
    owed = have & {"balance", "amount", "debit", "invoice_amount"}
    remit = (len(REMIT_WORDS.findall(words)) > 0) + len(have & {"payment_ref", "payment_date",
                                                                "amount_paid"})
    stmt = (len(STATEMENT_WORDS.findall(words)) > 0) + len(have & {"balance", "due_date",
                                                                   "debit"})
    if paid and remit > stmt:
        return "remittance"
    if owed and stmt > remit and STATEMENT_WORDS.search(words):
        return "statement"
    return None


def _facts(rows: Mapping[int, list], before: int, order: str) -> dict[str, Any]:
    """Labelled values and the first date above a header (``label: value`` cell pairs)."""
    out: dict[str, Any] = {}
    for r in sorted(k for k in rows if k < before):
        cells = rows[r]
        hidden = {i + 1 for i, (_c, _a, v) in enumerate(cells)
                  if SENSITIVE.search(norm_header(v.split(":")[0]))}
        for i, (_c, addr, value) in enumerate(cells):
            label = norm_header(value.split(":")[0])
            if SENSITIVE.search(label) or i in hidden:
                continue   # a bank or card detail, or the value beside its label
            rest = value.split(":", 1)[1].strip() if ":" in value else ""
            if LABELS.match(label):
                got = rest or (cells[i + 1][2] if i + 1 < len(cells) else "")
                cell = addr if rest else (cells[i + 1][1] if i + 1 < len(cells) else addr)
                # a label beside another label is a header row, its values below
                if got and not LABELS.match(norm_header(got)) and role_of(got) is None:
                    kept = _fact_value(label, got, order)
                    if kept is not None:
                        out.setdefault(label, (kept, cell))
            day = parse_date(rest or value, order)
            if day is not None and 2000 <= day.year <= 2100:
                out.setdefault("_first_date", (day.isoformat(), addr))
            if cur := CURRENCY.search(value):
                out.setdefault("currency", (cur[1], addr))
    return out


def _value(cells: Mapping[int, tuple[str, str]], roles: Mapping[str, tuple[int, str, str]],
           role: str) -> tuple[str, str] | None:
    head = roles.get(role)
    if head is None:
        return None
    return cells.get(head[0])


def tables(sheet: Sheet, words: str) -> Iterator[dict[str, Any]]:
    """Every table of a sheet: its header, kind, labelled facts and typed rows."""
    rows = sheet.rows
    ordered = sorted(rows)
    start = 0
    while start < len(ordered):
        found = None
        for i in range(start, min(len(ordered), start + HEADER_SCAN_ROWS)):
            roles = _header(rows[ordered[i]])
            if roles is not None:
                found = (i, roles)
                break
        if found is None:
            return
        i, roles = found
        header_row = ordered[i]
        if sheet.ends is not None:
            roles = _two_line_header(sheet, ordered, i, roles)
        top = " ".join(v for r in ordered[max(0, i - 12):i] for _c, _a, v in rows[r])
        kind = _kind(roles, words + " " + top)
        body: list[int] = []
        last = header_row
        j = i + 1
        while j < len(ordered) and len(body) < MAX_ROWS:
            r = ordered[j]
            if r - last > EMPTY_RUN or _header(rows[r]) is not None:
                break
            body.append(r)
            last = r
            j += 1
        start = j
        if kind is None:
            continue
        date_cols = [h[0] for k, h in roles.items() if k.endswith("date") or k in ("expiry",
                                                                                   "date")]
        order = date_order(v for r in body for c, _a, v in rows[r] if c in date_cols)
        facts = _facts(rows, header_row, order)
        out_rows = []
        for r in body:
            cells = {c: (v, a) for c, a, v in _placed(sheet, r, header_row, roles)}
            if any(TOTAL.search(v) for v, _a in cells.values()):
                continue
            got = _row(kind, cells, roles, order)
            if got is not None:
                out_rows.append(got)
        if out_rows:
            yield {"kind": kind, "sheet": sheet.name, "header": roles[next(iter(roles))][1],
                   "roles": {k: v[2][:60] for k, v in roles.items()}, "facts": facts,
                   "order": order, "rows": out_rows}


def _row(kind: str, cells: Mapping[int, tuple[str, str]],
         roles: Mapping[str, tuple[int, str, str]], order: str) -> dict[str, Any] | None:
    def val(role: str) -> tuple[str, str] | None:
        return _value(cells, roles, role)

    def day(role: str) -> tuple[str, str] | None:
        got = val(role)
        d = parse_date(got[0], order, serial=True) if got else None
        return (d.isoformat(), got[1]) if d else None

    def money(role: str) -> tuple[str, str] | None:
        got = val(role)
        n = amount(got[0]) if got else None
        return (str(n), got[1]) if n is not None else None

    fields: dict[str, tuple[str, str]] = {}
    if kind == "on_hand":
        item, cust = val("item"), val("customer_item")
        if not (item or cust):
            return None
        q = val("on_hand")
        qty, unit = quantity(q[0]) if q else (None, None)
        if qty is None:
            return None
        fields["qty"] = (str(qty), q[1])
        if item:
            fields["item"] = (item[0][:80], item[1])
        if cust:
            fields["customer_item"] = (cust[0][:80], cust[1])
        if (u := val("unit")) and len(u[0]) <= 12:
            fields["unit"] = (u[0].strip().lower(), u[1])
        elif unit:
            fields["unit"] = (unit, q[1])
        if lot := val("lot"):
            fields["lot"] = (lot[0][:60], lot[1])
        for role in ("expiry", "date"):
            if d := day(role):
                fields[role] = d
        return fields
    ref = val("invoice_ref")
    if not ref or not re.search(r"\d", ref[0]) or len(ref[0]) > 40:
        return None
    # a figure, a label or a number cut at the cell's edge is no document reference
    if re.search(r"[$:]|\.\.\.|[-/]$|\s", ref[0].strip()) or re.fullmatch(
            r"-?[\d,]*\.\d+", ref[0].strip()):
        return None
    text = ref[0].strip()
    if re.fullmatch(r"\d(\.\d+)?E\d+", text, re.I):   # a number cell written as 4.8E9
        number = Decimal(text)
        text = str(int(number)) if number == number.to_integral() else text
    fields["ref"] = (text, ref[1])
    for role in ("amount_paid", "payment_amount", "amount", "discount", "invoice_amount",
                 "balance", "debit", "credit"):
        if m := money(role):
            fields[role] = m
    for role in ("invoice_date", "payment_date", "due_date", "date"):
        if d := day(role):
            fields[role] = d
    if p := val("payment_ref"):
        fields["payment_ref"] = (p[0][:40], p[1])
    if kind == "remittance" and not fields.keys() & {"amount_paid", "payment_amount", "amount"}:
        return None
    if kind == "statement" and not fields.keys() & {"balance", "amount", "debit", "credit",
                                                    "invoice_amount"}:
        return None
    return fields


def read_text(text: str, form: str, name: str) -> list[dict[str, Any]]:
    """The tables of one file's text (``form``: ``workbook``, ``csv``, ``text`` or a PDF's
    ``layout``)."""
    words = name or ""
    out = []
    for sheet in grid(text, form):
        out.extend(tables(sheet, words + " " + sheet.name))
    return out


SUFFIX_FORM = {".xlsx": "workbook", ".xlsm": "workbook", ".xls": "workbook", ".xltx": "workbook",
               ".csv": "csv", ".txt": "csv", ".pdf": "text"}
# a PDF is read when its name says what family it is (the text layer of every other PDF is
# a document, not a table); spreadsheets are always read
PDF_FAMILY = re.compile(r"remit|payment|advice|\beft\b|statement|\bsoa\b|open ?item|aged|"
                        r"aging|inventory|on ?hand|stock", re.I)


def form_of(name: str) -> str | None:
    suffix = os.path.splitext(name.lower())[1]
    form = SUFFIX_FORM.get(suffix)
    if form == "text" and not PDF_FAMILY.search(name):
        return None
    return form
