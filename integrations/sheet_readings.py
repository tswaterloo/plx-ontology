"""Sheet readings: spreadsheet and PDF attachments and SharePoint files read once per file
version into dated, cited rows the alert rules read by index (``operating.proactive_sheets``,
``proactive_business.stock_out_risk``). A step of its own, outside the alert rules and the
delta cycle's projection step (whose code digest it leaves alone): it reads the text the
captures already stored for each file (``text_sha256``: the workbook extractor's
``[Sheet: <name>]`` and ``A1=value | B1=value`` lines, a CSV's own text, a PDF's text layer)
and never the business systems.

What a reading is
-----------------
Layouts are detected, never coded per file or sender. Each sheet's rows are scanned for a
header: a row of cells whose words map to column roles (``ROLES``: item, customer item,
lot, on hand, unit, expiry, invoice reference, amount paid, discount, payment reference and
date, invoice amount, balance, debit, credit, dates). A table's kind follows from its roles
and the words of its file (name, title rows):

* ``on_hand``: an item column and an on-hand column (a plain quantity column never: it is
  an order, an invoice or a quote as often as a stock list): one reading per item row (item, customer
  item, lot, quantity, unit, expiry), dated by the sheet's own date (a dated label or the
  first date above the header), else a date in the file name, else the day it was received
  (``date_basis`` says which);
* ``remittance``: an invoice reference with an amount paid (payment words, a payment
  reference or payment date column): one reading per invoice line (reference, amount paid,
  discount, invoice amount and date, payment reference and date);
* ``statement``: an invoice reference with an amount or balance in a file that says it is
  a statement or open-item list: one reading per line (reference, dates, amount, balance).

Which side a remittance or statement is on (a customer paying us, our own advice to a
supplier, a supplier's statement or one we sent) is not decided here: the rules decide it by
the records the references match. Every reading cites its cell (``Sheet!B7``, a CSV's
``B7``, a PDF's ``L12``) and its file version; amounts are ``Decimal`` text, never floats,
and carry a currency only when the file states one.

Sources, permissions and people
-------------------------------
A source is one captured file a mailbox or SharePoint drive holds (``sources``): its
handle, the store record that holds its text (``Attachment`` or ``SharePointFile``), that
record's audience and markings, the message's sender and recipients (mail) and when it was
received. A source whose record the store does not hold is kept but ``object_id`` NULL: the
rules never read it (no record to cite or to check a reader against). Nothing here decides
who may read: the rules cite the record, so a reader who may not open the attachment never
sees what it supports, and they clear the people on the message first (fail closed).

Incremental, parallel
---------------------
A file version is its ``content_sha256`` and ``text_sha256``; it is read once per reader code
version (``READER``, the digest of this module's source), on every core
(``ProcessPoolExecutor``), and written in one transaction per batch. A file whose text is
not stored yet (the attachments the OCR backlog defers) is not a source until its text
arrives: every run lists the captures again and reads only the new versions, so
``--follow`` picks them up as they land.

Evidence of a run over a capture is the capture's, read-only; fixture runs are
``isolated-fixture``.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence
from operating import declared_schema as _schemas
from operating.declared_schema import sheet_readings as _SHEET_READINGS

READER = "sheet-readings/" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
KINDS = ("on_hand", "remittance", "statement")

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


# ----------------------------------------------------------------------------- store



def open_readings(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    _schemas.apply(conn, _SHEET_READINGS)
    return conn


@dataclass(frozen=True)
class Source:
    handle: str
    origin: str             # mail | sharepoint
    name: str
    content_sha256: str
    text_sha256: str
    received_at: str | None
    participants: tuple[str, ...] = ()
    message: str | None = None

    @property
    def file_key(self) -> str:
        return f"{self.content_sha256}:{self.text_sha256}"


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


def mail_sources(state: Path) -> Iterator[Source]:
    """Every captured mail attachment with stored text whose name is a sheet or a family PDF;
    its message's sender and recipients (lowercase addresses) and received time."""
    db = sqlite3.connect(f"file:{state / 'mail-capture' / 'capture.db'}?mode=ro", uri=True,
                         timeout=60)
    try:
        rows = db.execute(
            "SELECT a.l1_handle, json_extract(a.metadata, '$.name'), a.content_sha256, "
            "a.text_sha256, a.message_handle FROM attachments a WHERE a.content_status = "
            "'captured' AND a.text_sha256 IS NOT NULL AND a.l1_handle IS NOT NULL")
        wanted = [r for r in rows if r[1] and form_of(r[1])]
        from integrations.capture_mail import undecided_free_sql
        free = undecided_free_sql(db)
        withheld: set[str] = set()
        messages: dict[str, tuple[str | None, tuple[str, ...]]] = {}
        handles = sorted({r[4] for r in wanted})
        for i in range(0, len(handles), 500):
            for handle, record in db.execute(
                    "SELECT l1_handle, record FROM messages WHERE superseded_at IS NULL AND "
                    "l1_handle IN (SELECT value FROM json_each(?))",
                    (json.dumps(handles[i:i + 500]),)):
                messages[handle] = _participants(record)
            if free != "1":   # a message naming an undecided address is no reader's
                withheld.update(h for (h,) in db.execute(
                    "SELECT DISTINCT l1_handle FROM messages WHERE l1_handle IN (SELECT "
                    f"value FROM json_each(?)) AND NOT {free}",
                    (json.dumps(handles[i:i + 500]),)))
        for handle, name, content, text, message in wanted:
            if message in withheld:
                continue
            received, people = messages.get(message, (None, ("",)))
            yield Source(handle, "mail", name, content, text, received, people, message)
    finally:
        db.close()


