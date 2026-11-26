"""Non-destructive Excel (.xlsx) workbook editing for PLX Workbench.

Supports:
* ``add_sheet_to_workbook``: add a formatted worksheet tab (clean executive typography,
  frozen header row, auto-filter, auto-fitted column widths, currency/percentage/date
  number formats, and live Excel formulas) to an existing ``.xlsx`` workbook without
  modifying existing tabs, formulas, charts, drawings, or styles.
* ``append_rows_to_sheet``: append new rows to an existing worksheet (matching column
  headers when given dicts, or creating the worksheet if missing) while preserving
  existing rows, formulas, charts, and styles, and extending auto-filters and Excel
  tables.
* ``update_named_cells``: update Excel named cells (``<definedNames>``) or explicit
  ``Sheet!A1`` / ``A1`` cell references while preserving cell formatting, existing
  formulas, charts, and styles.
* ``inspect_workbook``: summarize sheet names, headers, row counts, defined names, and
  formula counts for preview and validation.

Implemented directly over OOXML zip packages with the Python standard library so every
untouched workbook part (charts, drawings, themes, macros, other sheets) is copied byte
for byte and no optional third-party dependency is required.

Evidence label: ``isolated-fixture`` (``operating/tests/test_sharepoint_excel_studio.py``).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from html import unescape
import io
import re
from typing import Any, Mapping, Optional, Sequence
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

_FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

_SHEET_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_STYLES_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
_WORKSHEET_REL = f"{_NS_REL}/worksheet"
_STYLES_REL = f"{_NS_REL}/styles"
_TABLE_REL = f"{_NS_REL}/table"

_CELL_REF_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")
_PLAIN_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_MONEY_RE = re.compile(
    r"^(?P<neg>-|\()?\s*(?P<cur>\$)?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*\)?$"
)
_PERCENT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*%$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SHEET_BAD_CHARS = re.compile(r"[\\/?*\[\]:]+")
_AGG_FUNCS = frozenset({"SUM", "AVERAGE", "AVG", "COUNT", "MIN", "MAX"})

# Executive neutral palette for newly added studio tabs.
_INK = "1F2937"
_HEADER_INK = "0F172A"
_HEADER_FILL = "F1F5F9"
_ZEBRA_FILL = "F8FAFC"
_TOTAL_FILL = "F1F5F9"
_BORDER_LIGHT = "E2E8F0"
_BORDER_MID = "CBD5E1"
_BORDER_HEADER = "64748B"
_BORDER_TOTAL = "334155"

_FORMAT_ALIASES: Mapping[str, str] = {
    "currency": '"$"#,##0.00;-"$"#,##0.00;"$"0.00',
    "money": '"$"#,##0.00;-"$"#,##0.00;"$"0.00',
    "usd": '"$"#,##0.00;-"$"#,##0.00;"$"0.00',
    "percent": "0.0%",
    "pct": "0.0%",
    "percentage": "0.0%",
    "integer": "#,##0",
    "int": "#,##0",
    "decimal": "#,##0.00",
    "number": "#,##0.00",
    "dec2": "#,##0.00",
    "date": "yyyy-mm-dd",
    "text": "@",
}


class ExcelOpError(ValueError):
    """Raised when an Excel workbook operation cannot be safely applied."""


def _col_letter(index: int) -> str:
    """0-based column index to Excel column letters (0 -> 'A')."""
    if index < 0:
        raise ExcelOpError("Column index must be non-negative.")
    out = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _col_index(letters: str) -> int:
    """Excel column letters to 0-based index ('A' -> 0)."""
    acc = 0
    for ch in letters.upper():
        if not ("A" <= ch <= "Z"):
            raise ExcelOpError(f"Invalid Excel column: {letters}")
        acc = acc * 26 + (ord(ch) - 64)
    return acc - 1


def _parse_cell_ref(ref: str) -> tuple[int, int]:
    """Return (col_index_0, row_1) for a cell coordinate such as 'C4' or '$C$4'."""
    m = _CELL_REF_RE.fullmatch(ref.strip())
    if not m:
        raise ExcelOpError(f"Invalid Excel cell reference: {ref!r}")
    return _col_index(m.group(1)), int(m.group(2))


def sanitize_sheet_name(raw: Any) -> str:
    """Return a valid Excel worksheet tab name (1..31 chars, no illegal chars)."""
    text = str(raw or "").replace("\u2014", " - ").replace("\u2013", "-")
    text = _SHEET_BAD_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" '")
    if not text:
        return "Sheet"
    return text[:31].rstrip(" '") or "Sheet"


def _norm_header(raw: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(raw or "").strip().lower())
    return " ".join(text.split())


def _open_zip(data: bytes) -> zipfile.ZipFile:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ExcelOpError("Workbook bytes are empty.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data), "r")
    except zipfile.BadZipFile:
        raise ExcelOpError("The input is not a valid .xlsx workbook.") from None
    if "xl/workbook.xml" not in zf.namelist():
        zf.close()
        raise ExcelOpError("The zip package has no xl/workbook.xml part.")
    return zf


def _rezip_with_updates(
    original: bytes,
    updates: Mapping[str, bytes],
    *,
    remove: Sequence[str] = (),
) -> bytes:
    """Rebuild an .xlsx zip replacing/adding ``updates`` and copying all other parts byte for byte."""
    drop = set(remove)
    out = io.BytesIO()
    written: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(original), "r") as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            name = info.filename
            if name in drop:
                continue
            if name in updates:
                new_info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
                new_info.compress_type = zipfile.ZIP_DEFLATED
                dst.writestr(new_info, updates[name])
            else:
                dst.writestr(info, src.read(name))
            written.add(name)
        for name, body in updates.items():
            if name not in written and name not in drop:
                new_info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
                new_info.compress_type = zipfile.ZIP_DEFLATED
                dst.writestr(new_info, body)
    return out.getvalue()


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    raw = zf.read("xl/sharedStrings.xml")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out: list[str] = []
    for si in root.findall(f"{{{_NS_MAIN}}}si"):
        parts = [t.text or "" for t in si.iter(f"{{{_NS_MAIN}}}t")]
        out.append("".join(parts))
    return out


def _workbook_sheets(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    """Return ordered sheet entries: name, sheet_id, r_id, target (xl/...)."""
    book_xml = zf.read("xl/workbook.xml").decode("utf-8", "replace")
    rels_xml = (
        zf.read("xl/_rels/workbook.xml.rels").decode("utf-8", "replace")
        if "xl/_rels/workbook.xml.rels" in zf.namelist()
        else ""
    )
    rel_map: dict[str, str] = {}
    for m in re.finditer(r"<Relationship\s[^>]*/?>", rels_xml):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(0)))
        rid = attrs.get("Id")
        tgt = unescape(attrs.get("Target", "")).lstrip("/")
        if rid and tgt:
            rel_map[rid] = tgt if tgt.startswith("xl/") else f"xl/{tgt}"

    sheets: list[dict[str, Any]] = []
    for m in re.finditer(r"<sheet\s[^>]*/?>", book_xml):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(0)))
        name = unescape(attrs.get("name", ""))
        sid = int(attrs.get("sheetId", "0") or 0)
        rid = attrs.get("r:id", "")
        sheets.append({
            "name": name,
            "sheet_id": sid,
            "r_id": rid,
            "target": rel_map.get(rid, ""),
        })
    return sheets


def _defined_names(zf: zipfile.ZipFile) -> dict[str, str]:
    book_xml = zf.read("xl/workbook.xml").decode("utf-8", "replace")
    out: dict[str, str] = {}
    for m in re.finditer(r'<definedName\s+([^>]*)>(.*?)</definedName>', book_xml, re.S):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(1)))
        name = unescape(attrs.get("name", "")).strip()
        ref = unescape(m.group(2)).strip()
        if name and ref:
            out[name] = ref
    return out


def _read_sheet_grid(
    sheet_xml: str,
    shared: Sequence[str],
) -> tuple[dict[int, dict[int, dict[str, Any]]], int]:
    """Parse worksheet rows into {row_1: {col_0: {value, style, formula, type}}}, max_row."""
    rows: dict[int, dict[int, dict[str, Any]]] = {}
    max_row = 0
    try:
        root = ET.fromstring(sheet_xml.encode("utf-8"))
    except ET.ParseError as exc:
        raise ExcelOpError(f"Worksheet XML is not readable: {exc}") from None
    sheet_data = root.find(f"{{{_NS_MAIN}}}sheetData")
    if sheet_data is None:
        return rows, 0
    for row_el in sheet_data.findall(f"{{{_NS_MAIN}}}row"):
        r_attr = row_el.get("r")
        r_num = int(r_attr) if r_attr and r_attr.isdigit() else max_row + 1
        max_row = max(max_row, r_num)
        row_cells: dict[int, dict[str, Any]] = {}
        fallback_col = 0
        for c_el in row_el.findall(f"{{{_NS_MAIN}}}c"):
            ref = c_el.get("r")
            if ref:
                try:
                    c_idx, _ = _parse_cell_ref(ref)
                except ExcelOpError:
                    c_idx = fallback_col
            else:
                c_idx = fallback_col
            fallback_col = c_idx + 1
            ctype = c_el.get("t", "")
            style = c_el.get("s")
            f_el = c_el.find(f"{{{_NS_MAIN}}}f")
            v_el = c_el.find(f"{{{_NS_MAIN}}}v")
            is_el = c_el.find(f"{{{_NS_MAIN}}}is")
            formula = (f_el.text or "") if f_el is not None else None
            val: Any = None
            if ctype == "s" and v_el is not None and (v_el.text or "").isdigit():
                idx = int(v_el.text or "0")
                val = shared[idx] if idx < len(shared) else ""
            elif ctype == "inlineStr" and is_el is not None:
                val = "".join(t.text or "" for t in is_el.iter(f"{{{_NS_MAIN}}}t"))
            elif v_el is not None:
                val = v_el.text
            row_cells[c_idx] = {
                "value": val,
                "style": int(style) if style and style.isdigit() else None,
                "formula": formula,
                "type": ctype,
            }
        if row_cells:
            rows[r_num] = row_cells
    return rows, max_row


def _detect_header_row(grid: Mapping[int, Mapping[int, Mapping[str, Any]]]) -> tuple[int, list[str]]:
    """Return (header_row_1, headers_list) from a parsed worksheet grid."""
    if not grid:
        return 0, []
    sorted_rows = sorted(grid.keys())
    # Skip a leading single-cell title banner if followed by a multi-column header row.
    for r in sorted_rows[:6]:
        cells = grid[r]
        non_empty = [(c, str(info.get("value") or "").strip())
                     for c, info in sorted(cells.items())
                     if info.get("value") is not None and str(info.get("value")).strip() != ""]
        if len(non_empty) >= 2:
            width = max(c for c, _ in non_empty) + 1
            return r, [str(cells.get(c, {}).get("value") or "").strip() for c in range(width)]
    first_r = sorted_rows[0]
    cells = grid[first_r]
    width = max(cells.keys()) + 1 if cells else 0
    return first_r, [str(cells.get(c, {}).get("value") or "").strip() for c in range(width)]


def _classify_value(
    raw: Any,
    explicit_fmt: Optional[str] = None,
) -> tuple[str, str, str]:
    """Convert a Python cell value to (kind, wire_value, style_key).

    kind is one of: 'empty', 'formula', 'number', 'date', 'text'.
    """
    if raw is None:
        return "empty", "", "text"
    if isinstance(raw, bool):
        return "text", "TRUE" if raw else "FALSE", "text"
    if isinstance(raw, dt.datetime):
        raw = raw.date()
    if isinstance(raw, dt.date):
        serial = (raw - dt.date(1899, 12, 30)).days
        return "date", str(serial), explicit_fmt or "date"
    if isinstance(raw, (int, Decimal)):
        num_str = format(raw, "f") if isinstance(raw, Decimal) else str(raw)
        if explicit_fmt:
            return "number", num_str, explicit_fmt
        return "number", num_str, "int" if isinstance(raw, int) else "dec2"
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            return "text", str(raw), "text"
        num_str = repr(raw)
        if explicit_fmt:
            return "number", num_str, explicit_fmt
        return "number", num_str, "int" if raw.is_integer() else "dec2"

    text = str(raw).strip()
    if not text:
        return "empty", "", "text"

    if text.startswith("=") and len(text) > 1:
        return "formula", text[1:].strip(), explicit_fmt or "dec2"

    if explicit_fmt == "text":
        return "text", text, "text"

    # Preserve leading-zero identifiers (PO numbers, SKUs, ZIP codes like "00123") as text.
    if _PLAIN_NUM_RE.fullmatch(text):
        int_part = text.lstrip("-").split(".")[0]
        if len(int_part) > 1 and int_part.startswith("0"):
            return "text", text, "text"
        if len(text.replace("-", "").replace(".", "")) > 15:
            return "text", text, "text"
        if explicit_fmt:
            return "number", text, explicit_fmt
        return "number", text, "dec2" if "." in text else "int"

    m_money = _MONEY_RE.fullmatch(text)
    if m_money and (m_money.group("cur") or "," in m_money.group("num") or m_money.group("neg") == "("):
        if (m_money.group("neg") == "(") == text.endswith(")"):
            num = m_money.group("num").replace(",", "")
            val = ("-" if m_money.group("neg") else "") + num
            if m_money.group("cur") == "$":
                return "number", val, explicit_fmt or "currency"
            return "number", val, explicit_fmt or ("dec2" if "." in num else "int")

    m_pct = _PERCENT_RE.fullmatch(text)
    if m_pct:
        try:
            frac = Decimal(m_pct.group(1)) / Decimal(100)
            return "number", format(frac.normalize(), "f"), explicit_fmt or "percent"
        except InvalidOperation:
            pass

    m_date = _ISO_DATE_RE.fullmatch(text)
    if m_date:
        try:
            day = dt.date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))
            serial = (day - dt.date(1899, 12, 30)).days
            return "date", str(serial), explicit_fmt or "date"
        except ValueError:
            pass

    return "text", text, "text"


def _minimal_styles_xml() -> str:
    return (
        f'{_XML_DECL}<styleSheet xmlns="{_NS_MAIN}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        "</styleSheet>"
    )


def _append_studio_styles(
    existing_styles_xml: Optional[str],
    extra_format_codes: Sequence[str] = (),
) -> tuple[str, dict[str, int]]:
    """Append studio styles to ``xl/styles.xml`` without changing any existing xf index.

    Returns ``(updated_styles_xml, style_index_map)``.
    """
    ET.register_namespace("", _NS_MAIN)
    raw = existing_styles_xml or _minimal_styles_xml()
    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except ET.ParseError:
        root = ET.fromstring(_minimal_styles_xml().encode("utf-8"))

    def _tag(name: str) -> str:
        return f"{{{_NS_MAIN}}}{name}"

    def _ensure_section(name: str, insert_before: Sequence[str] = ()) -> ET.Element:
        el = root.find(_tag(name))
        if el is not None:
            return el
        el = ET.Element(_tag(name), {"count": "0"})
        for anchor in insert_before:
            found = root.find(_tag(anchor))
            if found is not None:
                idx = list(root).index(found)
                root.insert(idx, el)
                return el
        root.append(el)
        return el

    num_fmts = _ensure_section("numFmts", ("fonts", "fills", "borders", "cellStyleXfs", "cellXfs"))
    fonts = _ensure_section("fonts", ("fills", "borders", "cellStyleXfs", "cellXfs"))
    fills = _ensure_section("fills", ("borders", "cellStyleXfs", "cellXfs"))
    borders = _ensure_section("borders", ("cellStyleXfs", "cellXfs"))
    cell_xfs = _ensure_section("cellXfs")

    # Determine next available custom numFmtId (>= 164).
    used_fmt_ids = {
        int(nf.get("numFmtId", "0"))
        for nf in num_fmts.findall(_tag("numFmt"))
        if (nf.get("numFmtId") or "").isdigit()
    }
    code_to_id: dict[str, int] = {}
    for nf in num_fmts.findall(_tag("numFmt")):
        fc = nf.get("formatCode")
        fid = nf.get("numFmtId")
        if fc and fid and fid.isdigit():
            code_to_id[fc] = int(fid)
    next_fmt_id = max(used_fmt_ids | {163}) + 1

    def add_numfmt(code: str) -> int:
        nonlocal next_fmt_id
        if code in code_to_id:
            return code_to_id[code]
        fid = next_fmt_id
        next_fmt_id += 1
        ET.SubElement(num_fmts, _tag("numFmt"), {"numFmtId": str(fid), "formatCode": code})
        code_to_id[code] = fid
        return fid

    fmt_ids = {
        "int": add_numfmt("#,##0"),
        "dec2": add_numfmt("#,##0.00"),
        "currency": add_numfmt(_FORMAT_ALIASES["currency"]),
        "percent": add_numfmt(_FORMAT_ALIASES["percent"]),
        "date": add_numfmt(_FORMAT_ALIASES["date"]),
        "text": 49,  # Built-in Excel text format "@"
    }
    custom_fmt_ids: dict[str, int] = {}
    for code in extra_format_codes:
        if code and code not in custom_fmt_ids:
            custom_fmt_ids[code] = add_numfmt(code)
    num_fmts.set("count", str(len(list(num_fmts))))

    # Append studio fonts.
    def add_font(size: str, color: str, bold: bool = False) -> int:
        idx = len(list(fonts))
        f = ET.SubElement(fonts, _tag("font"))
        if bold:
            ET.SubElement(f, _tag("b"))
        ET.SubElement(f, _tag("sz"), {"val": size})
        ET.SubElement(f, _tag("color"), {"rgb": f"FF{color}"})
        ET.SubElement(f, _tag("name"), {"val": "Calibri"})
        return idx

    f_body = add_font("10.5", _INK, bold=False)
    f_bold = add_font("10.5", _HEADER_INK, bold=True)
    f_title = add_font("13.5", _HEADER_INK, bold=True)
    fonts.set("count", str(len(list(fonts))))

    # Append studio fills.
    def add_fill(rgb: str) -> int:
        idx = len(list(fills))
        fill = ET.SubElement(fills, _tag("fill"))
        pf = ET.SubElement(fill, _tag("patternFill"), {"patternType": "solid"})
        ET.SubElement(pf, _tag("fgColor"), {"rgb": f"FF{rgb}"})
        ET.SubElement(pf, _tag("bgColor"), {"indexed": "64"})
        return idx

    fill_head = add_fill(_HEADER_FILL)
    fill_zebra = add_fill(_ZEBRA_FILL)
    fill_total = add_fill(_TOTAL_FILL)
    fills.set("count", str(len(list(fills))))

    # Append studio borders.
    def add_border(
        top: Optional[tuple[str, str]] = None,
        bottom: Optional[tuple[str, str]] = None,
    ) -> int:
        idx = len(list(borders))
        b = ET.SubElement(borders, _tag("border"))
        ET.SubElement(b, _tag("left"))
        ET.SubElement(b, _tag("right"))
        top_el = ET.SubElement(b, _tag("top"))
        if top:
            top_el.set("style", top[0])
            ET.SubElement(top_el, _tag("color"), {"rgb": f"FF{top[1]}"})
        bot_el = ET.SubElement(b, _tag("bottom"))
        if bottom:
            bot_el.set("style", bottom[0])
            ET.SubElement(bot_el, _tag("color"), {"rgb": f"FF{bottom[1]}"})
        ET.SubElement(b, _tag("diagonal"))
        return idx

    b_head = add_border(top=("thin", _BORDER_MID), bottom=("medium", _BORDER_HEADER))
    b_data = add_border(bottom=("thin", _BORDER_LIGHT))
    b_total = add_border(top=("thin", _BORDER_HEADER), bottom=("double", _BORDER_TOTAL))
    borders.set("count", str(len(list(borders))))

    # Append studio cellXfs at the end so all existing cell s="..." indices remain valid.
    def add_xf(
        num_fmt: int = 0,
        font_id: int = f_body,
        fill_id: int = 0,
        border_id: int = 0,
        *,
        h_align: Optional[str] = None,
        v_align: str = "center",
        wrap: bool = False,
    ) -> int:
        idx = len(list(cell_xfs))
        attrs = {
            "numFmtId": str(num_fmt),
            "fontId": str(font_id),
            "fillId": str(fill_id),
            "borderId": str(border_id),
            "xfId": "0",
            "applyFont": "1",
        }
        if num_fmt:
            attrs["applyNumberFormat"] = "1"
        if fill_id:
            attrs["applyFill"] = "1"
        if border_id:
            attrs["applyBorder"] = "1"
        attrs["applyAlignment"] = "1"
        xf = ET.SubElement(cell_xfs, _tag("xf"), attrs)
        al_attrs = {"vertical": v_align}
        if h_align:
            al_attrs["horizontal"] = h_align
        if wrap:
            al_attrs["wrapText"] = "1"
        ET.SubElement(xf, _tag("alignment"), al_attrs)
        return idx

    style_map: dict[str, int] = {
        "title": add_xf(0, f_title, 0, 0, h_align="left"),
        "header": add_xf(0, f_bold, fill_head, b_head, h_align="left", wrap=True),
        "header_num": add_xf(0, f_bold, fill_head, b_head, h_align="right", wrap=True),
        "text": add_xf(0, f_body, 0, b_data, h_align="left"),
        "text_alt": add_xf(0, f_body, fill_zebra, b_data, h_align="left"),
        "int": add_xf(fmt_ids["int"], f_body, 0, b_data, h_align="right"),
        "int_alt": add_xf(fmt_ids["int"], f_body, fill_zebra, b_data, h_align="right"),
        "dec2": add_xf(fmt_ids["dec2"], f_body, 0, b_data, h_align="right"),
        "dec2_alt": add_xf(fmt_ids["dec2"], f_body, fill_zebra, b_data, h_align="right"),
        "currency": add_xf(fmt_ids["currency"], f_body, 0, b_data, h_align="right"),
        "currency_alt": add_xf(fmt_ids["currency"], f_body, fill_zebra, b_data, h_align="right"),
        "percent": add_xf(fmt_ids["percent"], f_body, 0, b_data, h_align="right"),
        "percent_alt": add_xf(fmt_ids["percent"], f_body, fill_zebra, b_data, h_align="right"),
        "date": add_xf(fmt_ids["date"], f_body, 0, b_data, h_align="left"),
        "date_alt": add_xf(fmt_ids["date"], f_body, fill_zebra, b_data, h_align="left"),
        "total_text": add_xf(0, f_bold, fill_total, b_total, h_align="left"),
        "total_int": add_xf(fmt_ids["int"], f_bold, fill_total, b_total, h_align="right"),
        "total_dec2": add_xf(fmt_ids["dec2"], f_bold, fill_total, b_total, h_align="right"),
        "total_currency": add_xf(fmt_ids["currency"], f_bold, fill_total, b_total, h_align="right"),
        "total_percent": add_xf(fmt_ids["percent"], f_bold, fill_total, b_total, h_align="right"),
    }
    for code, fid in custom_fmt_ids.items():
        key = f"custom:{code}"
        style_map[key] = add_xf(fid, f_body, 0, b_data, h_align="right")
        style_map[f"{key}_alt"] = add_xf(fid, f_body, fill_zebra, b_data, h_align="right")
        style_map[f"total_{key}"] = add_xf(fid, f_bold, fill_total, b_total, h_align="right")

    cell_xfs.set("count", str(len(list(cell_xfs))))
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8"), style_map


def _resolve_col_formats(
    headers: Sequence[str],
    number_formats: Optional[Mapping[Any, str]],
) -> tuple[dict[int, str], list[str]]:
    """Map 0-based column index -> logical format key, plus any custom Excel format codes."""
    col_fmts: dict[int, str] = {}
    custom_codes: list[str] = []
    if not number_formats:
        return col_fmts, custom_codes
    header_by_norm = {_norm_header(h): i for i, h in enumerate(headers)}
    header_by_exact = {str(h): i for i, h in enumerate(headers)}
    for key, raw_spec in number_formats.items():
        spec = str(raw_spec or "").strip()
        if not spec:
            continue
        col_idx: Optional[int] = None
        if isinstance(key, int) and 0 <= key < len(headers):
            col_idx = key
        elif isinstance(key, str):
            if key in header_by_exact:
                col_idx = header_by_exact[key]
            elif _norm_header(key) in header_by_norm:
                col_idx = header_by_norm[_norm_header(key)]
            elif re.fullmatch(r"[A-Za-z]{1,3}", key.strip()):
                cand = _col_index(key.strip())
                if 0 <= cand < len(headers):
                    col_idx = cand
        if col_idx is None:
            continue
        low = spec.lower()
        if low in ("currency", "money", "usd"):
            col_fmts[col_idx] = "currency"
        elif low in ("percent", "pct", "percentage"):
            col_fmts[col_idx] = "percent"
        elif low in ("integer", "int"):
            col_fmts[col_idx] = "int"
        elif low in ("decimal", "number", "dec2"):
            col_fmts[col_idx] = "dec2"
        elif low == "date":
            col_fmts[col_idx] = "date"
        elif low == "text":
            col_fmts[col_idx] = "text"
        else:
            col_fmts[col_idx] = f"custom:{spec}"
            if spec not in custom_codes:
                custom_codes.append(spec)
    return col_fmts, custom_codes


def _normalize_row_values(
    row: Any,
    headers: Sequence[str],
    match_headers: bool = True,
) -> list[Any]:
    if isinstance(row, Mapping):
        if match_headers and headers:
            by_exact = {str(k): v for k, v in row.items()}
            by_lower = {str(k).strip().lower(): v for k, v in row.items()}
            by_norm = {_norm_header(k): v for k, v in row.items()}
            out: list[Any] = []
            for h in headers:
                hs = str(h)
                if hs in by_exact:
                    out.append(by_exact[hs])
                elif hs.strip().lower() in by_lower:
                    out.append(by_lower[hs.strip().lower()])
                elif _norm_header(hs) in by_norm:
                    out.append(by_norm[_norm_header(hs)])
                else:
                    out.append("")
            return out
        return list(row.values())
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        vals = list(row)
        if headers and len(vals) < len(headers):
            vals += [""] * (len(headers) - len(vals))
        return vals
    return [row]


def _cell_xml(ref: str, kind: str, wire: str, style_idx: Optional[int]) -> str:
    s_attr = f' s="{style_idx}"' if style_idx is not None else ""
    if kind == "empty":
        return f'<c r="{ref}"{s_attr}/>'
    if kind == "formula":
        return f'<c r="{ref}"{s_attr}><f>{escape(wire)}</f></c>'
    if kind in ("number", "date"):
        return f'<c r="{ref}"{s_attr}><v>{escape(wire)}</v></c>'
    return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t xml:space="preserve">{escape(wire)}</t></is></c>'


def _build_styled_worksheet_xml(
    headers: Sequence[str],
    rows: Sequence[Any],
    style_map: Mapping[str, int],
    col_fmts: Mapping[int, str],
    *,
    title: Optional[str] = None,
    formulas: Optional[Mapping[Any, str] | Sequence[tuple[str, str]]] = None,
) -> str:
    clean_headers = [str(h or "").strip() for h in headers]
    ncol = max(len(clean_headers), 1)
    widths: dict[int, int] = {c: max(len(clean_headers[c]) if c < len(clean_headers) else 8, 8) for c in range(ncol)}

    row_entries: dict[int, dict[int, str]] = {}
    row_heights: dict[int, float] = {}
    cur_row = 1

    merges: list[str] = []
    if title and str(title).strip():
        t_text = str(title).replace("\u2014", " - ").strip()
        row_entries[cur_row] = {0: _cell_xml(f"A{cur_row}", "text", t_text, style_map["title"])}
        row_heights[cur_row] = 26.0
        if ncol > 1:
            merges.append(f"A{cur_row}:{_col_letter(ncol - 1)}{cur_row}")
        cur_row += 1

    header_row = cur_row
    # Inspect data rows first to know which columns are numeric (for right-aligned headers).
    norm_rows = [_normalize_row_values(r, clean_headers, match_headers=True) for r in rows]
    col_numeric_hits: dict[int, int] = {}
    col_inferred_fmt: dict[int, str] = {}
    classified_rows: list[list[tuple[str, str, str]]] = []
    for r_vals in norm_rows:
        row_cls: list[tuple[str, str, str]] = []
        for c in range(max(ncol, len(r_vals))):
            val = r_vals[c] if c < len(r_vals) else ""
            kind, wire, fmt_key = _classify_value(val, col_fmts.get(c))
            row_cls.append((kind, wire, fmt_key))
            if kind == "number":
                col_numeric_hits[c] = col_numeric_hits.get(c, 0) + 1
                if c not in col_inferred_fmt or fmt_key in ("currency", "percent"):
                    col_inferred_fmt[c] = fmt_key
        classified_rows.append(row_cls)
        ncol = max(ncol, len(row_cls))

    # Write header row.
    head_cells: dict[int, str] = {}
    for c in range(ncol):
        h_text = clean_headers[c] if c < len(clean_headers) else f"Column {c + 1}"
        is_num_col = col_fmts.get(c) in ("currency", "percent", "int", "dec2") or (
            col_numeric_hits.get(c, 0) > 0 and col_numeric_hits.get(c, 0) >= max(1, len(norm_rows) // 2)
        )
        h_style = style_map["header_num"] if is_num_col else style_map["header"]
        head_cells[c] = _cell_xml(f"{_col_letter(c)}{header_row}", "text", h_text, h_style)
        widths[c] = max(widths.get(c, 8), len(h_text) + 2)
    row_entries[header_row] = head_cells
    row_heights[header_row] = 24.0
    cur_row += 1

    data_start = cur_row
    for r_idx, row_cls in enumerate(classified_rows):
        is_alt = (r_idx % 2) == 1
        cells_xml: dict[int, str] = {}
        for c, (kind, wire, fmt_key) in enumerate(row_cls):
            base_fmt = col_fmts.get(c) or fmt_key
            alt_key = f"{base_fmt}_alt"
            s_idx = style_map.get(alt_key if is_alt else base_fmt, style_map["text_alt" if is_alt else "text"])
            cells_xml[c] = _cell_xml(f"{_col_letter(c)}{cur_row}", kind, wire, s_idx)
            disp_len = 12 if kind == "formula" else (len(wire) + (4 if base_fmt == "currency" else 2))
            widths[c] = max(widths.get(c, 8), disp_len)
        row_entries[cur_row] = cells_xml
        row_heights[cur_row] = 19.5
        cur_row += 1
    data_end = cur_row - 1

    # Apply requested formulas (explicit cell refs or column-keyed summary formulas).
    has_formula = any(kind == "formula" for r_cls in classified_rows for kind, _, _ in r_cls)
    if formulas:
        items = list(formulas.items()) if isinstance(formulas, Mapping) else list(formulas)
        header_by_norm = {_norm_header(h): i for i, h in enumerate(clean_headers)}
        header_by_exact = {str(h): i for i, h in enumerate(clean_headers)}
        total_row = data_end + 1
        total_used = False

        for key, expr_raw in items:
            expr = str(expr_raw or "").strip()
            if not expr:
                continue
            if isinstance(key, str) and _CELL_REF_RE.fullmatch(key.strip()):
                c_idx, r_num = _parse_cell_ref(key.strip())
                formula_body = expr[1:].strip() if expr.startswith("=") else expr
                col_fmt = col_fmts.get(c_idx) or col_inferred_fmt.get(c_idx) or "dec2"
                s_idx = (
                    style_map.get(f"total_{col_fmt}", style_map["total_dec2"])
                    if r_num > data_end
                    else style_map.get(col_fmt, style_map["dec2"])
                )
                row_entries.setdefault(r_num, {})[c_idx] = _cell_xml(
                    f"{_col_letter(c_idx)}{r_num}", "formula", formula_body, s_idx
                )
                if r_num > data_end:
                    row_heights.setdefault(r_num, 22.0)
                widths[c_idx] = max(widths.get(c_idx, 8), 14)
                ncol = max(ncol, c_idx + 1)
                has_formula = True
                continue

            # Column-keyed formula placed on the summary/totals row.
            c_idx_opt: Optional[int] = None
            if isinstance(key, int) and 0 <= key < ncol:
                c_idx_opt = key
            elif isinstance(key, str):
                if key in header_by_exact:
                    c_idx_opt = header_by_exact[key]
                elif _norm_header(key) in header_by_norm:
                    c_idx_opt = header_by_norm[_norm_header(key)]
                elif re.fullmatch(r"[A-Za-z]{1,3}", key.strip()):
                    c_idx_opt = _col_index(key.strip())
            if c_idx_opt is None:
                raise ExcelOpError(f"Formula target {key!r} does not match a cell reference or column header.")

            col_l = _col_letter(c_idx_opt)
            up = expr.upper()
            if up in _AGG_FUNCS:
                fn = "AVERAGE" if up == "AVG" else up
                formula_body = f"{fn}({col_l}{data_start}:{col_l}{data_end})" if data_end >= data_start else "0"
            else:
                body = expr[1:].strip() if expr.startswith("=") else expr
                formula_body = body.format(col=col_l, first=data_start, last=max(data_end, data_start))

            col_fmt = col_fmts.get(c_idx_opt) or col_inferred_fmt.get(c_idx_opt) or "dec2"
            s_idx = style_map.get(f"total_{col_fmt}", style_map["total_dec2"])
            row_entries.setdefault(total_row, {})[c_idx_opt] = _cell_xml(
                f"{col_l}{total_row}", "formula", formula_body, s_idx
            )
            widths[c_idx_opt] = max(widths.get(c_idx_opt, 8), 14)
            total_used = True
            has_formula = True

        if total_used:
            row_heights[total_row] = 22.0
            if 0 not in row_entries[total_row]:
                row_entries[total_row][0] = _cell_xml(
                    f"A{total_row}", "text", "Total", style_map["total_text"]
                )
            for c in range(ncol):
                if c not in row_entries[total_row]:
                    row_entries[total_row][c] = _cell_xml(
                        f"{_col_letter(c)}{total_row}", "empty", "", style_map["total_text"]
                    )

    max_row = max(row_entries.keys(), default=header_row)
    dim_ref = f"A1:{_col_letter(ncol - 1)}{max_row}"

    cols_xml = "".join(
        f'<col min="{c + 1}" max="{c + 1}" width="{min(max(widths.get(c, 10) + 3, 10), 56)}" customWidth="1"/>'
        for c in range(ncol)
    )
    rows_xml: list[str] = []
    for r_num in sorted(row_entries.keys()):
        ht = row_heights.get(r_num)
        ht_attr = f' ht="{ht:.1f}" customHeight="1"' if ht else ""
        cells = "".join(row_entries[r_num][c] for c in sorted(row_entries[r_num].keys()))
        rows_xml.append(f'<row r="{r_num}"{ht_attr}>{cells}</row>')

    views_xml = (
        f'<sheetViews><sheetView workbookViewId="0">'
        f'<pane ySplit="{header_row}" topLeftCell="A{header_row + 1}" '
        f'activePane="bottomLeft" state="frozen"/>'
        f"</sheetView></sheetViews>"
    )
    auto_filter = (
        f'<autoFilter ref="A{header_row}:{_col_letter(ncol - 1)}{data_end}"/>'
        if data_end >= header_row + 1
        else ""
    )
    merge_xml = (
        f'<mergeCells count="{len(merges)}">'
        + "".join(f'<mergeCell ref="{m}"/>' for m in merges)
        + "</mergeCells>"
        if merges
        else ""
    )
    page_xml = (
        '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
        '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
    )
    return (
        f'{_XML_DECL}<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
        '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
        f'<dimension ref="{dim_ref}"/>'
        f"{views_xml}"
        f"<cols>{cols_xml}</cols>"
        f'<sheetData>{"".join(rows_xml)}</sheetData>'
        f"{auto_filter}"
        f"{merge_xml}"
        f"{page_xml}"
        "</worksheet>"
    )


def _ensure_styles_wired(
    zf: zipfile.ZipFile,
    rels_xml: str,
    ct_xml: str,
) -> tuple[str, str]:
    """Ensure xl/styles.xml is registered in workbook.xml.rels and [Content_Types].xml."""
    if "styles.xml" not in rels_xml:
        used_rids = {int(n) for n in re.findall(r'Id="rId(\d+)"', rels_xml)}
        next_rid = f"rId{max(used_rids | {0}) + 1}"
        rel_tag = f'<Relationship Id="{next_rid}" Type="{_STYLES_REL}" Target="styles.xml"/>'
        rels_xml = rels_xml.replace("</Relationships>", rel_tag + "</Relationships>")
    if "/xl/styles.xml" not in ct_xml:
        ov_tag = f'<Override PartName="/xl/styles.xml" ContentType="{_STYLES_CT}"/>'
        ct_xml = ct_xml.replace("</Types>", ov_tag + "</Types>")
    return rels_xml, ct_xml


def _set_full_calc_on_load(book_xml: str) -> str:
    if "<calcPr" in book_xml:
        if "fullCalcOnLoad=" in book_xml:
            return re.sub(r'fullCalcOnLoad="[^"]*"', 'fullCalcOnLoad="1"', book_xml, count=1)
        return book_xml.replace("<calcPr", '<calcPr fullCalcOnLoad="1"', 1)
    return book_xml.replace("</workbook>", '<calcPr fullCalcOnLoad="1"/></workbook>', 1)


def add_sheet_to_workbook(
    xlsx_bytes: bytes,
    sheet_name: str,
    headers: Sequence[Any],
    rows: Sequence[Any],
    *,
    title: Optional[str] = None,
    formulas: Optional[Mapping[Any, str] | Sequence[tuple[str, str]]] = None,
    number_formats: Optional[Mapping[Any, str]] = None,
    if_exists: str = "error",
) -> bytes:
    """Add a styled worksheet tab to an existing ``.xlsx`` workbook without breaking existing tabs.

    Preserves all existing sheets, formulas, charts, drawings, themes, and styles byte for byte.
    """
    if if_exists not in ("error", "rename", "replace"):
        raise ExcelOpError(f"Unsupported if_exists policy: {if_exists!r}")
    if not headers and rows and isinstance(rows[0], Mapping):
        headers = list(rows[0].keys())
    if not headers:
        raise ExcelOpError("At least one column header is required to add a worksheet.")

    base_name = sanitize_sheet_name(sheet_name)
    clean_headers = [str(h or "").strip() for h in headers]
    col_fmts, custom_codes = _resolve_col_formats(clean_headers, number_formats)

    with _open_zip(xlsx_bytes) as zf:
        existing_sheets = _workbook_sheets(zf)
        existing_names = {s["name"].lower(): s for s in existing_sheets}
        book_xml = zf.read("xl/workbook.xml").decode("utf-8")
        rels_xml = (
            zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
            if "xl/_rels/workbook.xml.rels" in zf.namelist()
            else f'{_XML_DECL}<Relationships xmlns="{_NS_PKG_REL}"></Relationships>'
        )
        ct_xml = (
            zf.read("[Content_Types].xml").decode("utf-8")
            if "[Content_Types].xml" in zf.namelist()
            else f'{_XML_DECL}<Types xmlns="{_NS_CT}"></Types>'
        )
        existing_styles = (
            zf.read("xl/styles.xml").decode("utf-8")
            if "xl/styles.xml" in zf.namelist()
            else None
        )
        all_names = set(zf.namelist())

        final_name = base_name
        replace_target: Optional[str] = None
        if final_name.lower() in existing_names:
            if if_exists == "error":
                raise ExcelOpError(f'Worksheet "{final_name}" already exists in the workbook.')
            if if_exists == "replace":
                replace_target = existing_names[final_name.lower()]["target"]
            else:
                suffix = 2
                while True:
                    tag = f" ({suffix})"
                    cand = f"{base_name[: 31 - len(tag)].rstrip()}{tag}"
                    if cand.lower() not in existing_names:
                        final_name = cand
                        break
                    suffix += 1

        new_styles_xml, style_map = _append_studio_styles(existing_styles, custom_codes)
        rels_xml, ct_xml = _ensure_styles_wired(zf, rels_xml, ct_xml)
        sheet_xml = _build_styled_worksheet_xml(
            clean_headers,
            rows,
            style_map,
            col_fmts,
            title=title,
            formulas=formulas,
        )

        updates: dict[str, bytes] = {"xl/styles.xml": new_styles_xml.encode("utf-8")}
        if replace_target:
            updates[replace_target] = sheet_xml.encode("utf-8")
        else:
            used_sheet_nums = {
                int(m.group(1))
                for n in all_names
                if (m := re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", n))
            }
            next_num = max(used_sheet_nums | {0}) + 1
            new_part = f"xl/worksheets/sheet{next_num}.xml"

            used_sids = {s["sheet_id"] for s in existing_sheets}
            next_sid = max(used_sids | {0}) + 1

            used_rids = {int(n) for n in re.findall(r'Id="rId(\d+)"', rels_xml)}
            next_rid = f"rId{max(used_rids | {0}) + 1}"

            sheet_tag = (
                f'<sheet name="{escape(final_name)}" sheetId="{next_sid}" r:id="{next_rid}"/>'
            )
            if "</sheets>" not in book_xml:
                raise ExcelOpError("Workbook XML is missing a </sheets> element.")
            book_xml = book_xml.replace("</sheets>", sheet_tag + "</sheets>", 1)

            rel_tag = (
                f'<Relationship Id="{next_rid}" Type="{_WORKSHEET_REL}" '
                f'Target="worksheets/sheet{next_num}.xml"/>'
            )
            rels_xml = rels_xml.replace("</Relationships>", rel_tag + "</Relationships>", 1)

            ov_tag = f'<Override PartName="/{new_part}" ContentType="{_SHEET_CT}"/>'
            if f'PartName="/{new_part}"' not in ct_xml:
                ct_xml = ct_xml.replace("</Types>", ov_tag + "</Types>", 1)

            updates[new_part] = sheet_xml.encode("utf-8")

        if formulas or "<f>" in sheet_xml:
            book_xml = _set_full_calc_on_load(book_xml)

        updates["xl/workbook.xml"] = book_xml.encode("utf-8")
        updates["xl/_rels/workbook.xml.rels"] = rels_xml.encode("utf-8")
        updates["[Content_Types].xml"] = ct_xml.encode("utf-8")

    return _rezip_with_updates(xlsx_bytes, updates)


def _extend_range_ref(ref: str, new_end_row: int, min_end_col: int) -> str:
    """Extend an A1:D5 range reference to cover at least ``new_end_row`` and ``min_end_col``."""
    if ":" not in ref:
        return ref
    left, right = ref.split(":", 1)
    try:
        lc, lr = _parse_cell_ref(left)
        rc, rr = _parse_cell_ref(right)
    except ExcelOpError:
        return ref
    end_c = max(rc, min_end_col)
    end_r = max(rr, new_end_row)
    return f"{_col_letter(lc)}{lr}:{_col_letter(end_c)}{end_r}"


def _extend_sheet_tables(
    zf: zipfile.ZipFile,
    sheet_part: str,
    old_last_row: int,
    new_last_row: int,
    max_col_idx: int,
) -> dict[str, bytes]:
    """Extend any Excel Table part linked from ``sheet_part`` whose ref ended at ``old_last_row``."""
    base_name = sheet_part.rsplit("/", 1)[-1]
    rels_part = f"xl/worksheets/_rels/{base_name}.rels"
    if rels_part not in zf.namelist():
        return {}
    rels_xml = zf.read(rels_part).decode("utf-8", "replace")
    updates: dict[str, bytes] = {}
    for m in re.finditer(r"<Relationship\s[^>]*/?>", rels_xml):
        attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(0)))
        if attrs.get("Type") != _TABLE_REL:
            continue
        tgt = unescape(attrs.get("Target", ""))
        part = tgt.lstrip("/")
        if part.startswith("../"):
            part = "xl/" + part[3:]
        elif not part.startswith("xl/"):
            part = "xl/worksheets/" + part
        if part not in zf.namelist():
            continue
        tbl_xml = zf.read(part).decode("utf-8")
        ref_m = re.search(r'\bref="([A-Z]+\d+:[A-Z]+\d+)"', tbl_xml)
        if not ref_m:
            continue
        old_ref = ref_m.group(1)
        _, old_end_r = _parse_cell_ref(old_ref.split(":", 1)[1])
        if old_end_r == old_last_row:
            new_ref = _extend_range_ref(old_ref, new_last_row, max_col_idx)
            tbl_xml = tbl_xml.replace(f'ref="{old_ref}"', f'ref="{new_ref}"')
            updates[part] = tbl_xml.encode("utf-8")
    return updates


def append_rows_to_sheet(
    xlsx_bytes: bytes,
    sheet_name: str,
    rows: Sequence[Any],
    *,
    match_headers: bool = True,
    create_if_missing: bool = True,
    headers: Optional[Sequence[Any]] = None,
    number_formats: Optional[Mapping[Any, str]] = None,
) -> bytes:
    """Append data rows to an existing worksheet (or create it if missing).

    Preserves all existing rows, formulas, formatting, charts, drawings, and other tabs.
    When ``rows`` are dicts and ``match_headers=True``, values are aligned to the sheet's
    existing column headers (case- and punctuation-insensitive).
    """
    if not rows:
        return xlsx_bytes

    with _open_zip(xlsx_bytes) as zf:
        sheets = _workbook_sheets(zf)
        target_sheet = next(
            (s for s in sheets if s["name"] == sheet_name),
            next((s for s in sheets if s["name"].lower() == sheet_name.strip().lower()), None),
        )
        if target_sheet is None:
            if not create_if_missing:
                raise ExcelOpError(f'Worksheet "{sheet_name}" does not exist in the workbook.')
            inferred_headers: list[Any] = list(headers) if headers else []
            if not inferred_headers:
                if isinstance(rows[0], Mapping):
                    seen_keys: list[str] = []
                    for r in rows:
                        if isinstance(r, Mapping):
                            for k in r.keys():
                                ks = str(k)
                                if ks not in seen_keys:
                                    seen_keys.append(ks)
                    inferred_headers = seen_keys
                elif isinstance(rows[0], Sequence) and not isinstance(rows[0], (str, bytes)):
                    inferred_headers = [f"Column {i + 1}" for i in range(len(rows[0]))]
                else:
                    inferred_headers = ["Value"]
            return add_sheet_to_workbook(
                xlsx_bytes,
                sheet_name,
                inferred_headers,
                rows,
                number_formats=number_formats,
            )

        part = target_sheet["target"]
        if not part or part not in zf.namelist():
            raise ExcelOpError(f'Worksheet part for "{sheet_name}" could not be found.')
        sheet_xml = zf.read(part).decode("utf-8")
        shared = _shared_strings(zf)
        grid, max_row = _read_sheet_grid(sheet_xml, shared)
        header_row, existing_headers = _detect_header_row(grid)
        active_headers = existing_headers if existing_headers else [str(h) for h in (headers or ())]

        # Capture existing column styles from the last populated data row (below header_row).
        col_styles: dict[int, int] = {}
        data_row_nums = [r for r in sorted(grid.keys()) if r > header_row]
        if data_row_nums:
            sample_row = grid[data_row_nums[-1]]
            for c_idx, info in sample_row.items():
                if info.get("style") is not None:
                    col_styles[c_idx] = info["style"]

        col_fmts, custom_codes = _resolve_col_formats(active_headers, number_formats)
        needs_studio_styles = bool(col_fmts or custom_codes)
        updates: dict[str, bytes] = {}
        style_map: dict[str, int] = {}
        if needs_studio_styles:
            existing_styles = (
                zf.read("xl/styles.xml").decode("utf-8")
                if "xl/styles.xml" in zf.namelist()
                else None
            )
            new_styles_xml, style_map = _append_studio_styles(existing_styles, custom_codes)
            updates["xl/styles.xml"] = new_styles_xml.encode("utf-8")
            rels_xml = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
            ct_xml = zf.read("[Content_Types].xml").decode("utf-8")
            rels_xml, ct_xml = _ensure_styles_wired(zf, rels_xml, ct_xml)
            updates["xl/_rels/workbook.xml.rels"] = rels_xml.encode("utf-8")
            updates["[Content_Types].xml"] = ct_xml.encode("utf-8")

        appended_xml: list[str] = []
        cur_row = max_row + 1
        max_col_idx = max((len(active_headers) - 1), 0)
        has_new_formula = False

        for raw_row in rows:
            vals = _normalize_row_values(raw_row, active_headers, match_headers=match_headers)
            cells: list[str] = []
            for c_idx, val in enumerate(vals):
                max_col_idx = max(max_col_idx, c_idx)
                explicit_fmt = col_fmts.get(c_idx)
                kind, wire, fmt_key = _classify_value(val, explicit_fmt)
                if kind == "formula":
                    has_new_formula = True
                if explicit_fmt and explicit_fmt in style_map:
                    s_idx: Optional[int] = style_map[explicit_fmt]
                elif c_idx in col_styles:
                    s_idx = col_styles[c_idx]
                else:
                    s_idx = None
                cells.append(_cell_xml(f"{_col_letter(c_idx)}{cur_row}", kind, wire, s_idx))
            appended_xml.append(f'<row r="{cur_row}">{"".join(cells)}</row>')
            cur_row += 1

        new_last_row = cur_row - 1
        joined = "".join(appended_xml)
        if "</sheetData>" in sheet_xml:
            sheet_xml = sheet_xml.replace("</sheetData>", joined + "</sheetData>", 1)
        elif "<sheetData/>" in sheet_xml:
            sheet_xml = sheet_xml.replace("<sheetData/>", f"<sheetData>{joined}</sheetData>", 1)
        else:
            raise ExcelOpError(f'Worksheet "{sheet_name}" has no <sheetData> element.')

        # Extend <dimension ref="..."/> and <autoFilter ref="..."/> cleanly.
        sheet_xml = re.sub(
            r'(<dimension\s[^>]*\bref=")([^"]+)(")',
            lambda m: m.group(1) + _extend_range_ref(m.group(2), new_last_row, max_col_idx) + m.group(3),
            sheet_xml,
            count=1,
        )
        sheet_xml = re.sub(
            r'(<autoFilter\s[^>]*\bref=")([^"]+)(")',
            lambda m: m.group(1) + _extend_range_ref(m.group(2), new_last_row, max_col_idx) + m.group(3),
            sheet_xml,
            count=1,
        )
        updates[part] = sheet_xml.encode("utf-8")

        # Extend any structured Excel Table ending at max_row.
        updates.update(_extend_sheet_tables(zf, part, max_row, new_last_row, max_col_idx))

        if has_new_formula:
            book_xml = zf.read("xl/workbook.xml").decode("utf-8")
            updates["xl/workbook.xml"] = _set_full_calc_on_load(book_xml).encode("utf-8")

    return _rezip_with_updates(xlsx_bytes, updates)


def _insert_or_replace_cell_in_sheet(
    sheet_xml: str,
    cell_ref: str,
    new_value: Any,
    *,
    allow_formula_overwrite: bool = False,
) -> tuple[str, bool]:
    """Update a single cell in ``sheet_xml``, preserving its style and all other cells.

    Returns ``(updated_sheet_xml, wrote_formula)``.
    """
    col_idx, row_num = _parse_cell_ref(cell_ref)
    clean_ref = f"{_col_letter(col_idx)}{row_num}"
    kind, wire, _fmt = _classify_value(new_value)

    cell_re = re.compile(rf'<c\s[^>]*\br="{clean_ref}"[^>]*?(?:/>|>.*?</c>)', re.S)
    m_cell = cell_re.search(sheet_xml)
    if m_cell:
        old_xml = m_cell.group(0)
        if not allow_formula_overwrite and ("<f>" in old_xml or "<f " in old_xml):
            raise ExcelOpError(f"Cell {clean_ref} holds a formula and cannot be overwritten without allow_formula_overwrite=True.")
        s_m = re.search(r'\bs="(\d+)"', old_xml)
        s_idx = int(s_m.group(1)) if s_m else None
        replacement = _cell_xml(clean_ref, kind, wire, s_idx)
        return sheet_xml[: m_cell.start()] + replacement + sheet_xml[m_cell.end() :], (kind == "formula")

    # Cell does not exist yet: insert into its row in column order, or add the row.
    new_cell = _cell_xml(clean_ref, kind, wire, None)
    row_re = re.compile(rf'(<row\s[^>]*\br="{row_num}"[^>]*>)(.*?)(</row>)', re.S)
    m_row = row_re.search(sheet_xml)
    if m_row:
        inner = m_row.group(2)
        inserted = False
        out_parts: list[str] = []
        pos = 0
        for cm in re.finditer(r'<c\s[^>]*\br="([A-Z]+\d+)"[^>]*?(?:/>|>.*?</c>)', inner, re.S):
            existing_col, _ = _parse_cell_ref(cm.group(1))
            if not inserted and existing_col > col_idx:
                out_parts.append(inner[pos : cm.start()])
                out_parts.append(new_cell)
                out_parts.append(cm.group(0))
                pos = cm.end()
                inserted = True
        out_parts.append(inner[pos:])
        if not inserted:
            out_parts.append(new_cell)
        new_row_xml = m_row.group(1) + "".join(out_parts) + m_row.group(3)
        return sheet_xml[: m_row.start()] + new_row_xml + sheet_xml[m_row.end() :], (kind == "formula")

    # Row does not exist yet: insert in row order inside <sheetData>.
    new_row = f'<row r="{row_num}">{new_cell}</row>'
    sd_m = re.search(r"(<sheetData[^>]*>)(.*?)(</sheetData>)", sheet_xml, re.S)
    if not sd_m:
        raise ExcelOpError("Worksheet XML has no <sheetData> section.")
    body = sd_m.group(2)
    insert_at: Optional[int] = None
    for rm in re.finditer(r'<row\s[^>]*\br="(\d+)"[^>]*?(?:/>|>.*?</row>)', body, re.S):
        if int(rm.group(1)) > row_num:
            insert_at = rm.start()
            break
    new_body = (body[:insert_at] + new_row + body[insert_at:]) if insert_at is not None else (body + new_row)
    updated = sheet_xml[: sd_m.start()] + sd_m.group(1) + new_body + sd_m.group(3) + sheet_xml[sd_m.end() :]
    return updated, (kind == "formula")


def _parse_sheet_and_cell(raw_ref: str) -> tuple[Optional[str], str]:
    text = raw_ref.strip()
    if "!" in text:
        s_part, c_part = text.rsplit("!", 1)
        s_clean = s_part.strip()
        if s_clean.startswith("'") and s_clean.endswith("'") and len(s_clean) >= 2:
            s_clean = s_clean[1:-1].replace("''", "'")
        return s_clean, c_part.strip().replace("$", "").upper()
    return None, text.replace("$", "").upper()


def update_named_cells(
    xlsx_bytes: bytes,
    updates: Mapping[str, Any],
    *,
    sheet_name: Optional[str] = None,
    allow_formula_overwrite: bool = False,
) -> bytes:
    """Update workbook named cells (``<definedNames>``) or ``Sheet!A1`` / ``A1`` references.

    Preserves cell formatting, existing formulas, charts, drawings, and untouched tabs.
    """
    if not updates:
        return xlsx_bytes

    with _open_zip(xlsx_bytes) as zf:
        sheets = _workbook_sheets(zf)
        by_name = {s["name"]: s for s in sheets}
        by_lower = {s["name"].lower(): s for s in sheets}
        defined = _defined_names(zf)
        defined_lower = {k.lower(): v for k, v in defined.items()}

        sheet_xml_cache: dict[str, str] = {}
        wrote_formula = False

        for key, value in updates.items():
            target_sheet_name: Optional[str] = None
            cell_ref = ""

            if key in defined:
                target_sheet_name, cell_ref = _parse_sheet_and_cell(defined[key])
            elif key.lower() in defined_lower:
                target_sheet_name, cell_ref = _parse_sheet_and_cell(defined_lower[key.lower()])
            elif "!" in key:
                target_sheet_name, cell_ref = _parse_sheet_and_cell(key)
            elif sheet_name and _CELL_REF_RE.fullmatch(key.strip()):
                target_sheet_name = sheet_name
                cell_ref = key.strip().replace("$", "").upper()
            else:
                raise ExcelOpError(
                    f'Named cell "{key}" was not found in workbook defined names (pass Sheet!A1 or sheet_name).'
                )

            if not _CELL_REF_RE.fullmatch(cell_ref):
                raise ExcelOpError(f'Named target "{key}" resolves to "{cell_ref}", which is not a single cell.')
            if not target_sheet_name:
                if sheet_name:
                    target_sheet_name = sheet_name
                elif sheets:
                    target_sheet_name = sheets[0]["name"]
                else:
                    raise ExcelOpError("Workbook has no sheets.")

            sheet_entry = by_name.get(target_sheet_name) or by_lower.get(target_sheet_name.lower())
            if sheet_entry is None:
                raise ExcelOpError(f'Worksheet "{target_sheet_name}" does not exist in the workbook.')
            part = sheet_entry["target"]
            if part not in sheet_xml_cache:
                sheet_xml_cache[part] = zf.read(part).decode("utf-8")

            sheet_xml_cache[part], is_f = _insert_or_replace_cell_in_sheet(
                sheet_xml_cache[part],
                cell_ref,
                value,
                allow_formula_overwrite=allow_formula_overwrite,
            )
            wrote_formula = wrote_formula or is_f

        part_updates: dict[str, bytes] = {p: xml.encode("utf-8") for p, xml in sheet_xml_cache.items()}
        if wrote_formula:
            book_xml = zf.read("xl/workbook.xml").decode("utf-8")
            part_updates["xl/workbook.xml"] = _set_full_calc_on_load(book_xml).encode("utf-8")

    return _rezip_with_updates(xlsx_bytes, part_updates)


def inspect_workbook(xlsx_bytes: bytes) -> dict[str, Any]:
    """Return a structured summary of an ``.xlsx`` workbook (sheets, headers, rows, formulas, names)."""
    with _open_zip(xlsx_bytes) as zf:
        shared = _shared_strings(zf)
        sheets_meta: list[dict[str, Any]] = []
        for entry in _workbook_sheets(zf):
            part = entry["target"]
            if not part or part not in zf.namelist():
                continue
            xml = zf.read(part).decode("utf-8", "replace")
            grid, max_row = _read_sheet_grid(xml, shared)
            header_row, headers = _detect_header_row(grid)
            formula_count = sum(
                1
                for r_cells in grid.values()
                for info in r_cells.values()
                if info.get("formula")
            )
            data_rows = max(0, sum(1 for r in grid.keys() if r > header_row)) if header_row else len(grid)
            sheets_meta.append({
                "name": entry["name"],
                "header_row": header_row,
                "headers": headers,
                "row_count": data_rows,
                "max_row": max_row,
                "formula_count": formula_count,
            })
        chart_parts = [n for n in zf.namelist() if re.fullmatch(r"xl/charts/chart\d+\.xml", n)]
        return {
            "sheets": sheets_meta,
            "sheet_names": [s["name"] for s in sheets_meta],
            "defined_names": _defined_names(zf),
            "chart_count": len(chart_parts),
        }


__all__ = [
    "ExcelOpError",
    "add_sheet_to_workbook",
    "append_rows_to_sheet",
    "inspect_workbook",
    "sanitize_sheet_name",
    "update_named_cells",
]
