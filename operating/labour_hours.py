"""Labour hours from the emailed department time sheets: department and week totals only.

Hours only, never pay, rates,
wages or any money about people; the product shows department and week totals (hours,
overtime hours, headcount-days), hours per production run or order only where a sheet
names them, and the overtime trend. Per-person rows are never shown, never searchable and
never sent to a model.

**Reading** (``read_grid`` / ``sheet_rows``): a captured time-sheet attachment's text (the
sheet-by-sheet rendering of ``integrations.xlsx_extract``: ``[Sheet: <name>]`` then
``A1=value | B1=FORMULA(..); cached=value``), a CSV, or a message body (an HTML table or
tab/pipe separated lines) becomes a grid of cells; the layout is detected, never assumed:
the header row is the first that names an hours column (total, regular, overtime or day
columns) beside a name column; a column whose header names money (rate, pay, wage, cost,
amount, ``$`` ...) is never read at all. A row is a person when its name cell holds words
and its hours are numbers within bounds (a day cell at most 24, a week at most 100); a
"total" row ends the table. The department is only ever a word of ``DEPARTMENTS`` (from a
"Department:" cell, a department column, the sheet name, the file name or the subject),
never free text, so a person's name can never become a department; none found:
``unassigned``. The week is a stated week-ending / period / date cell or the day
columns' dates; failing those the message's sent date (basis ``sent``).

**Per-person rows are marked ``hr``** (``PersonHours.markings``) and live only in memory
for one build: the person is a keyed digest (a random key per build, never stored), the
latest message's sheet wins for one person and week (a revised sheet replaces the first).
``LabourStore`` refuses any row that is not an aggregate.

**People exclusion, fail closed** (``ExclusionCheck``): the guard must be available and
every excluded person must be resolvable to their directory addresses, else nothing is
built. A message whose mailbox, sender or recipients include an excluded person, or an
internal address the guard cannot decide, is skipped; a sheet with a row whose name matches
an excluded person's name forms (from their addresses) is dropped whole, so their hours
never enter even an aggregate.

**Small groups suppressed**: a department-week with fewer than ``MIN_GROUP`` people is not
stored (only counted); per-run rows are stored only when every run of that department-week
and the hours on no run each have ``MIN_GROUP`` people or none, so no difference of two
served figures isolates fewer than ``MIN_GROUP`` people.

Overtime is a department-week figure only, never on a run row (the department's minus a
run's would be the off-run people's), and only when ``MIN_GROUP`` people or more worked
some.

**Known limitation**: totals are published per week and replaced per build, so a figure can
move between two weeks or two builds by one person's hours: one person joining or leaving a
group, or a revised sheet changing one person's hours, shifts a total by exactly that
person's figure. A reader comparing the two sees that difference.

**Access**: every aggregate carries the ``hr`` marking; only a reader holding one of
``GRANTS`` (the host's grants file, ``labour_grants.json`` for the definitions; the owner
assigns them, nobody holds one by default) sees any figure, the page, the alert or the Ask
answer. Labour questions are answered here from the aggregates alone and never reach the
model path.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence
from operating import declared_schema as _schemas
from operating.declared_schema import labour_hours as _LABOUR_HOURS

MARKING = "hr"
GRANTS = ("hr", "ops-management")
MIN_GROUP = 3
SPIKE_RATIO = Decimal("1.30")
BASELINE_WEEKS = 8
MIN_BASELINE = 4          # prior weeks with overtime a spike is judged against, at least
MAX_DAY_HOURS = Decimal(24)
# Hours in one week past which a sheet with no overtime column is read as overtime (the
# Ontario Employment Standards weekly threshold); labelled basis over_44_hours.
OVERTIME_AFTER = Decimal(44)
MAX_WEEK_HOURS = Decimal(100)
UNASSIGNED = "unassigned"
GRANTS_FILE_ENV = "PLX_LABOUR_GRANTS_FILE"
STORE_NAME = "labour/labour.db"

# A time sheet by its file name or subject.
TIME_SHEET = re.compile(r"\btime[\s_-]*(?:sheet|card)s?\b|\btime(?:sheet|card)s?\b|"
                        r"\bhours[\s_-]*(?:sheet|report|worked)\b", re.I)

# Canonical departments and the words that name them (generic manufacturing departments).
DEPARTMENTS: dict[str, tuple[str, ...]] = {
    "production": ("production", "manufacturing", "plant", "processing"),
    "packaging": ("packaging", "packing", "filling", "bottling"),
    "blending": ("blending", "mixing", "compounding", "batching"),
    "warehouse": ("warehouse", "inventory", "stores"),
    "shipping": ("shipping", "receiving", "logistics", "dispatch"),
    "quality": ("quality", "qa", "qc", "lab", "laboratory"),
    "sanitation": ("sanitation", "cleaning", "janitorial"),
    "maintenance": ("maintenance", "facilities", "engineering"),
    "office": ("office", "admin", "administration", "accounting", "sales",
               "purchasing", "customer service"),
}
_DEPT_WORDS = sorted(((w, d) for d, ws in DEPARTMENTS.items() for w in ws),
                     key=lambda p: -len(p[0]))

_MONEY = re.compile(r"\$|\b(?:rate|rates|pay|paid|wage|wages|salary|cost|costs|amount|"
                    r"earnings?|gross|net|bonus|premium|dollars?|cad|usd|invoice|charge|"
                    r"bill(?:ed|ing)?|price)\b", re.I)
_NAME_H = re.compile(r"^(?:employee(?:'?s)?\s*)?(?:full\s*)?name$|^employee$|^worker$|"
                     r"^associate$|^staff$|^team member$", re.I)
_TOTAL_H = re.compile(r"^(?:total|ttl|weekly|week)\s*(?:hours|hrs|hr)$|^(?:hours|hrs|hour)\s*"
                      r"(?:total|worked)$|^(?:total\s*)?(?:hours|hrs|hour)(?:\s*\(hrs\))?$|^total$", re.I)
_REG_H = re.compile(r"^(?:reg(?:ular)?|straight(?:\s*time)?|st)\s*(?:hours|hrs|time)?$", re.I)
_OT_H = re.compile(r"^(?:o\.?\s?t\.?|over\s*time|overtime|time and a half|1\.5x?|dt|"
                   r"double\s*time)\s*(?:hours|hrs)?$", re.I)
_DEPT_H = re.compile(r"^(?:dept\.?|department|area)$", re.I)
_RUN_H = re.compile(r"^(?:production\s*)?(?:run|order|job|batch|lot|work\s*order|wo|"
                    r"mo|production order)\s*(?:#|no\.?|number)?$", re.I)
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DATE_H = re.compile(r"^(?:date|day|work\s*date)$", re.I)
_INOUT_H = re.compile(r"^(?:time\s*)?(?:in|out)$|^(?:start|finish|end)(?:\s*time)?$", re.I)
_DAY_MONTH = re.compile(r"^\d{1,2}[-/ ](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?$",
                        re.I)
_PERSON_LABEL = re.compile(r"^(?:employee(?:'?s)?\s*(?:name)?|name)\s*[:\-]\s*(.*)$", re.I)
_DAY_H = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?(?:\s+(.*))?$", re.I)
_WEEK_LABEL = re.compile(r"(?:week\s*(?:ending|end|of)|w/e|pay\s*period|period\s*"
                         r"(?:ending|end)|period|date)\s*[:\-]?\s*(.*)$", re.I)
_DEPT_LABEL = re.compile(r"^(?:dept\.?|department|area)\s*[:\-]\s*(.+)$", re.I)
_STOP_ROW = re.compile(r"^(?:sub)?totals?\b|^grand total|^signature|^approved|^supervisor",
                       re.I)
_CELL = re.compile(r"^([A-Z]{1,3})(\d{1,7})=(.*)$", re.S)
_RUN_VALUE = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9#/._-]{1,30}$")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}


class LabourRefused(Exception):
    """Nothing is built or served (an unavailable people exclusion, a non-aggregate row)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ----------------------------------------------------------------------- grid reading

