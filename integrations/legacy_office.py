"""Stdlib readers for legacy binary Office files: Excel 97-2003 ``.xls`` (BIFF8) and
Word 97-2003 ``.doc``, both OLE compound files (``operating.msg_reader.Cfb``, the one
compound-file reader of the repository).

* ``.xls``: the ``Workbook`` stream's records are read sheet by sheet into the same
  transcription as an ``.xlsx`` (``integrations.xlsx_extract``: ``[Sheet: name]`` then
  ``A1=v | B1=w`` rows, the same per-sheet row and cell bounds, the same fair share of
  the text bound and the same visible truncation markers). Cell values are the stored
  ones (shared strings, labels, numbers, booleans and errors); a formula cell shows its
  cached value as ``FORMULA(not transcribed); cached=<value>`` because BIFF stores the
  formula as parsed tokens: it is never decompiled nor evaluated (warning
  ``xls_formulas_not_transcribed``). Chart, macro and VBA sheets hold no cells and are
  named as not read.
* ``.doc``: the text of every story (the body, then headers, footnotes, comments,
  endnotes and text boxes after a marker line) from the piece table (``Clx``) of the
  ``WordDocument`` stream, field instructions dropped and field results kept.

An encrypted file, a format version before BIFF8 / Word 97, or a malformed structure
is ``unsupported`` with a typed warning, never guessed. Nothing here fetches, writes
or evaluates anything; the output is a pure function of the bytes.
"""

from __future__ import annotations

import struct
from typing import Any

from operating.msg_reader import CFB_MAGIC, Cfb
from .xlsx_extract import (MAX_CELL_CHARS, MAX_CELLS_PER_SHEET, assemble, sheet_plan,
                           rows_budget, _cut, _letters)

MAX_STREAM_BYTES = 64_000_000
# BIFF records read per workbook (all sheets together): bounds the time of a workbook
# whatever its records claim (a sheet without its end record, sheets pointing at one
# offset); a sheet cut by it ends with a truncation marker.
MAX_RECORDS = 2_000_000
MAX_SHARED_STRINGS = 1_000_000
MAX_SHARED_CHARS = 50_000_000
_CFB_ERRORS = (ValueError, OverflowError, struct.error, IndexError, KeyError)

# BIFF8 record types
BOF, EOF, CONTINUE, FILEPASS, BOUNDSHEET, SST = 0x0809, 0x000A, 0x003C, 0x002F, 0x0085, 0x00FC
LABELSST, LABEL, RSTRING, NUMBER, RK, MULRK = 0x00FD, 0x0204, 0x00D6, 0x0203, 0x027E, 0x00BD
FORMULA, STRING, BOOLERR = 0x0006, 0x0207, 0x0205
_ERRORS = {0x00: "#NULL!", 0x07: "#DIV/0!", 0x0F: "#VALUE!", 0x17: "#REF!", 0x1D: "#NAME?",
           0x24: "#NUM!", 0x2A: "#N/A", 0x2B: "#GETTING_DATA"}


class LegacyFormatError(Exception):
    """The bytes are not a readable file of the format; ``str()`` is the warning."""


def _stream(content: bytes, *names: str) -> bytes:
    if not content.startswith(CFB_MAGIC):
        raise LegacyFormatError("not_compound_file")
    try:
        cfb = Cfb(content)
        for name in names:
            index = cfb.child(0, name)
            if index is not None:
                return cfb.stream(index, MAX_STREAM_BYTES)
    except OverflowError:
        raise LegacyFormatError("stream_exceeds_bound") from None
    except _CFB_ERRORS:
        raise LegacyFormatError("compound_file_invalid") from None
    if any(cfb.entries[i][0] == "EncryptedPackage" for i in cfb.children(0)):
        raise LegacyFormatError("encrypted")
    raise LegacyFormatError("stream_missing")


# ------------------------------------------------------------------ BIFF8 (.xls)

def _records(data: bytes, start: int = 0):
    """``(offset, type, body)`` of each record from ``start`` on."""
    position = start
    while position + 4 <= len(data):
        kind, size = struct.unpack_from("<HH", data, position)
        body = data[position + 4:position + 4 + size]
        if len(body) != size:
            raise LegacyFormatError("xls_record_truncated")
        yield position, kind, body
        position += 4 + size