def _participants(record: str) -> tuple[str | None, tuple[str, ...]]:
    try:
        rec = json.loads(record)
    except ValueError:
        return None, ("",)
    found: list[str] = []
    for key in ("from", "to", "cc"):
        value = rec.get(key)
        for one in (value if isinstance(value, list) else [value]):
            if not one:
                continue
            addr = one.get("emailAddress", one) if isinstance(one, dict) else {"address": one}
            text = str(addr.get("address") or "").strip().lower() \
                if isinstance(addr, dict) else str(addr).strip().lower()
            found.append(text)    # "" (no address) never clears downstream
    return rec.get("received_at") or rec.get("sent_at"), tuple(dict.fromkeys(found))


def sharepoint_sources(state: Path) -> Iterator[Source]:
    db = sqlite3.connect(f"file:{state / 'sharepoint-capture' / 'capture.db'}?mode=ro",
                         uri=True, timeout=60)
    try:
        for handle, path, content, text, at in db.execute(
                "SELECT l1_handle, path, content_sha256, text_sha256, json_extract(record, "
                "'$.last_modified') FROM items WHERE superseded_at IS NULL AND removed_reason "
                "IS NULL AND text_sha256 IS NOT NULL AND content_sha256 IS NOT NULL"):
            name = path.rsplit("/", 1)[-1]
            if form_of(name):
                yield Source(handle, "sharepoint", name, content, text, at)
    finally:
        db.close()


def _blob(dirs: Sequence[Path], digest: str) -> bytes | None:
    from integrations.captured_text import read_blob
    for d in dirs:
        got = read_blob(d, digest)
        if got is not None:
            return got
    return None


MAX_PDF_PAGES = 12          # pages of a PDF laid out (a remittance or statement is short)
MAX_PDF_BYTES = 20_000_000  # a larger PDF is read by its stored text only
PDF_SECONDS = 30            # wall time for one PDF's layout; past it the stored text is read


_LAYOUT_CHILD = """
import io, sys
from pypdf import PdfReader
reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()))
pages = [p.extract_text(extraction_mode="layout") or "" for p in reader.pages[:%d]]
sys.stdout.write("\\f".join(pages))
""" % MAX_PDF_PAGES
PDF_CHILD_BYTES = 2 << 30   # address space of one PDF's layout process