def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def read_grid(text: str) -> list[tuple[str, dict[int, dict[int, str]]]]:
    """``text`` as sheets of cells: ``[(sheet name, {row: {column: value}})]``. The
    workbook rendering's formula cells read their cached value only."""
    if not isinstance(text, str) or not text.strip():
        return []
    if "[Sheet: " in text and re.search(r"^[A-Z]{1,3}\d+=", text, re.M):
        sheets: list[tuple[str, dict[int, dict[int, str]]]] = []
        for line in text.splitlines():
            if line.startswith("[Sheet: ") and line.endswith("]"):
                sheets.append((line[8:-1], {}))
                continue
            if not sheets or not _CELL.match(line):
                continue
            for piece in line.split(" | "):
                match = _CELL.match(piece)
                if match is None:
                    continue
                value = match.group(3)
                if value.startswith("FORMULA("):
                    value = value.rpartition("; cached=")[2]
                    value = "" if value == "(missing)" else value
                sheets[-1][1].setdefault(int(match.group(2)), {})[_col(match.group(1))] = \
                    value.strip()
        return sheets
    if re.search(r"<t[dh][\s>]", text, re.I):
        parser = _Tables()
        parser.feed(text)
        rows = parser.rows
    else:
        rows = []
        for line in text.splitlines():
            if "\t" in line:
                parts = line.split("\t")
            elif line.count("|") >= 2:
                parts = line.strip().strip("|").split("|")
            elif line.count(",") >= 2:
                parts = line.split(",")
            else:
                parts = re.split(r"\s{2,}", line.strip())
            if any(p.strip() for p in parts):
                rows.append([p.strip() for p in parts])
    return [("", {i + 1: {j + 1: v for j, v in enumerate(r)} for i, r in enumerate(rows)})]


