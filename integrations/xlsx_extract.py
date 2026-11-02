"""Bounded, read-only extraction of OOXML workbook evidence, sheet by sheet.

Cells and formulas are transcribed, never evaluated. Adjacent label/value
renderings are heuristic review leads; the original workbook remains the
authority and can be opened through its inherited SharePoint/Outlook link.

A workbook is read per sheet, never all-or-nothing (coverage audit 2026-10-30: 1,215
large workbooks had failed whole on one bound). Each sheet's XML is streamed
(``iterparse``) and read up to its bounds: ``MAX_ROWS_PER_SHEET`` populated rows,
``MAX_CELLS_PER_SHEET`` populated cells, ``MAX_SHEET_BYTES`` of XML and
``MAX_ELEMENTS_PER_SHEET`` elements; all sheets together ``MAX_ELEMENTS_PER_WORKBOOK``
elements and, as a wall-clock backstop, ``MAX_SECONDS`` (the only bound that is not a
pure function of the bytes; it ends a single file's extraction on a stalled machine). The text
bound (``max_text``) is shared fairly between the sheets (water-filling: a sheet
needing less than an equal share keeps all of it, the rest is split between the
larger ones), so one big sheet never crowds out the others. A sheet cut by any of
these bounds ends with a visible marker line::

    [Sheet truncated: <name>; rows shown: <n>, last row <r>; not read: <reason>]

and the reading carries the warning ``xlsx_rows_truncated``. A sheet whose part is
missing, damaged or not well-formed is a marker ``[Sheet not read: <name>; <reason>]``
(warning ``xlsx_sheet_unreadable``), its rows never half-shown; sheets past
``MAX_SHEETS`` are named in one ``[Sheets not read: ...]`` line (warning
``xlsx_sheets_not_read``). A cell over ``MAX_CELL_CHARS`` is cut with a marker. So a
partial workbook is never silent: what was not read is named in the text itself.

Hard failures stay where the workbook cannot be read safely at all: over
``MAX_MEMBERS`` ZIP members, a workbook/relationship part over ``MAX_PART_BYTES``,
shared strings over their bounds (every sheet indexes them), a DTD in any part, or a
text bound too small for the sheets' headings and markers. The output is a pure
function of the bytes and the bounds: deterministic.
"""

from __future__ import annotations

from io import BytesIO
import posixpath
import re
import time
from typing import Any, Iterator
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from ontology.core import ValidationError
from .office_text import ZIP_ERRORS, encrypted_ooxml


MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_PART_BYTES = 5_000_000          # workbook.xml and its relationships, read whole
MAX_MEMBERS = 2_000
MAX_SHEET_ENTRIES = 1_000           # sheets a workbook may declare at all
MAX_SHEETS = 40                     # sheets read; the rest are named, not read
# Decompressed XML streamed: a few KB of ZIP can expand to gigabytes, so every bound is
# on bytes read and elements parsed, never on what the archive declares.
MAX_SHEET_BYTES = 16_000_000        # XML streamed per sheet
MAX_READ_BYTES = 64_000_000         # XML streamed per workbook (all sheets)
MAX_SHARED_BYTES = 32_000_000       # shared strings XML streamed
MAX_SHARED_STRINGS = 300_000
MAX_SHARED_CHARS = 20_000_000
# XML elements parsed per sheet (cells, values, formulas, anything else): what bounds a
# sheet's parse time and memory whatever its shape (millions of empty cells in one row,
# deep nesting); each cell is dropped from the tree as soon as it is read.
MAX_ELEMENTS_PER_SHEET = 400_000
# Elements parsed per workbook, all sheets together (a few seconds of parsing), and wall
# time as a backstop: a single file's extraction always ends, with a marker.
MAX_ELEMENTS_PER_WORKBOOK = 2_000_000
MAX_SECONDS = 30.0
_WORK_STEP = 1_024                  # elements between checks of the workbook's work
_clock = time.monotonic
MAX_CELLS_PER_ROW = 16_384          # Excel's own column limit
MAX_ROWS_PER_SHEET = 5_000          # populated rows per sheet
MAX_CELLS_PER_SHEET = 50_000        # populated cells per sheet
MAX_CELL_CHARS = 2_000
CELL = re.compile(r"^([A-Z]{1,4})([1-9][0-9]{0,6})$")
CURRENCY = {"USD", "CAD", "EUR", "GBP"}
_CHUNK = 64 * 1024
_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
# The longest marker of each kind is reserved in the text bound up front.
_TRUNCATED = "[Sheet truncated: {name}; rows shown: {shown}, last row {last}; not read: {why}]"
_UNREAD = "[Sheet not read: {name}; {why}]"
_REASONS = {"rows": f"rows past the row bound {MAX_ROWS_PER_SHEET}",
            "cells": f"cells past the cell bound {MAX_CELLS_PER_SHEET}",
            "row_cells": f"cells past the row cell bound {MAX_CELLS_PER_ROW}",
            "workbook_elements": f"past the workbook element bound {MAX_ELEMENTS_PER_WORKBOOK}",
            "time": f"past the extraction time bound {MAX_SECONDS:g} s",
            "bytes": f"sheet XML past the byte bound {MAX_SHEET_BYTES}",
            "elements": f"sheet XML past the element bound {MAX_ELEMENTS_PER_SHEET}",
            "records": "records past the workbook record bound",
            "workbook_bytes": f"workbook XML past the byte bound {MAX_READ_BYTES}",
            "text": "rows past the workbook text bound"}
_UNREAD_REASONS = {"missing": "sheet part missing", "damaged": "sheet part damaged",
                   "invalid": "sheet XML not well-formed",
                   "chart": "chart, macro or dialog sheet: no cells",
                   "records": "past the workbook record bound",
                   "workbook_elements": "past the workbook element bound "
                                        f"{MAX_ELEMENTS_PER_WORKBOOK}",
                   "time": f"past the extraction time bound {MAX_SECONDS:g} s",
                   "workbook_bytes": f"workbook XML past the byte bound {MAX_READ_BYTES}"}
_MAX_REASON = max(len(r) for r in _REASONS.values())
_MAX_NAMES = 400                    # characters of sheet names in the not-read line


def _column(letters: str) -> int:
    result = 0
    for character in letters:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _letters(number: int) -> str:
    out = ""
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(ord("A") + rest) + out
    return out


class _DamagedMember(Exception):
    """A ZIP member that cannot be read (bad CRC or header, unsupported method)."""


class _BadSheet(Exception):
    """A sheet whose content is not a valid transcription source (bad reference)."""


class _Bounded:
    """A read-only stream over a ZIP member that stops after ``limit`` bytes (``cut``
    set) and refuses a DTD or entity declaration anywhere in it."""

    def __init__(self, stream: Any, limit: int) -> None:
        self.stream, self.limit, self.read_bytes, self.cut = stream, limit, 0, False
        self._tail = b""

    def read(self, size: int = -1) -> bytes:
        room = self.limit - self.read_bytes
        if room <= 0:
            return b""
        want = _CHUNK if size is None or size < 0 else min(size, _CHUNK)
        try:
            chunk = self.stream.read(min(want, room))
        except ZIP_ERRORS as error:
            raise _DamagedMember() from error
        if not chunk:
            return b""
        self.read_bytes += len(chunk)
        window = (self._tail + chunk).upper()
        if any(marker in window for marker in _FORBIDDEN):
            raise ValidationError("workbook XML DTD not permitted")
        self._tail = window[-16:]
        if self.read_bytes >= self.limit:
            # One byte more tells a part that ends exactly at the bound from a longer one.
            try:
                more = self.stream.read(1)
            except ZIP_ERRORS as error:
                raise _DamagedMember() from error
            self.cut = bool(more)
        return chunk