def _child_limits() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (PDF_CHILD_BYTES, PDF_CHILD_BYTES))


def pdf_layout(content: bytes) -> str | None:
    """A PDF's pages laid out by position (pypdf's layout mode keeps columns apart), pages
    joined by ``\\f``; None when pypdf is not installed, the file does not open, or it runs
    past ``PDF_SECONDS`` (the capture's stored text is read instead). The layout runs in a
    child process of its own (bounded memory, killed at the time limit): pypdf can
    stall in native code where no in-process timer reaches it."""
    import subprocess
    try:
        done = subprocess.run([sys.executable, "-c", _LAYOUT_CHILD], input=content,
                              capture_output=True, timeout=PDF_SECONDS,
                              preexec_fn=_child_limits, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace") or None


def _read_one(job: tuple) -> tuple[str, str, list, float]:
    """Worker: one file version's tables (``status``: read, no_table, text_missing,
    unreadable). A PDF is laid out from its bytes (``pdf_layout``) when they are held."""
    file_key, name, text_digest, dirs = job[:4]
    started = time.monotonic()
    if name.lower().endswith(".pdf") and len(job) > 4:
        content = _blob([Path(d) for d in dirs], job[4])
        laid = pdf_layout(content) if content is not None and len(content) <= MAX_PDF_BYTES \
            else None
        if laid:
            try:
                found = read_text(laid, "layout", name)
            except Exception as exc:   # noqa: BLE001 - counted, never stops a run
                return file_key, "unreadable:" + type(exc).__name__, [], \
                    time.monotonic() - started
            if found:
                return file_key, "read", found, time.monotonic() - started
    raw = _blob([Path(d) for d in dirs], text_digest)
    if raw is None:
        return file_key, "text_missing", [], time.monotonic() - started
    try:
        found = read_text(raw.decode("utf-8", "replace"), form_of(name) or "text", name)
    except Exception as exc:   # noqa: BLE001 - one bad file never stops a run (counted)
        return file_key, "unreadable:" + type(exc).__name__, [], time.monotonic() - started
    return file_key, ("read" if found else "no_table"), found, time.monotonic() - started


WAVE = 512                      # file versions handed to the workers at once
WAVE_SECONDS = 4 * PDF_SECONDS + 120   # a wave not done by then: its stragglers are dropped


def _results(todo: list[tuple], width: int) -> Iterator[tuple[str, str, list, float]]:
    """Each job's reading, on ``width`` worker processes in waves; a file whose worker
    has not answered within ``WAVE_SECONDS`` of its wave (a reader stuck in native code)
    is ``timed_out`` and its workers are replaced, so one file never stalls a pass."""
    if width == 1 or len(todo) < 64:
        yield from map(_read_one, todo)
        return
    from concurrent.futures import wait
    pool = ProcessPoolExecutor(width)
    try:
        for start in range(0, len(todo), WAVE):
            wave = {pool.submit(_read_one, job): job for job in todo[start:start + WAVE]}
            done, late = wait(wave, timeout=WAVE_SECONDS)
            for fut in done:
                try:
                    yield fut.result()
                except Exception as exc:   # noqa: BLE001 - a dead worker: counted
                    yield wave[fut][0], "unreadable:" + type(exc).__name__, [], 0.0
            if late:
                for fut in late:
                    yield wave[fut][0], "timed_out", [], float(WAVE_SECONDS)
                for proc in list(getattr(pool, "_processes", {}).values()):
                    proc.kill()          # this pass's own workers only
                pool.shutdown(wait=False, cancel_futures=True)
                pool = ProcessPoolExecutor(width)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _store_conn(store: Path | None) -> Any:
    """The ontology the sources' records are resolved in: Postgres when it serves the
    generation at ``store`` (``PLX_ONTOLOGY_PG``; ``only``: always, with no generation
    file), through the compat layer that runs ``_record_of``'s SQL as written; else the
    SQLite generation file; else None (no store: no source has a record). With Postgres
    the only store and not whole, the pass stops here (``store_not_whole``) and the
    sources keep the records they had, never rewritten without one."""
    from operating.pg import ontology_reader
    if ontology_reader.pg_only():
        problem = ontology_reader.whole()
        if problem is not None:
            raise RuntimeError(f"store_not_whole:{problem}")
    if ontology_reader.serves(store):
        from operating.pg.sqlite_compat import PgCompatConnection
        return PgCompatConnection(ontology_reader.dsn())
    if store is not None and Path(store).is_file():
        return sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=60)
    return None