def number(value: Any) -> Optional[Decimal]:
    """A cell's number of hours, or None (text, money, a time of day)."""
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "")
    if not text or "$" in text or ":" in text:
        return None
    try:
        out = Decimal(text)
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def parse_date(value: Any) -> Optional[date]:
    """A stated date in a cell: ISO, m/d/y, ``Mon dd, yyyy``, ``dd Mon yyyy`` or an Excel
    serial; None when ambiguous or absent."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    try:
        if match:
            return date(*map(int, match.groups()))
        match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
        if match:
            m, d, y = map(int, match.groups())
            if m > 12 >= d:
                m, d = d, m
            return date(y + 2000 if y < 100 else y, m, d)
        match = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
                          text)
        if match and match.group(1).lower() in _MONTHS:
            return date(int(match.group(3)), _MONTHS[match.group(1).lower()],
                        int(match.group(2)))
        match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", text)
        if match and match.group(2).lower() in _MONTHS:
            return date(int(match.group(3)), _MONTHS[match.group(2).lower()],
                        int(match.group(1)))
        if re.fullmatch(r"\d{5}(?:\.0+)?", text) and 36000 <= float(text) <= 60000:
            return date(1899, 12, 30) + timedelta(days=int(float(text)))
    except ValueError:
        return None
    return None


def week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def department_in(*texts: Any) -> Optional[str]:
    """The first ``DEPARTMENTS`` word any of ``texts`` names (whole words), else None."""
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        folded = " " + re.sub(r"[^a-z&]+", " ", text.lower()) + " "
        for word, dept in _DEPT_WORDS:
            if f" {word} " in folded:
                return dept
    return None


# ----------------------------------------------------------------------- per person (hr)

@dataclass(frozen=True)
class PersonHours:
    """One person's hours on one sheet: intermediate, marked ``hr``, never stored or
    served. ``name`` is held only for the exclusion check of the build."""
    name: str
    department: str
    week: date
    week_basis: str
    hours: Decimal
    overtime: Optional[Decimal]
    days: Optional[int]
    run: Optional[str]
    markings: tuple[str, ...] = (MARKING,)


@dataclass
class SheetReading:
    status: str                       # parsed | no_table | implausible | empty
    rows: list[PersonHours] = field(default_factory=list)


def _header(row: Mapping[int, str]) -> Optional[dict[str, Any]]:
    cols: dict[str, Any] = {"days": {}}
    for c, raw in row.items():
        h = " ".join(str(raw).split()).strip(" :#")
        if not h:
            continue
        if _MONEY.search(h):
            cols.setdefault("money", set()).add(c)
            continue
        day = _DAY_H.match(h)
        if _NAME_H.match(h):
            cols.setdefault("name", c)
        elif _OT_H.match(h):
            cols.setdefault("ot", []).append(c)
        elif _REG_H.match(h):
            cols.setdefault("reg", c)
        elif _TOTAL_H.match(h):
            cols.setdefault("total", c)
        elif _DEPT_H.match(h):
            cols.setdefault("dept", c)
        elif _RUN_H.match(h):
            cols.setdefault("run", c)
        elif _DATE_H.match(h):
            cols.setdefault("date", c)
        elif _INOUT_H.match(h):
            cols["inout"] = cols.get("inout", 0) + 1
        elif _DAY_MONTH.match(h):
            cols["days"][c] = ("", None)
        elif day:
            cols["days"][c] = (day.group(1).lower(), parse_date(day.group(2) or ""))
        else:
            parsed = parse_date(h)
            if parsed is not None and re.search(r"\d", h):
                cols["days"][c] = (_DAYS[parsed.weekday()], parsed)
    hours = "total" in cols or "reg" in cols or "ot" in cols or len(cols["days"]) >= 3
    if hours and "name" in cols:
        return cols
    # a person's own time card: rows are days (a date or day column, in/out times) and the
    # person is named by a label above the table
    card = ("date" in cols or cols.get("inout", 0) >= 2) and ("total" in cols or "reg" in cols)
    if card and "name" not in cols and not cols["days"]:
        cols["card"] = True
        return cols
    return None


def _card_person(grid: Mapping[int, Mapping[int, str]], upto: int) -> Optional[str]:
    for r in sorted(k for k in grid if k < upto):
        cells = [str(grid[r][c]) for c in sorted(grid[r])]
        for i, cell in enumerate(cells):
            label = _PERSON_LABEL.match(" ".join(cell.split()))
            if label:
                name = label.group(1).strip() or (cells[i + 1].strip() if i + 1 < len(cells)
                                                   else "")
                if re.search(r"[^\W\d_]{2,}", name) and "@" not in name:
                    return name
    return None


def _card_rows(grid: Mapping[int, Mapping[int, str]], header_at: int, cols: Mapping[str, Any],
               dept: Optional[str], sent: Optional[date]) -> SheetReading:
    """A time card: one person, a row per day; hours summed per week of the row dates."""
    name = _card_person(grid, header_at)
    if name is None:
        return SheetReading("no_table")
    weeks: dict[date, list[Decimal]] = {}
    ots: dict[date, Decimal] = {}
    for r in sorted(k for k in grid if k > header_at):
        row = grid[r]
        first = " ".join(str(row.get(min(row), "")).split()) if row else ""
        if _STOP_ROW.match(first):
            break
        day = parse_date(str(row.get(cols["date"], ""))) if "date" in cols else None
        if day is None:
            continue
        total = number(row.get(cols["total"], "")) if "total" in cols else None
        if total is None and "reg" in cols:
            total = number(row.get(cols["reg"], ""))
        if total is None:
            continue
        if not 0 <= total <= MAX_DAY_HOURS:
            return SheetReading("implausible")
        ot = sum(((number(row.get(c, "")) or Decimal(0)) for c in cols.get("ot", ())),
                 Decimal(0))
        if ot > total and "total" in cols:
            return SheetReading("implausible")
        weeks.setdefault(week_of(day), []).append(total + (ot if "total" not in cols else 0))
        ots[week_of(day)] = ots.get(week_of(day), Decimal(0)) + ot
    rows = []
    for week, hours in sorted(weeks.items()):
        total = sum(hours, Decimal(0))
        if total == 0:
            continue
        if total > MAX_WEEK_HOURS:
            return SheetReading("implausible")
        rows.append(PersonHours(name, dept or UNASSIGNED, week, "stated", total,
                                ots[week] if cols.get("ot") else None,
                                sum(1 for h in hours if h > 0), None))
    return SheetReading("parsed" if rows else "empty", rows)


def _context(grid: Mapping[int, Mapping[int, str]], upto: int) -> tuple[
        Optional[str], Optional[date]]:
    """A "Department:" and a week date stated above the header row (``upto``)."""
    dept = stated = None
    for r in sorted(k for k in grid if k < upto):
        cells = [grid[r][c] for c in sorted(grid[r])]
        for i, cell in enumerate(cells):
            text = " ".join(str(cell).split())
            nxt = cells[i + 1] if i + 1 < len(cells) else ""
            label = _DEPT_LABEL.match(text)
            if dept is None and (label or _DEPT_H.match(text.strip(" :"))):
                dept = department_in(label.group(1) if label else nxt)
            week = _WEEK_LABEL.search(text)
            if stated is None and week and not _MONEY.search(text):
                stated = parse_date(week.group(1)) or parse_date(nxt)
    return dept, stated


def sheet_rows(grid: Mapping[int, Mapping[int, str]], *, sheet: str, file_name: str,
               subject: str, sent: Optional[date]) -> SheetReading:
    """One sheet's per-person hours (module doc); money columns never read."""
    header_at = cols = None
    for r in sorted(grid)[:60]:
        cols = _header(grid[r])
        if cols is not None:
            header_at = r
            break
    if header_at is None or cols is None:
        return SheetReading("no_table")
    stated_dept, stated_week = _context(grid, header_at)
    days_with_dates = [d for _n, d in cols["days"].values() if d is not None]
    if stated_week is None and days_with_dates:
        stated_week = max(days_with_dates)
    if stated_week is not None:
        week, basis = week_of(stated_week), "stated"
    elif sent is not None:
        week, basis = week_of(sent - timedelta(days=3)), "sent"
    else:
        return SheetReading("no_week")
    sheet_dept = stated_dept or department_in(sheet, file_name, subject)
    rows: list[PersonHours] = []
    if cols.get("card"):
        return _card_rows(grid, header_at, cols, sheet_dept, sent)
    for r in sorted(k for k in grid if k > header_at):
        row = grid[r]
        name = " ".join(str(row.get(cols["name"], "")).split())
        if _STOP_ROW.match(name):
            break
        if not re.search(r"[^\W\d_]{2,}", name) or _MONEY.search(name):
            continue
        day_hours = {c: number(row.get(c, "")) for c in cols["days"]}
        if any(v is not None and not (0 <= v <= MAX_DAY_HOURS) for v in day_hours.values()):
            return SheetReading("implausible")
        reg = number(row.get(cols["reg"], "")) if "reg" in cols else None
        ots = [number(row.get(c, "")) for c in cols.get("ot", ())]
        ot = sum((v for v in ots if v is not None), Decimal(0)) if cols.get("ot") else None
        total = number(row.get(cols["total"], "")) if "total" in cols else None
        if total is None and reg is not None:
            total = reg + (ot or 0)
        if total is None and any(v is not None for v in day_hours.values()):
            total = sum((v for v in day_hours.values() if v is not None), Decimal(0))
        if total is None:
            continue
        if not (0 <= total <= MAX_WEEK_HOURS) or (ot is not None and not 0 <= ot <= total):
            return SheetReading("implausible")
        if total == 0:
            continue
        worked = sum(1 for v in day_hours.values() if v is not None and v > 0)
        row_week, row_basis, days = week, basis, worked if cols["days"] else None
        if "date" in cols:   # a daily log (Name | Date | Hours): the row's own date
            dated = parse_date(str(row.get(cols["date"], "")))
            if dated is not None:
                row_week, row_basis = week_of(dated), "stated"
                days = (worked if cols["days"] else 0) + (0 if cols["days"] else 1)
        dept = department_in(row.get(cols["dept"], "")) if "dept" in cols else None
        run = str(row.get(cols["run"], "")).strip() if "run" in cols else ""
        rows.append(PersonHours(name, dept or sheet_dept or UNASSIGNED, row_week, row_basis,
                                total, ot, days,
                                run if _RUN_VALUE.match(run) else None))
    return SheetReading("parsed" if rows else "empty", rows)