def _part(archive: ZipFile, name: str) -> ET.Element:
    info = archive.getinfo(name)
    if info.file_size > MAX_PART_BYTES:
        raise ValidationError("workbook XML part exceeds bound")
    try:
        with archive.open(info) as stream:
            raw = stream.read(MAX_PART_BYTES + 1)
    except ZIP_ERRORS as error:
        raise _DamagedMember(name) from error
    if len(raw) > MAX_PART_BYTES:
        raise ValidationError("workbook XML part exceeds bound")
    if any(marker in raw.upper() for marker in _FORBIDDEN):
        raise ValidationError("workbook XML DTD not permitted")
    return ET.fromstring(raw)


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(part.text or "" for part in node.iter(MAIN + "t"))


def _shared_strings(archive: ZipFile, name: str) -> list[str]:
    """The shared strings, streamed; each kept to ``MAX_CELL_CHARS`` + 1 characters (a
    longer one is cut where a cell shows it)."""
    shared: list[str] = []
    total = 0
    stream: _Bounded | None = None
    try:
        with archive.open(archive.getinfo(name)) as raw:
            stream = _Bounded(raw, MAX_SHARED_BYTES)
            root: ET.Element | None = None
            elements = 0
            for event, node in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    root = node if root is None else root
                    elements += 1
                    if elements > 4 * MAX_SHARED_STRINGS + 16:
                        raise ValidationError("workbook shared strings exceed bound")
                    continue
                if node.tag != MAIN + "si":
                    continue
                value = _text(node)[:MAX_CELL_CHARS + 1]
                if root is not None:
                    root.clear()   # the strings read are kept as text, not as elements
                total += len(value)
                shared.append(value)
                if len(shared) > MAX_SHARED_STRINGS:
                    raise ValidationError("workbook shared strings exceed bound")
                if total > MAX_SHARED_CHARS:
                    raise ValidationError("workbook shared strings exceed bound")
    except ET.ParseError:
        if stream is not None and stream.cut:   # cut by the byte bound: indexes past it cannot be resolved
            raise ValidationError("workbook shared strings exceed bound") from None
        raise
    except ValidationError:
        raise
    except ZIP_ERRORS as error:
        raise _DamagedMember(name) from error
    return shared


def _cut(value: str) -> str:
    if len(value) <= MAX_CELL_CHARS:
        return value
    return value[:MAX_CELL_CHARS] + f"...[cell cut at {MAX_CELL_CHARS} characters]"


def _cell_value(cell: ET.Element, shared: list[str]) -> tuple[str, str | None]:
    formula_node = cell.find(MAIN + "f")
    formula = formula_node.text if formula_node is not None else None
    value_node = cell.find(MAIN + "v")
    kind = cell.get("t")
    if kind == "inlineStr":
        value = _text(cell.find(MAIN + "is"))
    elif kind == "s" and value_node is not None:
        try:
            index = int(value_node.text or "")
        except ValueError as error:
            raise _BadSheet("shared string index invalid") from error
        if not 0 <= index < len(shared):
            raise _BadSheet("shared string index invalid")
        value = shared[index]
    else:
        value = value_node.text or "" if value_node is not None else ""
    # ISO dates are explicit strings; numeric Excel serials are left numeric.
    return _cut(value), (None if formula is None else _cut(formula))


def _label_line(cells: list[tuple[str, str, str | None]], sheet: str) -> str | None:
    """Render only a nearby label/value pair, always for human review."""
    if len(cells) not in {2, 3} or any(formula is not None for _, _, formula in cells):
        return None
    if any(_column(cells[index + 1][0].rstrip("0123456789")) !=
           _column(cells[index][0].rstrip("0123456789")) + 1
           for index in range(len(cells) - 1)):
        return None
    label = cells[0][1].strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 /()._-]{1,65}", label):
        return None
    if len(cells) == 2:
        value = cells[1][1].strip()
    elif cells[1][1].strip().upper() in CURRENCY:
        value = cells[1][1].strip().upper() + " " + cells[2][1].strip()
    else:
        return None
    if not value or len(value) > 150:
        return None
    coordinates = ",".join(cell[0] for cell in cells)
    return f"{label}: {value} [Sheet {sheet}; cells {coordinates}]"