def _resolve(store: Any, handles: Sequence[str]
             ) -> tuple[dict[str, tuple[str, str, str, str]], set[str]]:
    """(``_record_of`` of ``handles``, the handles left unsure). On Postgres each batch is
    followed by a check that Postgres was whole while it was read (a load emptying tables
    mid-pass); a handle of a batch read while it was not, and found in no record, is unsure:
    its source is skipped this pass (it keeps the record it had), never written without
    one. A handle found keeps its record (a row read is a row held)."""
    if store is None or isinstance(store, sqlite3.Connection):
        return _record_of(store, handles), set()
    from operating.pg import ontology_reader
    out: dict[str, tuple[str, str, str, str]] = {}
    unsure: set[str] = set()
    for i in range(0, len(handles), RESOLVE_BATCH):
        batch = handles[i:i + RESOLVE_BATCH]
        found = _record_of(store, batch)
        out.update(found)
        if ontology_reader.whole() is not None:
            unsure.update(h for h in batch if h not in found)
    return out, unsure


# sources whose records are read between two checks that Postgres is whole
RESOLVE_BATCH = 500


def _record_of(store: sqlite3.Connection | None, handles: Sequence[str]
               ) -> dict[str, tuple[str, str, str, str]]:
    """handle -> (object id, type, audience, markings) of the store record holding its text,
    else of the Attachment or SharePointFile record whose current version cites it."""
    out: dict[str, tuple[str, str, str, str]] = {}
    if store is None:
        return out
    for i in range(0, len(handles), 500):
        for handle, oid, typ, aud, marks in store.execute(
                "SELECT t.citation, t.object_id, o.object_type, o.audience_key, t.markings "
                "FROM text_fields t JOIN objects o ON o.object_id = t.object_id WHERE "
                "t.citation IN (SELECT value FROM json_each(?)) AND t.field_path = "
                "'row.content_text'", (json.dumps(list(handles[i:i + 500])),)):
            out.setdefault(handle, (oid, typ, aud, marks))
    # a file whose text the store does not index (most PDFs): its record by the version
    # that cites the handle, at that version's markings
    rest = [h for h in handles if h not in out]
    for i in range(0, len(rest), 500):
        # two index reads (versions by handle, then their records by id): one join let
        # the planner walk the whole record table (a pass stalled 40 minutes on gen 11)
        versions = store.execute(
            "SELECT l1_handle, object_id, markings FROM object_versions WHERE l1_handle IN "
            "(SELECT value FROM json_each(?)) AND superseded_at IS NULL",
            (json.dumps(rest[i:i + 500]),)).fetchall()
        records = {oid: (typ, aud) for oid, typ, aud in store.execute(
            "SELECT object_id, object_type, audience_key FROM objects WHERE object_id IN "
            "(SELECT value FROM json_each(?))", (json.dumps([v[1] for v in versions]),))}
        for handle, oid, marks in versions:
            typ, aud = records.get(oid, (None, None))
            if typ in ("Attachment", "SharePointFile"):
                out.setdefault(handle, (oid, typ, aud, marks))
    return out