# ----------------------------------------------------------------------- exclusion

def _name_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", text.lower()) if t]


class ExclusionCheck:
    """The people exclusion for the build, fail closed (module doc). ``guard``: an
    ``integrations.people_exclusion.PeopleExclusion`` with the capture directory;
    ``directory``: its users ``(object id, upn, identities)``."""

    def __init__(self, guard: Any, directory: Optional[Sequence[tuple[Any, Any, Any]]]
                 ) -> None:
        if guard is None or getattr(guard, "unavailable", "missing") is not None \
                or not directory:
            raise LabourRefused("exclusion_unavailable")
        self.guard = guard
        forms: list[tuple[str, ...]] = []
        resolved: set[str] = set()
        for oid, upn, identities in directory:
            try:
                excluded = guard.is_excluded(str(oid))
            except Exception:  # noqa: BLE001 - undecidable: refuse everything
                raise LabourRefused("exclusion_unavailable")
            if not excluded:
                continue
            for address in {str(a).lower() for a in (upn, *(identities or ())) if a}:
                local = address.split("@", 1)[0]
                tokens = tuple(t for t in re.split(r"[._\-+]+", local) if t.isalpha())
                if any(len(t) >= 2 for t in tokens):
                    forms.append(tokens)
                    resolved.add(str(oid).strip().lower())
        # every excluded id must yield a name form (distinct ids, not directory entries)
        if len(resolved) < guard.count or not forms:
            raise LabourRefused("exclusion_unresolved")   # an excluded id with no address
        self.forms = forms

    def message_cleared(self, mailbox: Any, addresses: Iterable[Any]) -> bool:
        """Whether a message's mailbox and participants are all decided and not excluded
        (an outside address is no employee's)."""
        domains = self.guard.known_domains()
        try:
            if mailbox and self.guard.is_excluded(str(mailbox)):
                return False
            for value in addresses:
                v = str(value or "").strip().lower()
                if not v:
                    continue
                if not self.guard.decides(v):
                    if "@" in v and v.rsplit("@", 1)[1] not in domains:
                        continue
                    return False
                if self.guard.is_excluded(v):
                    return False
        except Exception:  # noqa: BLE001 - undecidable: withheld
            return False
        return True

    def names_excluded(self, name: str) -> bool:
        """Whether a row's name could be an excluded person's, over-inclusive (fail
        closed): any word of the row (two letters or more) equal to any word of an excluded
        person's address form or contained in a one-word form (``jsmith`` holds
        ``smith``), or the row's joined / initial forms (``johnsmith``, ``jsmith``,
        ``johns``) equal to a one-word form. A first name alone matches, so an address that
        is only a first name still drops every sheet naming anyone of that first name."""
        tokens = [t for t in _name_tokens(name) if t]
        words = {t for t in tokens if len(t) >= 2}
        if not words:
            return False
        joined = {a + b for a in tokens for b in tokens if a != b}
        initials = {a[0] + b for a in tokens for b in tokens if a != b} | \
                   {a + b[0] for a in tokens for b in tokens if a != b}
        for form in self.forms:
            parts = {t for t in form if len(t) >= 2}
            if words & parts:
                return True
            if len(form) == 1 and (form[0] in joined or form[0] in initials or
                                   any(len(w) >= 3 and w in form[0] for w in words)):
                return True
        return False