class _Sheet:
    """One sheet as read: its row units (``(row, ((line, review), ...))``), why its
    reading stopped early (``_REASONS`` key) or why it was not read at all."""

    def __init__(self, name: str) -> None:
        self.name = name
        # (row number, ((line, review), ...), has a formula, has an interpreted line)
        self.units: list[tuple[int, tuple[tuple[str, bool], ...], bool, bool]] = []
        self.demand = 0
        self.cells = 0
        self.cut: str | None = None
        self.unread: str | None = None

    def heading(self) -> str:
        return f"[Sheet: {self.name}]"

    def reserve(self) -> int:
        """Characters the sheet needs whatever its share: its heading and its longest
        marker (a truncation marker is never shorter than a not-read one)."""
        marker = _TRUNCATED.format(name=self.name, shown=MAX_ROWS_PER_SHEET,
                                   last=9_999_999, why="x" * _MAX_REASON)
        return len(self.heading()) + 1 + len(marker) + 1

    def add(self, number: int, cells: list[tuple[str, str, str | None]], budget: int
            ) -> bool:
        """Add one populated row (``(address, value, formula)`` cells, in column order);
        False, with :attr:`cut` set, when a bound stops the sheet before it."""
        if len(self.units) >= MAX_ROWS_PER_SHEET:
            self.cut = "rows"
        elif self.cells + len(cells) > MAX_CELLS_PER_SHEET:
            self.cut = "cells"
        elif self.demand >= budget:   # no share is ever larger than the budget
            self.cut = "text"
        if self.cut is not None:
            return False
        self.cells += len(cells)
        pieces = []
        for address, value, formula in cells:
            if formula is not None:
                pieces.append(f"{address}=FORMULA({formula}); cached={value or '(missing)'}")
            else:
                pieces.append(f"{address}={value}")
        has_formula = any(formula is not None for _, _, formula in cells)
        lines = [(" | ".join(pieces), has_formula)]
        interpreted = _label_line(cells, self.name)
        if interpreted:
            lines.append((interpreted, True))
        self.units.append((number, tuple(lines), has_formula, bool(interpreted)))
        self.demand += sum(len(line) + 1 for line, _ in lines)
        return True

    def drop(self, why: str) -> None:
        """The sheet not read at all (``_UNREAD_REASONS`` key): no row of it shown."""
        self.units, self.demand, self.unread = [], 0, why


class _Work:
    """What one workbook's reading may still spend, across all its sheets: XML elements
    (``MAX_ELEMENTS_PER_WORKBOOK``, deterministic) and, as a backstop on a slow or loaded
    machine, wall time (``MAX_SECONDS``). Either one spent cuts the sheet being read with a
    marker and leaves every later sheet named as not read, so one file's extraction always
    ends, never holding a capture walk at that file run after run."""

    def __init__(self) -> None:
        self.elements = MAX_ELEMENTS_PER_WORKBOOK
        self.deadline = _clock() + MAX_SECONDS
        self.spent: str | None = None   # "workbook_elements" or "time" once spent

    def take(self, count: int) -> bool:
        """Spend ``count`` elements; False (``spent`` set) once the work is spent."""
        self.elements -= count
        if self.elements < 0:
            self.spent = "workbook_elements"
        elif _clock() > self.deadline:
            self.spent = "time"
        return self.spent is None