class _Segments:
    """A record body and its CONTINUE records read as one, where a string's character
    data that crosses into a CONTINUE restarts with its own high-byte flag."""

    def __init__(self, parts: list[bytes]) -> None:
        self.parts, self.index, self.at = parts, 0, 0

    def _advance(self) -> None:
        while self.index < len(self.parts) and self.at >= len(self.parts[self.index]):
            self.index, self.at = self.index + 1, 0
        if self.index >= len(self.parts):
            raise LegacyFormatError("xls_string_truncated")

    def raw(self, count: int) -> bytes:
        out = bytearray()
        while count:
            self._advance()
            part = self.parts[self.index]
            take = min(count, len(part) - self.at)
            out += part[self.at:self.at + take]
            self.at += take
            count -= take
        return bytes(out)

    def chars(self, count: int, high: bool) -> str:
        out: list[str] = []
        while count:
            if self.at >= len(self.parts[self.index]):
                self.index, self.at = self.index + 1, 0
                if self.index >= len(self.parts):
                    raise LegacyFormatError("xls_string_truncated")
                high = bool(self.parts[self.index][0] & 1)
                self.at = 1
                continue
            part = self.parts[self.index]
            width = 2 if high else 1
            take = min(count, (len(part) - self.at) // width)
            if take == 0:
                raise LegacyFormatError("xls_string_truncated")
            raw = part[self.at:self.at + take * width]
            out.append(raw.decode("utf-16-le" if high else "latin-1", "replace"))
            self.at += take * width
            count -= take
        return "".join(out)

    def string(self, length_bytes: int = 2) -> str:
        count = int.from_bytes(self.raw(length_bytes), "little")
        flags = self.raw(1)[0]
        runs = struct.unpack("<H", self.raw(2))[0] if flags & 0x08 else 0
        ext = struct.unpack("<i", self.raw(4))[0] if flags & 0x04 else 0
        text = self.chars(count, bool(flags & 1))
        self.raw(4 * runs)
        self.raw(max(0, ext))
        return text


def _short_string(body: bytes, offset: int, length_bytes: int = 1) -> str:
    return _Segments([body[offset:]]).string(length_bytes)


def _number(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return repr(value)
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _rk(raw: int) -> str:
    if raw & 0x02:
        value: float = raw >> 2 if not raw & 0x80000000 else (raw >> 2) - (1 << 30)
    else:
        value = struct.unpack("<d", struct.pack("<Q", (raw & 0xFFFFFFFC) << 32))[0]
    if raw & 0x01:
        value = value / 100
    return _number(float(value))


def _formula_value(body: bytes) -> tuple[str | None, bool]:
    """(cached value, value in the next STRING record) of a FORMULA record."""
    raw = body[6:14]
    if raw[6:8] != b"\xff\xff":
        return _number(struct.unpack("<d", raw)[0]), False
    kind = raw[0]
    if kind == 0:
        return None, True
    if kind == 1:
        return ("TRUE" if raw[2] else "FALSE"), False
    if kind == 2:
        return _ERRORS.get(raw[2], "#ERROR"), False
    return "", False


class _Budget:
    """The records a workbook may still read (``MAX_RECORDS``)."""

    def __init__(self) -> None:
        self.left = MAX_RECORDS


def _sheet_cells(data: bytes, start: int, end: int, shared: list[str], budget: _Budget
                 ) -> tuple[dict[int, list[tuple[int, str, bool]]], str | None]:
    """The sheet's populated cells by row (``(column, value, formula)``) and the bound
    that cut it (``cells``, ``records``), if one did. It ends at its EOF record, at
    ``end`` (the next sheet's start) or at the record bound, whichever comes first."""
    rows: dict[int, list[tuple[int, str, bool]]] = {}
    count, pending = 0, None
    first = True
    for offset, kind, body in _records(data, start):
        if offset >= end:
            break
        budget.left -= 1
        if budget.left < 0:
            return rows, "records"
        if pending is not None and kind not in (STRING, 0x0221, 0x04BC, 0x0236):
            # A string result whose STRING record never came: kept, its value missing.
            rows.setdefault(pending[0], []).append((pending[1], "", True))
            pending = None
        if first:
            if kind != BOF:
                raise LegacyFormatError("xls_sheet_invalid")
            first = False
            continue
        found: list[tuple[int, int, str, bool]] = []
        if kind == EOF:
            break
        if kind == LABELSST and len(body) >= 10:
            row, col, _xf, index = struct.unpack_from("<HHHI", body)
            if index >= len(shared):
                raise LegacyFormatError("xls_shared_string_invalid")
            found.append((row, col, shared[index], False))
        elif kind in (LABEL, RSTRING) and len(body) >= 8:
            row, col = struct.unpack_from("<HH", body)
            found.append((row, col, _short_string(body, 6, 2), False))
        elif kind == NUMBER and len(body) >= 14:
            row, col = struct.unpack_from("<HH", body)
            found.append((row, col, _number(struct.unpack_from("<d", body, 6)[0]), False))
        elif kind == RK and len(body) >= 10:
            row, col, _xf, raw = struct.unpack_from("<HHHI", body)
            found.append((row, col, _rk(raw), False))
        elif kind == MULRK and len(body) >= 6:
            row, first_col = struct.unpack_from("<HH", body)
            for index in range((len(body) - 6) // 6):
                raw = struct.unpack_from("<I", body, 4 + index * 6 + 2)[0]
                found.append((row, first_col + index, _rk(raw), False))
        elif kind == BOOLERR and len(body) >= 8:
            row, col = struct.unpack_from("<HH", body)
            value = (_ERRORS.get(body[6], "#ERROR") if body[7] else
                     ("TRUE" if body[6] else "FALSE"))
            found.append((row, col, value, False))
        elif kind == FORMULA and len(body) >= 20:
            row, col = struct.unpack_from("<HH", body)
            value, later = _formula_value(body)
            if later:
                pending = (row, col)
            else:
                found.append((row, col, value or "", True))
        elif kind == STRING and pending is not None:
            found.append((*pending, _short_string(body, 0, 2), True))
            pending = None
        for row, col, value, formula in found:
            if not value and not formula:
                continue
            count += 1
            if count > MAX_CELLS_PER_SHEET:
                return rows, "cells"
            rows.setdefault(row, []).append((col, _cut(value), formula))
    if pending is not None:
        rows.setdefault(pending[0], []).append((pending[1], "", True))
    return rows, None


def extract_xls_text(content: bytes, *, max_text: int) -> tuple[
        str, str, tuple[str, ...], tuple[tuple[int, int], ...], tuple[dict[str, Any], ...]]:
    """Text, status, warnings, review ranges and sheet spans of an ``.xls`` (BIFF8), in
    the shape of ``xlsx_extract.extract_xlsx_text``; ``unsupported`` with a typed
    ``xls_*`` warning when it cannot be read."""
    try:
        data = _stream(content, "Workbook", "Book")
        try:
            return _xls(data, max_text)
        except (struct.error, IndexError):
            raise LegacyFormatError("xls_record_invalid") from None
    except LegacyFormatError as error:
        code = str(error)
        return "", "unsupported", (code if code.startswith("xls_") else f"xls_{code}",), (), ()


def _xls(data: bytes, max_text: int) -> tuple[
        str, str, tuple[str, ...], tuple[tuple[int, int], ...], tuple[dict[str, Any], ...]]:
    records = _records(data)
    first = next(records, None)
    if first is None or first[1] != BOF or len(first[2]) < 4 or \
            struct.unpack_from("<H", first[2])[0] != 0x0600:
        raise LegacyFormatError("xls_version_unsupported")
    shared: list[str] = []
    entries: list[tuple[str, int, int]] = []   # (name, sheet type, BOF offset)
    sst: list[bytes] | None = None
    for _offset, kind, body in records:
        if sst is not None and kind != CONTINUE:
            shared = _sst(sst)
            sst = None
        if kind == FILEPASS:
            raise LegacyFormatError("xls_encrypted")
        if kind == BOUNDSHEET and len(body) >= 8:
            position, _state, sheet_type = struct.unpack_from("<IBB", body)
            entries.append((_short_string(body, 6, 1) or "(unnamed)", sheet_type, position))
        elif kind == SST:
            sst = [body]
        elif kind == CONTINUE and sst is not None:
            sst.append(body)
        elif kind == EOF:
            break
    if sst is not None:
        shared = _sst(sst)
    sheets, skipped = sheet_plan([name for name, _type, _at in entries])
    budget = rows_budget(sheets, skipped, max_text)
    formulas = False
    starts = sorted({position for _n, _t, position in entries})
    records = _Budget()
    for (name, sheet_type, position), sheet in zip(entries, sheets):
        if sheet_type != 0:
            sheet.drop("chart")
            continue
        if records.left <= 0:
            sheet.drop("records")
            continue
        end = next((s for s in starts if s > position), len(data))
        try:
            cells, cut = _sheet_cells(data, position, end, shared, records)
        except (LegacyFormatError, struct.error, IndexError):
            sheet.drop("invalid")
            continue
        for row in sorted(cells):
            found = sorted(cells[row], key=lambda cell: cell[0])
            formulas = formulas or any(formula for _, _, formula in found)
            transcribed = [(f"{_letters(col + 1)}{row + 1}", value,
                            "not transcribed" if formula else None)
                           for col, value, formula in found]
            if not sheet.add(row + 1, transcribed, budget):
                break
        if cut and sheet.cut is None:
            sheet.cut = cut
    text, status, warnings, ranges, spans = assemble(sheets, skipped, max_text, "xls")
    if formulas and status == "extracted":
        warnings = tuple(w for w in warnings if w != "xls_formulas_not_recalculated") + (
            "xls_formulas_not_transcribed",)
    return text, status, warnings, ranges, spans


def _sst(parts: list[bytes]) -> list[str]:
    segments = _Segments(parts)
    _total, unique = struct.unpack("<II", segments.raw(8))
    if unique > MAX_SHARED_STRINGS:
        raise LegacyFormatError("xls_shared_strings_exceed_bound")
    out, chars = [], 0
    for _ in range(unique):
        value = segments.string()[:MAX_CELL_CHARS + 1]
        chars += len(value)
        if chars > MAX_SHARED_CHARS:
            raise LegacyFormatError("xls_shared_strings_exceed_bound")
        out.append(value)
    return out


# ------------------------------------------------------------------ Word 97 (.doc)

_STORIES = ("footnotes", "headers", "macros", "comments", "endnotes", "text boxes",
            "header text boxes")
OTHER_STORIES = "[Headers, footnotes, comments, endnotes and text boxes]"


def _clean(text: str) -> str:
    """Word's control characters as text: paragraph and line marks as newlines, a
    table cell mark as a tab, field instructions dropped (their results kept), object
    anchors and other controls dropped."""
    out: list[str] = []
    fields: list[bool] = []   # per open field: past its separator (showing its result)
    for char in text:
        code = ord(char)
        if code == 0x13:
            fields.append(False)
            continue
        if code == 0x14:
            if fields:
                fields[-1] = True
            continue
        if code == 0x15:
            if fields:
                fields.pop()
            continue
        if fields and not all(fields):
            continue
        if char in "\r\x0b\x0c\x0e":
            out.append("\n")
        elif code == 0x07:
            out.append("\t")
        elif code == 0x1E:
            out.append("-")
        elif code < 0x20 and char not in "\t\n":
            continue
        else:
            out.append(char)
    return "".join(out)


def extract_doc_text(content: bytes, *, max_text: int | None = None
                     ) -> tuple[str, str, tuple[str, ...]]:
    """(text, status, warnings) of a Word 97-2003 ``.doc``; ``unsupported`` with a typed
    ``doc_*`` warning when it cannot be read."""
    try:
        word = _stream(content, "WordDocument")
        try:
            text, status, warnings = _doc(content, word)
        except (struct.error, IndexError):
            raise LegacyFormatError("doc_structure_invalid") from None
        if max_text is not None and len(text) > max_text:
            marker = (f"\n[Document truncated: characters past {{shown}} of {len(text)} "
                      "not read (text bound)]")
            shown = max_text - len(marker.format(shown=max_text))
            if shown <= 0:
                raise LegacyFormatError("doc_text_bound_too_small")
            text = text[:shown] + marker.format(shown=shown)
            warnings += ("doc_text_truncated",)
        return text, status, warnings
    except LegacyFormatError as error:
        code = str(error)
        return "", "unsupported", (code if code.startswith("doc_") else f"doc_{code}",)


def _doc(content: bytes, word: bytes) -> tuple[str, str, tuple[str, ...]]:
    ident, version = struct.unpack_from("<HH", word, 0)
    if ident != 0xA5EC:
        raise LegacyFormatError("doc_not_word")
    if version < 0x00C1:
        raise LegacyFormatError("doc_version_unsupported")
    flags = struct.unpack_from("<H", word, 0x0A)[0]
    if flags & 0x0100:
        raise LegacyFormatError("doc_encrypted")
    table = _stream(content, "1Table" if flags & 0x0200 else "0Table")
    csw = struct.unpack_from("<H", word, 32)[0]
    lw_at = 34 + csw * 2
    cslw = struct.unpack_from("<H", word, lw_at)[0]
    lw = lw_at + 2
    counts = struct.unpack_from("<11i", word, lw)
    if cslw < 11:
        raise LegacyFormatError("doc_structure_invalid")
    body = counts[3]
    others = [max(0, c) for c in counts[4:11]]
    blob = lw + cslw * 4 + 2
    fc_clx, lcb_clx = struct.unpack_from("<II", word, blob + 33 * 8)
    clx = table[fc_clx:fc_clx + lcb_clx]
    if len(clx) != lcb_clx or body < 0:
        raise LegacyFormatError("doc_structure_invalid")
    at = 0
    while at < len(clx) and clx[at] == 0x01:   # Prc: property modifiers, skipped
        at += 3 + struct.unpack_from("<h", clx, at + 1)[0]
    if at >= len(clx) or clx[at] != 0x02:
        raise LegacyFormatError("doc_structure_invalid")
    size = struct.unpack_from("<I", clx, at + 1)[0]
    plc = clx[at + 5:at + 5 + size]
    pieces = (len(plc) - 4) // 12
    if pieces <= 0 or len(plc) != pieces * 12 + 4:
        raise LegacyFormatError("doc_structure_invalid")
    cps = struct.unpack_from(f"<{pieces + 1}I", plc)
    wanted = body + sum(others)
    chunks: list[str] = []
    for index in range(pieces):
        start, end = cps[index], cps[index + 1]
        if start >= wanted:
            break
        end = min(end, wanted)
        if end <= start:
            continue
        raw_fc = struct.unpack_from("<I", plc, (pieces + 1) * 4 + index * 8 + 2)[0]
        compressed = bool(raw_fc & 0x40000000)
        fc = raw_fc & 0x3FFFFFFF
        if compressed:
            raw = word[fc // 2:fc // 2 + (end - start)]
            if len(raw) != end - start:
                raise LegacyFormatError("doc_structure_invalid")
            chunks.append(raw.decode("cp1252", "replace"))
        else:
            raw = word[fc:fc + 2 * (end - start)]
            if len(raw) != 2 * (end - start):
                raise LegacyFormatError("doc_structure_invalid")
            chunks.append(raw.decode("utf-16-le", "replace"))
    whole = "".join(chunks)
    text = _clean(whole[:body]).strip()
    rest = _clean(whole[body:]).strip()
    if rest:
        text = f"{text}\n\n{OTHER_STORIES}\n{rest}" if text else f"{OTHER_STORIES}\n{rest}"
    return text, ("extracted" if text.strip() else "no_text"), ()


def excel_named(kind: str, filename: str) -> bool:
    """A legacy Excel type, or a generic type and a legacy Excel name."""
    return kind in ("application/vnd.ms-excel", "application/msexcel",
                    "application/x-msexcel") or (
        kind == "application/octet-stream" and filename.lower().endswith((".xls", ".xlt")))


def is_xls(kind: str, filename: str, content: bytes) -> bool:
    """A legacy workbook by type or name, and its bytes a compound file."""
    return content.startswith(CFB_MAGIC) and excel_named(kind, filename)


def is_misnamed_xlsx(kind: str, filename: str, content: bytes) -> bool:
    """An OOXML workbook saved under a legacy Excel type or name (its bytes a ZIP)."""
    return content.startswith(b"PK\x03\x04") and excel_named(kind, filename)


def is_doc(kind: str, filename: str, content: bytes) -> bool:
    """A legacy Word document by type or name, and its bytes a compound file."""
    name = filename.lower()
    return content.startswith(CFB_MAGIC) and (
        kind in ("application/msword", "application/vnd.ms-word")
        or (kind == "application/octet-stream" and name.endswith((".doc", ".dot"))))


__all__ = ["LegacyFormatError", "OTHER_STORIES", "excel_named", "extract_doc_text",
           "extract_xls_text", "is_doc", "is_misnamed_xlsx", "is_xls"]