# ----------------------------------------------------------------------- aggregation

@dataclass(frozen=True)
class Source:
    """One sheet a figure is read from: the captured attachment (or message) handle and
    the message's sent date. No file name (a time sheet's name may be a person's)."""
    handle: str
    sent: str


@dataclass
class Sheet:
    """One time sheet found in the capture, as the build reads it."""
    handle: str
    sent: Optional[datetime]
    text: Optional[str]
    file_name: str = ""
    subject: str = ""
    mailbox: Any = None
    participants: tuple[Any, ...] = ()


def _summed(a: PersonHours, b: PersonHours) -> PersonHours:
    """Two rows of one person, week and run on one sheet, as one."""
    return PersonHours(a.name, a.department, a.week,
                       a.week_basis if a.week_basis == b.week_basis else "sent",
                       a.hours + b.hours,
                       None if a.overtime is None or b.overtime is None
                       else a.overtime + b.overtime,
                       None if a.days is None or b.days is None else a.days + b.days, a.run)


def _group() -> dict[str, Any]:
    return {"people": set(), "hours": Decimal(0), "overtime": Decimal(0), "ot_known": True,
            "ot_by": {},
            "ot_basis": set(), "days": 0, "days_known": True, "sources": set(),
            "basis": set()}


def aggregate(sheets: Iterable[Sheet], exclusion: ExclusionCheck) -> dict[str, Any]:
    """The department-week aggregates of ``sheets`` (module doc) and the build's coverage
    counts. Per-person rows exist only inside this call."""
    key = secrets.token_bytes(32)
    counts = {"found": 0, "parsed": 0, "withheld_participants": 0, "withheld_excluded": 0,
              "no_text": 0, "no_table": 0, "no_week": 0, "implausible": 0, "empty": 0,
              "duplicate_text": 0}
    seen: set[str] = set()
    withheld: set[str] = set()
    latest: dict[tuple, tuple[str, PersonHours, Source]] = {}
    for sheet in sheets:
        # one sheet mailed to several mailboxes (or again) is one sheet found
        digest = hashlib.sha256((sheet.text or sheet.handle).encode()).hexdigest()
        if digest in seen:
            counts["duplicate_text"] += 1
            continue
        seen.add(digest)
        counts["found"] += 1
        if not exclusion.message_cleared(sheet.mailbox, sheet.participants):
            # another copy of it whose participants all clear may still be read
            withheld.add(digest)
            seen.discard(digest)
            counts["found"] -= 1
            continue
        withheld.discard(digest)
        if not sheet.text:
            counts["no_text"] += 1
            continue
        sent = sheet.sent.date() if sheet.sent else None
        stamp = sheet.sent.isoformat() if sheet.sent else ""
        source = Source(sheet.handle, stamp[:10])
        readings = [sheet_rows(grid, sheet=name, file_name=sheet.file_name,
                               subject=sheet.subject, sent=sent)
                    for name, grid in read_grid(sheet.text)]
        rows = [p for r in readings for p in r.rows]
        if any(exclusion.names_excluded(p.name) for p in rows):
            counts["withheld_excluded"] += 1
            continue
        if not rows:
            status = next((r.status for r in readings if r.status == "implausible"),
                          readings[0].status if readings else "no_table")
            counts[status] = counts.get(status, 0) + 1
            continue
        # One person's rows on one sheet (a daily log, split lines) are summed; a later
        # sheet replaces an earlier one for that person and week (a revised sheet).
        mine: dict[tuple, PersonHours] = {}
        for p in rows:
            person = hmac.new(key, " ".join(sorted(_name_tokens(p.name))).encode(),
                              hashlib.sha256).hexdigest()
            k = (p.department, p.week, p.run, person)
            mine[k] = p if k not in mine else _summed(mine[k], p)
        if any(p.hours > MAX_WEEK_HOURS for p in mine.values()):
            counts["implausible"] += 1
            continue
        counts["parsed"] += 1
        for k, p in mine.items():
            if k not in latest or latest[k][0] <= stamp:
                latest[k] = (stamp, p, source)
    counts["withheld_participants"] = len(withheld)
    counts["found"] += len(withheld)
    # A person's week with no overtime column: the hours over OVERTIME_AFTER that week
    # (basis over_44_hours), on the department's whole week only, never on a run.
    week_hours: dict[tuple, list[PersonHours]] = {}
    for (dept, week, _run, person), (_s, p, _src) in latest.items():
        week_hours.setdefault((dept, week, person), []).append(p)
    derived: dict[tuple, Optional[Decimal]] = {}
    for k, ps in week_hours.items():
        if all(p.overtime is None for p in ps):
            derived[k] = max(Decimal(0), sum((p.hours for p in ps), Decimal(0)) -
                             OVERTIME_AFTER)
    groups: dict[tuple, dict[str, Any]] = {}
    for (dept, week, person), extra in derived.items():
        g = groups.setdefault((dept, week, None), _group())
        g["overtime"] += extra
        g["ot_by"][person] = g["ot_by"].get(person, Decimal(0)) + extra
        g["ot_basis"].add("over_44_hours")
    for (dept, week, run, person), (_s, p, source) in latest.items():
        for scope in ((dept, week, None), (dept, week, run)) if run else ((dept, week, None),):
            g = groups.setdefault(scope, _group())
            g["people"].add(person)
            g["hours"] += p.hours
            if p.overtime is None:
                if scope[2] is not None or (dept, week, person) not in derived:
                    g["ot_known"] = False
            else:
                g["overtime"] += p.overtime
                g["ot_by"][person] = g["ot_by"].get(person, Decimal(0)) + p.overtime
                g["ot_basis"].add("stated")
            if p.days is None:
                g["days_known"] = False
            else:
                g["days"] += p.days
            g["sources"].add(source)
            g["basis"].add(p.week_basis)
    weeks, suppressed = [], 0
    for (dept, week, run), g in sorted(groups.items(), key=lambda i: (i[0][0], i[0][1],
                                                                       i[0][2] or "")):
        if len(g["people"]) < MIN_GROUP:
            suppressed += 1
            continue
        if run is not None:
            # Published: the whole week D and every run R_i. A difference of them is the
            # hours outside some set S of runs: for S short of every run that holds a whole
            # other run (MIN_GROUP people or more); for S = every run it is the off-run
            # hours. So each run and the off-run set (anyone with any row on no run) must
            # each be empty or MIN_GROUP people, else no run of that week is shown.
            whole = groups[(dept, week, None)]
            runs = [v for (d, w, rn), v in groups.items() if d == dept and w == week and rn]
            off = {pn for (d, w, rn, pn) in latest if d == dept and w == week and rn is None}
            if any(len(v["people"]) < MIN_GROUP for v in runs) or 0 < len(off) < MIN_GROUP \
                    or len(whole["people"]) < MIN_GROUP:
                suppressed += 1
                continue
        # overtime only when MIN_GROUP people or more worked some: else it is one or two
        # people's overtime (and zero would say each person's is zero)
        # people's overtime (and zero would say each person's is zero). Never on a run row:
        # department overtime minus a run's would be the off-run people's overtime.
        ot_shown = run is None and g["ot_known"] and \
            sum(1 for v in g["ot_by"].values() if v > 0) >= MIN_GROUP
        weeks.append({
            "department": dept, "week": week.isoformat(), "run": run,
            "hours": str(g["hours"].quantize(Decimal("0.01"))),
            "overtime_hours": str(g["overtime"].quantize(Decimal("0.01")))
            if ot_shown else None,
            "overtime_basis": (None if not g["ot_known"] or run is not None
                               else "fewer_than_3" if not ot_shown
                               else "mixed"
                               if len(g["ot_basis"]) > 1 else next(iter(g["ot_basis"]),
                                                                   "stated")),
            "headcount_days": g["days"] if g["days_known"] else None,
            "week_basis": "stated" if g["basis"] == {"stated"} else "sent",
            "markings": [MARKING],
            "sources": [{"handle": s.handle, "sent": s.sent}
                        for s in sorted(g["sources"], key=lambda s: (s.sent, s.handle))],
        })
    counts["groups_suppressed"] = suppressed
    return {"weeks": weeks, "coverage": counts}