def _parse_rows(stream: _Bounded, sheet: _Sheet, shared: list[str], budget: int,
                work: _Work) -> None:
    """Stream one sheet's rows into ``sheet``. Every finished element is dropped from its
    parent at once (a cell's children only when the cell is read), so the tree never holds
    more than the open path and one cell; parsing stops at ``</sheetData>`` (nothing after
    it is transcribed). Every element is counted against ``MAX_ELEMENTS_PER_SHEET`` and
    the workbook's ``work``, every row's cells against ``MAX_CELLS_PER_ROW``
    (``sheet.cut`` names a bound that stopped it), so neither memory nor time grows past
    the bounds whatever the XML's shape: work is linear in the elements read."""
    elements = 0
    open_path: list[ET.Element] = []
    row: ET.Element | None = None
    cell: ET.Element | None = None
    number = previous_row = column = in_row = 0
    cells: list[tuple[str, str, str | None]] = []
    for event, node in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            elements += 1
            if elements > MAX_ELEMENTS_PER_SHEET:
                sheet.cut = "elements"
                return
            if elements % _WORK_STEP == 0 and not work.take(_WORK_STEP):
                sheet.cut = work.spent
                return
            parent = open_path[-1] if open_path else None
            open_path.append(node)
            if cell is not None:
                continue   # inside a cell: its value, formula or inline string
            if node.tag == MAIN + "row":
                text = node.get("r")
                if text is not None:
                    if not text.isdigit() or not 0 < int(text) < 10_000_000:
                        raise _BadSheet("row reference invalid")
                    number = int(text)
                else:
                    number = previous_row + 1
                previous_row, column, in_row, cells, row = number, 0, 0, [], node
            elif node.tag == MAIN + "c" and row is not None and parent is row:
                cell = node
            continue
        open_path.pop()
        if cell is not None and node is not cell:
            continue   # kept until its cell is read
        tag = node.tag
        if node is cell:
            cell = None
            address = node.get("r")
            if address is not None:
                found = CELL.fullmatch(address)
                if found is None:
                    raise _BadSheet("cell reference invalid")
                column = _column(found.group(1))
            else:
                column += 1
                address = f"{_letters(column)}{number}"
            value, formula = _cell_value(node, shared)
            in_row += 1
            if in_row > MAX_CELLS_PER_ROW:
                sheet.cut = "row_cells"
                return
            if value or formula is not None:
                cells.append((address, value, formula))
        elif node is row:
            row = None
            if cells and not sheet.add(number, cells, budget):
                return
            cells = []
        node.clear()
        if open_path:
            open_path[-1].remove(node)   # its only finished child: constant time
        if tag == MAIN + "sheetData":
            break
    # The sheet is read whole; what it spent past the last step counts for the next one.
    work.take(elements % _WORK_STEP)


def _read_sheet(archive: ZipFile, target: str, sheet: _Sheet, shared: list[str],
                budget: int, room: int, work: _Work) -> int:
    """Read one sheet's rows into ``sheet`` (at most ``budget`` characters of them are
    ever shown, so reading stops past it); ``room``: workbook XML bytes left. Returns
    the XML bytes read."""
    stream: _Bounded | None = None
    try:
        with archive.open(archive.getinfo(target)) as raw:
            stream = _Bounded(raw, min(MAX_SHEET_BYTES, room))
            _parse_rows(stream, sheet, shared, budget, work)
    except ET.ParseError:
        if stream is not None and stream.cut:
            sheet.cut = "bytes" if stream.limit == MAX_SHEET_BYTES else "workbook_bytes"
        else:
            sheet.drop("invalid")
    except _BadSheet:
        sheet.drop("invalid")
    except ValidationError:   # a DTD: the workbook is refused, never read in part
        raise
    except (_DamagedMember, *ZIP_ERRORS):
        sheet.drop("damaged")
    else:
        if stream is not None and stream.cut and sheet.cut is None:
            sheet.cut = "bytes" if stream.limit == MAX_SHEET_BYTES else "workbook_bytes"
    return 0 if stream is None else stream.read_bytes