def run(state: Path, out: Path, *, store: Path | None, workers: int | None = None,
        sources: Iterable[Source] | None = None, blob_dirs: Sequence[Path] | None = None,
        now: datetime | None = None, limit: int | None = None,
        records: Mapping[str, tuple[str, str, str, str]] | None = None) -> dict[str, Any]:
    """One incremental pass: list the sources, read the new file versions in parallel, store
    their tables and readings, and (re)resolve each source's store record (``records``:
    handle -> (object id, type, audience, markings) given instead). Counts only."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    started = time.monotonic()
    listed = list(sources) if sources is not None else [
        *mail_sources(state), *sharepoint_sources(state)]
    dirs = tuple(str(d) for d in (blob_dirs or (
        state / "mail-capture" / "blobs" / "sha256", state / "mail-capture" / "blobs.nvme" / "sha256",
        state / "sharepoint-capture" / "blobs" / "sha256", state / "sharepoint-capture" / "blobs")))
    conn = open_readings(out)
    done = {k for (k,) in conn.execute("SELECT file_key FROM files WHERE reader = ?", (READER,))}
    jobs: dict[str, tuple] = {}
    for s in listed:
        if s.file_key not in done and s.file_key not in jobs:
            jobs[s.file_key] = (s.file_key, s.name, s.text_sha256, dirs, s.content_sha256)
    todo = list(jobs.values())[:limit] if limit is not None else list(jobs.values())
    counts: dict[str, Any] = {"sources": len(listed), "versions_new": len(todo),
                              "versions_known": len(done)}
    status: dict[str, int] = {}
    kinds: dict[str, int] = {}
    width = max(1, workers or os.cpu_count() or 1)

    def store_batch(batch: list[tuple[str, str, list, float]]) -> None:
        with conn:
            for key, st, found, secs in batch:
                status[st.split(":")[0]] = status.get(st.split(":")[0], 0) + 1
                conn.execute("DELETE FROM tables WHERE file_key = ?", (key,))
                conn.execute("DELETE FROM readings WHERE file_key = ?", (key,))
                n_rows = 0
                for t_no, table in enumerate(found):
                    kinds[table["kind"]] = kinds.get(table["kind"], 0) + len(table["rows"])
                    conn.execute("INSERT INTO tables VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (key, t_no, table["kind"], table["sheet"], table["header"],
                                  json.dumps(table["roles"]), json.dumps(table["facts"])))
                    conn.executemany(
                        "INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?)",
                        [(key, t_no, seq, table["kind"],
                          next(iter(row.values()))[1], json.dumps(row))
                         for seq, row in enumerate(table["rows"])])
                    n_rows += len(table["rows"])
                conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (key, READER, st, json.dumps(sorted({t["kind"] for t in found})),
                              len(found), n_rows, round(secs, 4), stamp))
    batch: list = []
    for got in _results(todo, width):
        batch.append(got)
        if len(batch) >= 2000:
            store_batch(batch)
            batch = []
    store_batch(batch)
    counts["read_seconds"] = round(time.monotonic() - started, 1)
    # sources: (re)resolved every pass (a record the store gains later is picked up)
    sconn = _store_conn(store)
    with_tables = {k for (k,) in conn.execute("SELECT file_key FROM files WHERE status = "
                                               "'read'")}
    useful = [s for s in listed if s.file_key in with_tables]
    records, unsure = (dict(records), set()) if records is not None else _resolve(
        sconn, [s.handle for s in useful])
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(s.handle, s.file_key, s.origin, s.name[:200], *(records.get(s.handle) or
              (None, None, None, None)), s.received_at, json.dumps(list(s.participants)),
              stamp) for s in useful if s.handle not in unsure])
    counts["sources_skipped_not_whole"] = len(unsure)
    if sconn is not None:
        sconn.close()
    counts.update(status=status, readings=kinds, sources_with_tables=len(useful),
                  sources_in_store=sum(1 for s in useful if s.handle in records),
                  seconds=round(time.monotonic() - started, 1))
    conn.close()
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--follow", type=float, default=0.0,
                        help="seconds between passes (0: one pass)")
    args = parser.parse_args(argv)
    while True:
        print(json.dumps(run(args.state, args.out, store=args.store, workers=args.workers,
                             limit=args.limit), sort_keys=True), flush=True)
        if not args.follow:
            return 0
        time.sleep(args.follow)


if __name__ == "__main__":
    sys.exit(main())