# ----------------------------------------------------------------------- store

_AGGREGATE_KEYS = {"department", "week", "run", "hours", "overtime_hours", "overtime_basis",
                   "headcount_days",
                   "week_basis", "markings", "sources"}


class LabourStore:
    """The aggregates only (module doc): a row with any other field (a person, a name) is
    refused, so nothing per person is ever written."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        if read_only:
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(self.path, timeout=5)
            _schemas.apply(self.db, _LABOUR_HOURS)

    def replace(self, built: Mapping[str, Any], now: Optional[datetime] = None) -> None:
        for row in built["weeks"]:
            if set(row) != _AGGREGATE_KEYS or row["markings"] != [MARKING]:
                raise LabourRefused("not_an_aggregate")
        with self.db:
            self.db.execute("DELETE FROM labour_week")
            self.db.execute("DELETE FROM labour_build")
            self.db.executemany(
                "INSERT INTO labour_week VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(r["department"], r["week"], r["run"] or "", r["hours"],
                  r["overtime_hours"], r["overtime_basis"], r["headcount_days"], r["week_basis"],
                  json.dumps(r["markings"]), json.dumps(r["sources"]))
                 for r in built["weeks"]])
            self.db.execute("INSERT INTO labour_build VALUES (?,?)",
                            ((now or datetime.now(timezone.utc)).isoformat(),
                             json.dumps(built["coverage"], sort_keys=True)))

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.execute("SELECT department, week, run, hours, overtime_hours, "
                                 "overtime_basis, headcount_days, week_basis, markings, "
                                 "sources "
                                 "FROM labour_week ORDER BY department, week, run"):
            out.append({"department": r[0], "week": r[1], "run": r[2] or None, "hours": r[3],
                        "overtime_hours": r[4], "overtime_basis": r[5],
                        "headcount_days": r[6], "week_basis": r[7],
                        "markings": json.loads(r[8]), "sources": json.loads(r[9])})
        return out

    def built(self) -> Optional[dict[str, Any]]:
        row = self.db.execute("SELECT built_at, coverage FROM labour_build").fetchone()
        return None if row is None else {"built_at": row[0], "coverage": json.loads(row[1])}


# ----------------------------------------------------------------------- access

_PRINCIPAL = re.compile(r"[A-Za-z0-9_.-]+:\S+")


def load_grants(path: Optional[str | Path] = None,
                environ: Optional[Mapping[str, str]] = None) -> dict[str, frozenset[str]]:
    """The host's labour grants (``{"hr": [principal...], "ops-management": [...]}``) from
    ``path`` or ``$PLX_LABOUR_GRANTS_FILE``. Absent, unreadable, an unknown grant, a
    wildcard or a malformed principal: nobody holds any (fail closed)."""
    environ = os.environ if environ is None else environ
    named = path if path is not None else environ.get(GRANTS_FILE_ENV)
    if not named:
        return {}
    try:
        raw = json.loads(Path(named).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, frozenset[str]] = {}
    for grant, principals in raw.items():
        if grant not in GRANTS or not isinstance(principals, list) or any(
                not isinstance(p, str) or "*" in p or not _PRINCIPAL.fullmatch(p)
                for p in principals):
            return {}
        out[grant] = frozenset(principals)
    return out


def holds_labour(principals: Iterable[str], grants: Mapping[str, frozenset[str]]) -> bool:
    """Whether a reader with ``principals`` holds ``hr`` or ``ops-management``."""
    mine = {str(p) for p in principals or ()}
    return any(mine & set(grants.get(g, ())) for g in GRANTS)


# ----------------------------------------------------------------------- page, alert, ask

def _median(values: Sequence[Decimal]) -> Decimal:
    return Decimal(statistics.median(values))


def overtime_spikes(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A department whose latest week's overtime is over ``SPIKE_RATIO`` times the median
    of its ``BASELINE_WEEKS`` weeks before (at least ``MIN_BASELINE`` of them with overtime
    read, a median above zero). ``unassigned`` is never judged."""
    by_dept: dict[str, list[Mapping[str, Any]]] = {}
    newest: dict[str, str] = {}
    for r in rows:
        if r.get("run") or r["department"] == UNASSIGNED:
            continue
        newest[r["department"]] = max(newest.get(r["department"], ""), r["week"])
        if r.get("overtime_hours") is None:
            continue   # unknown, or fewer than MIN_GROUP people worked overtime
        by_dept.setdefault(r["department"], []).append(r)
    alerts = []
    for dept, series in sorted(by_dept.items()):
        series = sorted(series, key=lambda r: r["week"])
        last = series[-1]
        if last["week"] != newest[dept]:
            continue   # the latest week's overtime is not shown: never judge an older one
        start = (date.fromisoformat(last["week"]) - timedelta(weeks=BASELINE_WEEKS)).isoformat()
        prior = [Decimal(r["overtime_hours"]) for r in series[:-1] if r["week"] >= start]
        if len(prior) < MIN_BASELINE:
            continue
        median = _median(prior)
        now = Decimal(last["overtime_hours"])
        if median > 0 and now > median * SPIKE_RATIO:
            alerts.append({
                "kind": "labour_overtime_spike", "department": dept, "week": last["week"],
                "overtime_hours": str(now), "baseline_median": str(median.quantize(
                    Decimal("0.01"))), "baseline_weeks": len(prior),
                "rise_percent": str(((now / median - 1) * 100).quantize(Decimal("1"))),
                "markings": [MARKING], "sources": last["sources"]})
    return alerts