def _shares(demands: list[int], budget: int) -> list[int]:
    """Water-filling: each demand gets at most an equal share of what is left, the
    smallest first; what a small one leaves is split between the larger ones."""
    shares = [0] * len(demands)
    order = sorted(range(len(demands)), key=lambda i: (demands[i], i))
    left = budget
    for position, index in enumerate(order):
        fair = left // (len(order) - position)
        shares[index] = min(demands[index], fair)
        left -= shares[index]
    return shares


def extract_xlsx_text(content: bytes, *, max_text: int) -> tuple[
        str, str, tuple[str, ...], tuple[tuple[int, int], ...], tuple[dict[str, Any], ...]]:
    """Return text, status, warnings, interpreted span ranges and sheet spans.

    A sheet span (``{"sheet", "number", "start", "end"}``) covers the sheet's
    ``[Sheet: name]`` line, its rows and its marker, so a claim can cite its sheet.
    What was not read is named in the text (module docstring), never dropped silently.
    """
    try:
        archive = ZipFile(BytesIO(content))
    except ZIP_ERRORS:
        # An encrypted workbook is a CFB container holding the package.
        return "", "unsupported", ("xlsx_encrypted" if encrypted_ooxml(content)
                                   else "xlsx_invalid_zip",), (), ()
    with archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValidationError("workbook expansion exceeds bound")
        if any(item.flag_bits & 1 for item in members):
            return "", "unsupported", ("xlsx_encrypted",), (), ()
        names = set(archive.namelist())
        if len(names) != len(members):
            raise ValidationError("workbook has duplicate ZIP members")
        if ("[Content_Types].xml" not in names or "xl/workbook.xml" not in names or
                "xl/_rels/workbook.xml.rels" not in names):
            return "", "unsupported", ("xlsx_missing_workbook",), (), ()
        try:
            workbook = _part(archive, "xl/workbook.xml")
            rels = _part(archive, "xl/_rels/workbook.xml.rels")
            shared = (_shared_strings(archive, "xl/sharedStrings.xml")
                      if "xl/sharedStrings.xml" in names else [])
        except (ET.ParseError, KeyError):
            return "", "unsupported", ("xlsx_invalid_xml",), (), ()
        except _DamagedMember:
            return "", "unsupported", ("xlsx_invalid_zip",), (), ()
        targets: dict[str, str] = {}
        for relation in rels.findall(PKG_REL + "Relationship"):
            target = relation.get("Target") or ""
            if relation.get("TargetMode") == "External":
                continue
            normalized = posixpath.normpath(posixpath.join("xl", target.lstrip("/")))
            if target.startswith("/"):
                normalized = posixpath.normpath(target.lstrip("/"))
            if normalized.startswith(("xl/worksheets/", "xl/chartsheets/",
                                      "xl/dialogsheets/", "xl/macrosheets/")) and \
                    ".." not in normalized.split("/"):
                targets[relation.get("Id") or ""] = normalized
        declared = workbook.findall(".//" + MAIN + "sheet")
        if len(declared) > MAX_SHEET_ENTRIES:
            raise ValidationError("workbook sheet count exceeds bound")
        sheets, skipped = sheet_plan([entry.get("name") or "(unnamed)" for entry in declared])
        budget = rows_budget(sheets, skipped, max_text)
        room = MAX_READ_BYTES
        work = _Work()
        for entry, sheet in zip(declared, sheets):
            target = targets.get(entry.get(REL + "id") or "")
            if target not in names:
                sheet.unread = "missing"
            elif not target.startswith("xl/worksheets/"):
                sheet.unread = "chart"
            elif room <= 0:
                sheet.unread = "workbook_bytes"
            elif work.spent is not None:
                sheet.unread = work.spent
            else:
                room -= _read_sheet(archive, target, sheet, shared, budget, room, work)
    return assemble(sheets, skipped, max_text, "xlsx")


def sheet_plan(names: list[str]) -> tuple[list[_Sheet], list[str]]:
    """The sheets read (the first ``MAX_SHEETS``) and the names of the rest."""
    return [_Sheet(name) for name in names[:MAX_SHEETS]], names[MAX_SHEETS:]


