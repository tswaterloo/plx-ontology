"""Bounded, read-only extraction of OOXML workbook evidence.

Cells and formulas are transcribed, never evaluated. Adjacent label/value
renderings are heuristic review leads; the original workbook remains the
authority and can be opened through its inherited SharePoint/Outlook link.
"""

from __future__ import annotations

from io import BytesIO
import posixpath
import re
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from ontology.core import ValidationError
from .office_text import ZIP_ERRORS, encrypted_ooxml


MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_PART_BYTES = 5_000_000
MAX_XML_BYTES = 20_000_000
MAX_MEMBERS = 2_000
MAX_SHEETS = 40
MAX_CELLS = 8_000
MAX_CELL_CHARS = 2_000
CELL = re.compile(r"^[A-Z]{1,4}[1-9][0-9]{0,6}$")
CURRENCY = {"USD", "CAD", "EUR", "GBP"}


def _column(address: str) -> int:
    result = 0
    for character in address.rstrip("0123456789"):
        result = result * 26 + ord(character) - ord("A") + 1
    return result


class _DamagedMember(Exception):
    """A ZIP member that cannot be read (bad CRC or header, unsupported method)."""


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
    if b"<!DOCTYPE" in raw.upper():
        raise ValidationError("workbook XML DTD not permitted")
    return ET.fromstring(raw)


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(part.text or "" for part in node.iter(MAIN + "t"))


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
            raise ValidationError("workbook shared string index invalid") from error
        if not 0 <= index < len(shared):
            raise ValidationError("workbook shared string index invalid")
        value = shared[index]
    else:
        value = value_node.text or "" if value_node is not None else ""
    if len(value) > MAX_CELL_CHARS or (formula is not None and len(formula) > MAX_CELL_CHARS):
        raise ValidationError("workbook cell exceeds bound")
    # ISO dates are explicit strings; numeric Excel serials are left numeric.
    return value, formula


def _label_line(cells: list[tuple[str, str, str | None]], sheet: str) -> str | None:
    """Render only a nearby label/value pair, always for human review."""
    if len(cells) not in {2, 3} or any(formula is not None for _, _, formula in cells):
        return None
    if any(_column(cells[index + 1][0]) != _column(cells[index][0]) + 1
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


def extract_xlsx_text(content: bytes, *, max_text: int) -> tuple[
        str, str, tuple[str, ...], tuple[tuple[int, int], ...], tuple[dict[str, Any], ...]]:
    """Return text, status, warnings, interpreted span ranges and sheet spans.

    A sheet span (``{"sheet", "number", "start", "end"}``) covers the sheet's
    ``[Sheet: name]`` line and its rows, so a claim can cite its sheet.

    Any OOXML structure not proven safe/under bounds fails the document, so a
    Graph delta cursor cannot advance past a silently partial workbook.
    """
    try:
        archive = ZipFile(BytesIO(content))
    except ZIP_ERRORS:
        # An encrypted workbook is a CFB container holding the package.
        return "", "unsupported", ("xlsx_encrypted" if encrypted_ooxml(content)
                                   else "xlsx_invalid_zip",), (), ()
    with archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_XML_BYTES:
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
            shared_root = _part(archive, "xl/sharedStrings.xml") if "xl/sharedStrings.xml" in names else None
        except (ET.ParseError, KeyError):
            return "", "unsupported", ("xlsx_invalid_xml",), (), ()
        except _DamagedMember:
            return "", "unsupported", ("xlsx_invalid_zip",), (), ()
        shared = [_text(si) for si in shared_root.findall(MAIN + "si")] if shared_root is not None else []
        if len(shared) > MAX_CELLS or any(len(value) > MAX_CELL_CHARS for value in shared):
            raise ValidationError("workbook shared strings exceed bound")
        targets: dict[str, str] = {}
        for relation in rels.findall(PKG_REL + "Relationship"):
            target = relation.get("Target") or ""
            if relation.get("TargetMode") == "External":
                continue
            normalized = posixpath.normpath(posixpath.join("xl", target.lstrip("/")))
            if target.startswith("/"):
                normalized = posixpath.normpath(target.lstrip("/"))
            if normalized.startswith("xl/worksheets/") and ".." not in normalized.split("/"):
                targets[relation.get("Id") or ""] = normalized
        sheets = workbook.findall(".//" + MAIN + "sheet")
        if len(sheets) > MAX_SHEETS:
            raise ValidationError("workbook sheet count exceeds bound")
        lines: list[str] = []
        review_ranges: list[tuple[int, int]] = []
        sheet_spans: list[dict[str, Any]] = []
        formula_count = 0
        interpreted_count = 0
        cell_count = 0
        position = 0

        def add(line: str, *, review: bool = False) -> None:
            nonlocal position
            if position + len(line) + 1 > max_text:
                raise ValidationError("extracted workbook text exceeds bound")
            start = position
            lines.append(line)
            position += len(line) + 1
            if review:
                review_ranges.append((start, start + len(line)))

        for sheet in sheets:
            sheet_name = sheet.get("name") or "(unnamed)"
            target = targets.get(sheet.get(REL + "id") or "")
            if target not in names:
                return "", "unsupported", ("xlsx_missing_sheet",), (), ()
            try:
                root = _part(archive, target)
            except (ET.ParseError, KeyError):
                return "", "unsupported", ("xlsx_invalid_sheet_xml",), (), ()
            except _DamagedMember:
                return "", "unsupported", ("xlsx_invalid_zip",), (), ()
            sheet_start = position
            add(f"[Sheet: {sheet_name}]")
            for row in root.findall(".//" + MAIN + "sheetData/" + MAIN + "row"):
                cells: list[tuple[str, str, str | None]] = []
                for cell in row.findall(MAIN + "c"):
                    address = cell.get("r") or ""
                    if not CELL.fullmatch(address):
                        raise ValidationError("workbook cell reference invalid")
                    value, formula = _cell_value(cell, shared)
                    if not value and formula is None:
                        continue
                    cell_count += 1
                    if cell_count > MAX_CELLS:
                        raise ValidationError("workbook populated cell count exceeds bound")
                    cells.append((address, value, formula))
                if not cells:
                    continue
                pieces = []
                for address, value, formula in cells:
                    if formula is not None:
                        formula_count += 1
                        pieces.append(f"{address}=FORMULA({formula}); cached={value or '(missing)'}")
                    else:
                        pieces.append(f"{address}={value}")
                add(" | ".join(pieces), review=any(formula is not None for _, _, formula in cells))
                interpreted = _label_line(cells, sheet_name)
                if interpreted:
                    interpreted_count += 1
                    add(interpreted, review=True)
            sheet_spans.append({"sheet": sheet_name, "number": len(sheet_spans) + 1,
                                "start": sheet_start, "end": position - 1})
        warnings = tuple((["xlsx_formulas_not_recalculated"] if formula_count else []) +
                         (["xlsx_adjacent_cells_require_review"] if interpreted_count else []))
        text = "\n".join(lines)
        return (text, "extracted" if text.strip() else "no_text", warnings,
                tuple(review_ranges), tuple(sheet_spans))


__all__ = ["extract_xlsx_text"]