def labour_page(store: Optional[LabourStore], held: bool) -> dict[str, Any]:
    """The Labour page for a reader: nothing at all without ``hr`` / ``ops-management``."""
    if not held:
        return {"state": "not_granted", "weeks": [], "trend": [], "alerts": [],
                "built_at": None, "note": "Labour figures need the hr or ops-management "
                "grant."}
    if store is None:
        return {"state": "not_built", "weeks": [], "trend": [], "alerts": [],
                "built_at": None, "note": "No labour hours have been read yet."}
    rows = store.rows()
    built = store.built() or {}
    trend: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["run"] is None:
            trend.setdefault(r["department"], []).append(
                {"week": r["week"], "hours": r["hours"], "overtime_hours": r["overtime_hours"]})
    return {"state": "ready", "weeks": rows, "alerts": overtime_spikes(rows),
            "trend": [{"department": d, "points": pts} for d, pts in sorted(trend.items())],
            "built_at": built.get("built_at"),
            "note": f"Groups of fewer than {MIN_GROUP} people are not shown."}


_ASK = re.compile(r"\b(?:labou?r(?:\s+hours)?|overtime|man[\s-]?hours|hours\s+worked|"
                  r"time\s*sheets?|headcount[\s-]?days?)\b", re.I)


def serves(question: Any) -> bool:
    """Whether a question is a labour-hours question (answered here, never by the model)."""
    return isinstance(question, str) and bool(_ASK.search(question))


def answer(question: str, store: Optional[LabourStore], held: bool) -> dict[str, Any]:
    """A labour question answered from the aggregates alone: the named department's (or
    every department's) latest week, its overtime against its 8-week median and the
    spikes. No per-person figure exists to say; nothing goes to a model."""
    if not held:
        return {"outcome": "refused", "text": "Labour hours are shown only to readers "
                "granted hr or ops-management.", "sources": []}
    page = labour_page(store, True)
    if page["state"] != "ready" or not page["trend"]:
        return {"outcome": "abstained", "text": page["note"], "sources": []}
    dept = department_in(question)
    lines, sources = [], []
    latest = {r["department"]: r for r in page["weeks"] if r["run"] is None}
    for d in sorted(latest):
        if dept and d != dept:
            continue
        r = latest[d]
        ot = f", {r['overtime_hours']} overtime hours" if r["overtime_hours"] else ""
        days = (f", {r['headcount_days']} headcount-days" if r["headcount_days"] is not None
                else "")
        lines.append(f"{d.capitalize()}, week of {r['week']}: {r['hours']} hours{ot}{days}.")
        sources.extend(r["sources"][:3])
    for a in page["alerts"]:
        if dept and a["department"] != dept:
            continue
        lines.append(f"Overtime spike: {a['department']} {a['overtime_hours']} hours in the "
                     f"week of {a['week']}, {a['rise_percent']}% over its "
                     f"{a['baseline_weeks']}-week median of {a['baseline_median']}.")
    if not lines:
        return {"outcome": "abstained", "text": "No labour figures are shown for that "
                "department.", "sources": []}
    lines.append(page["note"])
    return {"outcome": "answered", "text": " ".join(lines), "sources": sources}