def _skipped_line(skipped: list[str]) -> str:
    if not skipped:
        return ""
    listed = ", ".join(skipped)
    if len(listed) > _MAX_NAMES:
        listed = listed[:_MAX_NAMES] + "..."
    return f"[Sheets not read: {len(skipped)} past the sheet bound {MAX_SHEETS}: {listed}]"


def rows_budget(sheets: list[_Sheet], skipped: list[str], max_text: int) -> int:
    """The characters of ``max_text`` left for rows once every heading and marker is
    reserved; a bound too small even for those fails the workbook."""
    line = _skipped_line(skipped)
    budget = max_text - sum(sheet.reserve() for sheet in sheets) - (len(line) + 1 if line
                                                                     else 0)
    if budget < 0:
        raise ValidationError("extracted workbook text exceeds bound")
    return budget


def assemble(sheets: list[_Sheet], skipped: list[str], max_text: int, prefix: str
             ) -> tuple[str, str, tuple[str, ...], tuple[tuple[int, int], ...],
                        tuple[dict[str, Any], ...]]:
    """The workbook's text from its read sheets: each sheet's rows within its fair
    share of the budget (:func:`_shares`), then its marker when cut; ``prefix`` names
    the format in the warnings (``xlsx_rows_truncated``, ``xls_...``)."""
    budget = rows_budget(sheets, skipped, max_text)
    skipped_line = _skipped_line(skipped)
    shares = _shares([sheet.demand for sheet in sheets], budget)
    lines: list[str] = []
    review_ranges: list[tuple[int, int]] = []
    sheet_spans: list[dict[str, Any]] = []
    position = 0

    def add(line: str, *, review: bool = False) -> None:
        nonlocal position
        start = position
        lines.append(line)
        position += len(line) + 1
        if review:
            review_ranges.append((start, start + len(line)))

    truncated = unreadable = False
    formula_count = interpreted_count = 0
    for sheet, share in zip(sheets, shares):
        sheet_start = position
        add(sheet.heading())
        if sheet.unread is not None:
            unreadable = True
            add(_UNREAD.format(name=sheet.name, why=_UNREAD_REASONS[sheet.unread]))
        else:
            used = shown = last = 0
            for number, unit, formula, interpreted in sheet.units:
                size = sum(len(line) + 1 for line, _ in unit)
                if used + size > share:
                    break
                used += size
                shown += 1
                last = number
                for line, review in unit:
                    add(line, review=review)
                formula_count += formula
                interpreted_count += interpreted
            why = sheet.cut if shown == len(sheet.units) else "text"
            if why is not None:
                truncated = True
                add(_TRUNCATED.format(name=sheet.name, shown=shown, last=last,
                                      why=_REASONS[why]))
        sheet_spans.append({"sheet": sheet.name, "number": len(sheet_spans) + 1,
                            "start": sheet_start, "end": position - 1})
    if skipped_line:
        add(skipped_line)
    text = "\n".join(lines)
    if len(text) > max_text:   # the reserve above makes this unreachable
        raise ValidationError("extracted workbook text exceeds bound")
    if sheets and all(sheet.unread is not None for sheet in sheets):
        first = sheets[0].unread
        return "", "unsupported", (f"{prefix}_missing_sheet" if first == "missing" else
                                   f"{prefix}_invalid_zip" if first == "damaged" else
                                   f"{prefix}_invalid_sheet_xml",), (), ()
    warnings = tuple(f"{prefix}_{name}" for name, present in (
        ("formulas_not_recalculated", formula_count),
        ("adjacent_cells_require_review", interpreted_count),
        ("rows_truncated", truncated), ("sheet_unreadable", unreadable),
        ("sheets_not_read", skipped)) if present)
    return (text, "extracted" if text.strip() else "no_text", warnings,
            tuple(review_ranges), tuple(sheet_spans))


__all__ = ["assemble", "extract_xlsx_text", "rows_budget", "sheet_plan"]