def store_path(state_dir: Optional[str | Path]) -> Optional[Path]:
    if state_dir is None:
        return None
    path = Path(state_dir) / STORE_NAME
    return path if path.is_file() else None


def open_store(state_dir: Optional[str | Path]) -> Optional[LabourStore]:
    path = store_path(state_dir)
    if path is None:
        return None
    try:
        return LabourStore(path, read_only=True)
    except sqlite3.Error:
        return None


# ----------------------------------------------------------------------- capture scan

def _addresses(value: Any) -> list[str]:
    out = []
    for item in value or () if isinstance(value, list) else [value]:
        if isinstance(item, dict):
            item = (item.get("emailAddress") or item).get("address")
        if isinstance(item, str) and "@" in item:
            out.append(item)
    return out


def capture_sheets(capture: Any) -> Iterator[Sheet]:
    """Every time sheet in a mail capture (``integrations.capture_mail.MailCaptureStore``,
    opened read-only): attachments named as one with captured text, and bodies of messages
    whose subject names one. Read-only."""
    from integrations.capture_mail import undecided_free_sql
    db = capture.db
    cur = db.execute("SELECT a.l1_handle, a.text_sha256, json_extract(a.metadata,'$.name'), "
                     "m.record, m.mailbox_id FROM attachments a JOIN messages m ON "
                     "m.l1_handle = a.message_handle AND m.superseded_at IS NULL "
                     f"WHERE a.content_status='captured' AND {undecided_free_sql(db, 'm')}")
    with_attachment: set[str] = set()
    for handle, text_digest, name, record, mailbox in cur:
        if not isinstance(name, str) or not TIME_SHEET.search(name):
            continue
        rec = json.loads(record)
        with_attachment.add(rec.get("internet_message_id") or "")
        text = None
        if text_digest:
            try:
                text = capture.blob(text_digest).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - a dropped text: counted as no_text
                text = None
        yield _sheet(handle, rec, mailbox, text, name)
    for handle, record, mailbox, body in db.execute(
            "SELECT l1_handle, record, mailbox_id, body_sha256 FROM messages WHERE "
            f"superseded_at IS NULL AND body_sha256 IS NOT NULL AND {undecided_free_sql(db)} "
            "AND (lower(json_extract("
            "record, '$.subject')) LIKE '%time%' OR lower(json_extract(record, '$.subject'))"
            " LIKE '%hours%')"):
        rec = json.loads(record)
        if not TIME_SHEET.search(str(rec.get("subject") or "")) or \
                (rec.get("internet_message_id") or "") in with_attachment:
            continue
        try:
            text = capture.blob(body).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a dropped body: counted as no_text
            text = None
        with_attachment.add(rec.get("internet_message_id") or handle)
        yield _sheet(handle, rec, mailbox, text, "")


def _sheet(handle: str, rec: Mapping[str, Any], mailbox: Any, text: Optional[str],
           name: str) -> Sheet:
    sent = None
    try:
        sent = datetime.fromisoformat(str(rec.get("sent_at") or "").replace("Z", "+00:00"))
    except ValueError:
        sent = None
    people = tuple(_addresses(rec.get("from")) + _addresses(rec.get("to")) +
                   _addresses(rec.get("cc")) + _addresses(rec.get("bcc")) +
                   [rec.get("mailbox_upn")])
    return Sheet(handle, sent, text, name or "", str(rec.get("subject") or ""), mailbox,
                 people)


ADDRESSES_ENV = "PLX_EXCLUDED_PEOPLE_ADDRESSES_FILE"


def excluded_addresses(environ: Mapping[str, str]) -> list[tuple[str, None, list[str]]]:
    """The excluded people's addresses the owner gives (``$PLX_EXCLUDED_PEOPLE_ADDRESSES_
    FILE``: ``{"<object id>": ["first.last@domain", ...]}``), for an excluded person whose
    mailbox the capture does not hold (so their name forms can be checked on time-sheet
    rows). Only a regular file of this user that nobody else can read; anything else:
    none (the build then refuses when an excluded person stays unresolved)."""
    import stat
    named = environ.get(ADDRESSES_ENV)
    if not named:
        return []
    try:
        path = Path(named).expanduser()
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or \
                info.st_mode & 0o077:
            return []
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    return [(str(oid), None, [a for a in addresses if isinstance(a, str) and "@" in a])
            for oid, addresses in raw.items() if isinstance(addresses, list)]


def build(state_dir: str | Path, out: str | Path,
          environ: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Read every time sheet of the capture under ``state_dir`` (read-only) and write the
    aggregates to ``out``. Refused (nothing written) without the people exclusion."""
    from integrations import people_exclusion as pe
    from integrations.capture_mail import MailCaptureStore
    environ = pe.environ_with_exclusion(environ)
    guard = pe.PeopleExclusion.from_env(environ)
    directory = pe.mail_capture_directory(state_dir)
    if directory is not None and guard.unavailable is None:
        guard = pe.with_directory(guard, state_dir)
    check = ExclusionCheck(guard, (directory or []) + excluded_addresses(environ))
    capture = MailCaptureStore.open_read_only(state_dir)
    built = aggregate(capture_sheets(capture), check)
    LabourStore(out).replace(built)
    return built["coverage"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        coverage = build(args.state_dir, args.out)
    except LabourRefused as error:
        print(json.dumps({"refused": error.code}))
        return 2
    print(json.dumps(coverage, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
