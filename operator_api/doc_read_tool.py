"""``plx.read_document``: the Ask reads one document whole, from its original, not from
the text a capture or the question's attachment reader cut (``operating.doc_read``): every
row of a big workbook (dates as dates), every page of a PDF (a scanned page read by local
OCR), a Word file's tables, headers and footers, a deck's slides and notes.

What it reads, and as whom:

* ``sharepoint://<drive>/<item>@<version>`` (a ``plx.search`` hit): only when the reader's
  own host opens that citation now (``DocumentEditMixin._pw_doc_bytes``: the host's gates
  decide, then that version's original from the SharePoint capture store; a version the
  store no longer holds is not read here; one whose original was dropped is read from the
  text the capture extracted from it, and says so: ``captured_text_note``);
* ``graph-mail://<mailbox>/<message>#attachment=<id>&sha256=<digest>`` (a mail attachment, byte-
  pinned): the same host gate; its original when held, else the text extracted from it;
* ``file:<id>``: the person's own file (``_pw_file``: a file they attached to an Ask, or one
  the Ask made or edited for them; another reader's id is none).

Before any of it reaches the model the whole text (the name too) is checked as every
document read is (``_pw_withheld``): Freightcom named anywhere, an excluded or undecidable
person's address or id, or the exclusion list not loaded: nothing of it is answered. A
reading is kept in memory for the reader for ``KEEP_SECONDS`` so the next page or search
does not parse (or OCR) the same bytes again; the citation is opened and the whole text
checked again on every call. Nothing is written anywhere.

Arguments: ``citation``; then ``offset`` (a page of ``doc_read.WINDOW`` characters from it,
``chars`` fewer or up to ``doc_read.MAX_WINDOW``) or ``find`` (the lines holding every
word, each with its offset and sheet/page/slide). Without either: the first page and the
document's outline (its sheets with row counts, pages or slides). Unknown arguments fail
closed. Evidence: isolated-fixture (``operator_api/tests/test_doc_read_tool.py``)."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Mapping, Optional

from operating import doc_read

from . import ocr_background

NAME = "plx.read_document"
KEEP_SECONDS = 900
# a file whose original is not held is read from its captured text: its name carries this
# for the read, which the answer then states
CAPTURED_SUFFIX = ".captured.txt"
KEEP_DOCS = 6
KEEP_CHARS = 12_000_000

SPEC = {
    "name": NAME,
    "description": (
        "Read one document to the end from its original: every sheet and row of a "
        "workbook (dates shown as dates), every page of a PDF (scanned pages read by OCR), "
        "a Word file's tables, headers and footers, a deck's slides and notes. Use it when "
        "plx.open's text is cut or a sheet says truncated, when an attached file's text was "
        "cut, or when the answer needs rows or pages further in. citation: a sharepoint:// "
        "or mail-attachment (graph-mail://...#attachment=...&sha256=...) citation from "
        "plx.search or a file: citation of the person's own file. With no "
        "offset or find: the first part and the outline (sheets with row counts, pages). "
        "offset: the next part (next_offset of the last read); find: the lines holding all "
        "the words (an item code, a name, a PO), each with its offset and sheet or page. "
        "Rows read 'r<row> | cell | cell'. To count, total or filter a sheet or CSV, send "
        "where (conditions {column: a header or a letter, op, value}, all must hold; none: "
        "every row) and sum (columns to total): it scans every row and answers matched, "
        "sums and the first 100 rows; sheet names one when there are several, header_row "
        "when the header is not the first row. Never count or total rows from one part. "
        "table: true makes the sheet (after where, if sent) a result you can cite and compute "
        "from, like a plx.sql result: it returns result doc://<id>, columns, rows "
        "(each with its cite doc://<id>#row=<sheet row>), a profile over EVERY row, and "
        "next_skip (send skip to read on: the same result grows). Write its figures as "
        "{{sum(doc://<id>.<column>, CAD)}}, {{doc://<id>#row=7.<column>}}. A column of "
        "amounts in two currencies is not summed and says so. A Word file's tables are sheets "
        "named table 1, table 2; any other text (a PDF, a scan, a deck, a Word file's "
        "paragraphs) is the sheet lines (columns part, text, how_sure: ocr, text layer or "
        "document text; the row number is the line's number): name it with sheet when more "
        "than one is there. find also takes skip (next_skip of the last find). A plain zip archive: no member "
        "lists its members (name, size, kind); member 'a.xlsx' (or 'inner.zip!a.xlsx') reads "
        "that file."),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["citation"],
        "properties": {"citation": {"type": "string", "maxLength": 2000},
                       "offset": {"type": "integer", "minimum": 0},
                       "chars": {"type": "integer", "minimum": 1000,
                                 "maximum": doc_read.MAX_WINDOW},
                       "find": {"type": "string", "maxLength": 200},
                       "sheet": {"type": "string", "maxLength": 200},
                       "where": {"type": "array", "maxItems": 8, "items": {
                           "type": "object", "required": ["column", "op"],
                           "properties": {"column": {"type": "string"},
                                          "op": {"type": "string",
                                                 "enum": list(doc_read.OPS)},
                                          "value": {}}}},
                       "sum": {"type": "array", "maxItems": 8,
                               "items": {"type": "string"}},
                       "header_row": {"type": "integer", "minimum": 1},
                       "table": {"type": "boolean"},
                       "skip": {"type": "integer", "minimum": 0},
                       "member": {"type": "string", "maxLength": 2000}}},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


class _Kept:
    """Readings kept per (reader, document) for a short while, bounded in count and size."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: "OrderedDict[tuple[str, str], tuple[float, str, dict[str, Any]]]" = \
            OrderedDict()

    def get(self, key: tuple) -> Optional[tuple[str, dict[str, Any]]]:
        with self.lock:
            got = self.items.get(key)
            if got is None or time.monotonic() - got[0] > KEEP_SECONDS:
                self.items.pop(key, None)
                return None
            self.items.move_to_end(key)
            return got[1], got[2]

    def put(self, key: tuple, text: str, about: dict[str, Any]) -> None:
        with self.lock:
            self.items[key] = (time.monotonic(), text, about)
            self.items.move_to_end(key)
            while len(self.items) > KEEP_DOCS or sum(
                    len(v[1]) for v in self.items.values()) > KEEP_CHARS:
                self.items.popitem(last=False)


KEPT = _Kept()


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _outline(text: str) -> list[str]:
    import re
    return [m.group(0).strip("[]") for m in re.finditer(r"^\[(?:sheet [^\]]*|slide \d+)\]",
                                                        text, re.M)][:80]


def _open(backend: Any, reader: Any, citation: str) -> Any:
    """(name, bytes) of the document as this reader may read it, or an error result."""
    if citation.startswith("file:"):
        own = backend._pw_file(reader, citation[len("file:"):])
        if own is None:
            return _error("not_found", "That file is not one of yours here: use the file: "
                          "citation you were given.")
        import base64
        return str(own["name"]), base64.b64decode(own["content_b64"])
    if citation.startswith("sharepoint://"):
        try:
            got = backend._pw_doc_bytes(reader, citation)
        except Exception:  # noqa: BLE001 - not readable here: said, never guessed
            got = None
        if got is None:
            # the original was dropped (or never held) but the capture kept the text it
            # extracted: the same gates, read from that text, and said so
            try:
                kept = backend._pw_doc_text(reader, citation)
            except Exception:  # noqa: BLE001 - not readable here: said, never guessed
                kept = None
            if kept is not None:
                return kept[0] + CAPTURED_SUFFIX, kept[1].encode("utf-8")
            return _error("not_readable", "Its original is not one this reader can open here "
                          "as that version (it changed since, or no text of it was kept): "
                          "use plx.open's text.")
        return got[0], got[1]
    if citation.startswith("graph-mail://") and "#attachment=" in citation:
        try:
            att = backend._pw_mail_attachment(reader, citation)
        except Exception:  # noqa: BLE001 - not readable here: said, never guessed
            att = None
        if att is None:
            return _error("not_readable", "That mail attachment is not one this reader can "
                          "open here (not byte-pinned with its sha256, not held, or no text "
                          "of it was kept): use plx.open's text.")
        name, data, text = att
        if data is not None:
            return name, data
        return name + CAPTURED_SUFFIX, (text or "").encode("utf-8")
    return _error("unsupported", "Only sharepoint://, mail-attachment (graph-mail://...#attachment=) "
                  "and file: citations are read whole here; read other items with plx.open.")


def _archive(backend: Any, guard: Any, citation: str, name: str,
             members: list[dict[str, Any]]) -> dict[str, Any]:
    """A plain zip archive: its members (every one), each readable with ``member``."""
    lines = [f"{m['name']} | {m['size']} bytes | {m['kind']}" for m in members]
    withheld = backend._pw_withheld(" \n".join([name, *lines]), guard)
    if withheld:
        return _error("egress_withheld", withheld.split(":")[0].rstrip(".") + ".")
    return {"citation": citation, "name": name, "kind": "zip", "members": members,
            "member_count": len(members),
            "note": "read one with member (a name above; inner.zip!file.xlsx goes inside a "
                    "zip held in it)",
            "content": {"untrusted_text": "\n".join(lines)}}


def _unit_of(cell: str) -> str:
    """The currency symbol or code (or %) a plain amount's cell carries, else ''."""
    m = re.match(r"^[\s(+-]*([$\u20ac\u00a3\u00a5]|[A-Z]{3}\s)", cell)
    if m:
        return m.group(1).strip()
    m = re.search(r"(\s[A-Z]{3}|%)\)?\s*$", cell)
    return m.group(1).strip() if m else ""


def _typed(header: list[str], rows: list[tuple[int, list[str]]]
           ) -> tuple[list[str], list[str], list[list[Any]], dict[str, Any]]:
    """(column names, categories N/D/X, typed rows, document notes): a column is a number
    column only when EVERY non-empty cell is a plain amount (``doc_read._amount``), a date
    column when every one is an unambiguous date; a number column whose cells carry two
    units (CAD and USD) is kept as text and said, never summed. Header names are made
    unique and at most 63 characters (``ask_refs`` column names)."""
    from decimal import Decimal
    width = max([len(header)] + [len(c) for _, c in rows])
    names, seen = [], {}
    for i in range(width):
        base = (header[i].strip() if i < len(header) else "") or f"col{i + 1}"
        base = base.replace('"', "'")[:60]  # guard-ok: a column name may be 63 characters (ask_refs._COL); the cell is kept whole in the rows
        seen[base] = seen.get(base, 0) + 1
        names.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    cats, units, notes = [], {}, {}
    cols = [[(c[i].strip() if i < len(c) else "") for _, c in rows] for i in range(width)]
    for i, col in enumerate(cols):
        full = [v for v in col if v]
        if full and all(doc_read._amount(v) is not None for v in full):
            found = {_unit_of(v) for v in full} - {""}
            if len(found) > 1:
                cats.append("X")
                notes[names[i]] = ("amounts in " + " and ".join(sorted(found))
                                   + ": not added together")
                continue
            cats.append("N")
            if found:
                units[names[i]] = next(iter(found))
        elif full and all(doc_read._date(v) is not None for v in full):
            cats.append("D")
        else:
            cats.append("X")
    typed = []
    for r in range(len(rows)):
        row: list[Any] = []
        for i in range(width):
            v = cols[i][r]
            if not v:
                row.append(None)
            elif cats[i] == "N":
                row.append(Decimal(repr(doc_read._amount(v))))
            elif cats[i] == "D":
                row.append(doc_read._date(v))
            else:
                row.append(v)
        typed.append(row)
    return names, cats, typed, {"units": units, "not_summed": notes}


def _sheets_for(text: str, about: Mapping[str, Any]) -> list:
    """Every table the reading holds, for ``doc_read.query``: a workbook's or CSV's sheets;
    else a Word file's tables and the ``lines`` of its text; else the ``lines`` (a PDF, a
    scan, a deck), each with where its words came from."""
    reading = doc_read.Reading(about["kind"], text,
                               ocr_numbers=list(about.get("ocr_numbers") or []),
                               grids=list(about.get("grids") or []))
    sheets = doc_read.sheets_of(reading)
    if reading.grids and not doc_read._sheets(text):
        sheets += doc_read.line_sheet(reading)
    return sheets


TABLE_PAGE = 100   # rows one call shows; skip reads on (the result is the whole sheet)


def _table(out: dict[str, Any], text: str, about: Mapping[str, Any], citation: str, sha: str,
           arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The sheet (or CSV) as a result: id ``doc://<20 hex>`` of (document bytes, sheet,
    header row, conditions), columns, ``TABLE_PAGE`` rows from ``skip`` (each cell typed,
    each row ``doc://<id>#row=<sheet row>``) and a profile over EVERY row that matched
    (``result_profile.from_rows``), so ``ask_refs`` prints figures from it as from a
    ``plx.sql`` result. Same arguments, same id: a later page adds its rows to the result."""
    import json
    from operating import result_profile
    where = arguments.get("where") or []
    if not isinstance(where, list) or not isinstance(arguments.get("sheet", ""), str):
        return _error("invalid_input", "where is a list of conditions, sheet a name")
    header_row = arguments.get("header_row")
    if header_row is not None and (isinstance(header_row, bool)
                                   or not isinstance(header_row, int)):
        return _error("invalid_input", "header_row must be a row number")
    try:
        sheets = _sheets_for(text, about)
        got = doc_read.query(text, arguments.get("sheet"), where, [], header_row, keep=True,
                             sheets=sheets)
    except ValueError as why:
        return _error("invalid_input", str(why))
    rows = got["table"]
    names, cats, typed, notes = _typed(got["header"], rows)
    rid = "doc://" + hashlib.sha256(json.dumps(
        [sha, got["sheet"], header_row, where], sort_keys=True, default=str).encode()
    ).hexdigest()[:20]
    profile = result_profile.from_rows(names, typed, cats, [(None, None)] * len(names))
    profile["rows"] = len(rows)
    # a column whose every amount carries one currency CODE is summed in that currency (the
    # figure names it: {{sum(doc://id.col, CAD)}}); a symbol ($ is CAD or USD) or % is not
    # turned into a currency, and its sum says the unit as written
    for col in profile["columns"]:
        unit = notes["units"].get(col.get("column"))
        if unit and re.fullmatch(r"[A-Z]{3}", unit) and col.get("sum") is not None:
            col["sum"] = [{"currency": unit, "sum": col["sum"]}]
        elif unit and col.get("sum") is not None:
            col["note"] = f"amounts written with {unit}"
    skip = arguments.get("skip") or 0
    shown = list(zip(rows, typed))[skip:skip + TABLE_PAGE]
    out.update({k: v for k, v in got.items() if k not in ("table", "rows", "more")})
    out.update({
        "result": rid, "columns": names, "profile": profile,
        "rows": [{"cite": f"{rid}#row={n}", "cells": [
            None if v is None else str(v) for v in cells]} for (n, _), cells in shown],
        "row_count": len(rows),
        "document": {"citation": citation, "sheet": got["sheet"], **notes}})
    if skip + TABLE_PAGE < len(rows):
        out["next_skip"] = skip + TABLE_PAGE
    return out


def call(backend: Any, reader: Any, arguments: Any) -> dict[str, Any]:
    """One ``plx.read_document`` call as ``reader`` (see the module doc)."""
    if not isinstance(arguments, Mapping):
        return _error("invalid_input", "arguments must be an object")
    unknown = sorted(set(arguments) - set(SPEC["inputSchema"]["properties"]))
    if unknown:
        return _error("invalid_input", "unknown arguments: " + ", ".join(unknown)[:200])
    citation = arguments.get("citation")
    if not isinstance(citation, str) or not citation.strip():
        return _error("invalid_input", "send citation")
    citation = citation.strip()
    offset, chars, words = arguments.get("offset"), arguments.get("chars"), arguments.get("find")
    for name, value in (("offset", offset), ("chars", chars)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or value < 0):
            return _error("invalid_input", f"{name} must be a whole number >= 0")
    if words is not None and (not isinstance(words, str) or not words.strip()):
        return _error("invalid_input", "find must be words")
    tabular = arguments.get("table") is True
    if "table" in arguments and not isinstance(arguments["table"], bool):
        return _error("invalid_input", "table is true or false")
    asked_rows = any(k in arguments for k in ("where", "sum", "sheet", "header_row")) \
        and not tabular
    if tabular and (offset is not None or words is not None or "sum" in arguments):
        return _error("invalid_input", "table goes with sheet, header_row, where and skip only")
    if "skip" in arguments and not tabular and words is None:
        return _error("invalid_input", "skip goes with table or find")
    for name in ("skip",):
        value = arguments.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or value < 0):
            return _error("invalid_input", f"{name} must be a whole number >= 0")
    if sum(x is not None for x in (offset, words)) + asked_rows > 1:
        return _error("invalid_input", "send one of offset, find, or where/sum")
    try:
        guard = backend._instruct_guard()
    except Exception:  # noqa: BLE001 - undecidable: nothing read
        guard = None
    if guard is None or getattr(guard, "unavailable", "absent"):
        return _error("not_ready", "Documents can't be read right now: the list of people "
                      "whose data stays out is not loaded.")
    # opened and checked on every call (the reader's access and the exclusion as they are
    # now); only the reading of the same bytes is kept
    opened = _open(backend, reader, citation)
    if isinstance(opened, dict):
        return opened
    name, data = opened
    member = arguments.get("member")
    if member is not None and (not isinstance(member, str) or not member.strip()):
        return _error("invalid_input", "member is the path of a file inside the archive")
    try:
        if member is not None:
            name, data = doc_read.zip_member(data, member.strip())
        else:
            members = doc_read.zip_members(data)
            if members is not None:
                return _archive(backend, guard, citation, name, members)
    except ValueError as why:
        return _error("invalid_input", str(why))
    # a kept file's scanned pages the background reading has done (``ocr_background``)
    desk = getattr(backend, "_uploads", None)
    desk = desk() if citation.startswith("file:") and callable(desk) else None
    file_id = citation[len("file:"):]
    later = ocr_background.known(desk, file_id) if desk is not None else None
    key = (str(getattr(reader, "key", "")), hashlib.sha256(data).hexdigest(),
           "" if later is None else hashlib.sha256(
               repr(sorted(later.items())).encode()).hexdigest())
    kept = KEPT.get(key)
    if kept is None:
        try:
            reading = doc_read.read(name, data, ocr_cap=doc_read.REQUEST_OCR_PAGES,
                                    ocr_known=later)
        except doc_read.Unread as why:
            return _error("unreadable", str(why))
        text = reading.text
        about = {"kind": reading.kind, "parts": reading.parts,
                 "outline": _outline(text), "not_read": reading.unread,
                 "ocr_pages": reading.ocr_pages, "pending": len(reading.ocr_pending),
                 "grids": reading.grids, "ocr_numbers": reading.ocr_numbers,
                 "scans": reading.ocr_total}
        KEPT.put(key, text, about)
    else:
        text, about = kept
    if about.get("pending") and desk is not None:
        # more scanned pages than one request reads: the rest is read in the background
        about = dict(about, background=ocr_background.start(desk, file_id))
    # the whole text and the name, before any of it can reach the model
    withheld = backend._pw_withheld(" \n".join([name, text]), guard)
    if withheld:
        return _error("egress_withheld", withheld.split(":")[0].rstrip(".") + ".")
    captured = name.endswith(CAPTURED_SUFFIX)
    about = dict(about, name=name[:-len(CAPTURED_SUFFIX)] if captured else name)
    out: dict[str, Any] = {"citation": citation, "name": about["name"], "kind": about["kind"],
                           "total_chars": len(text)}
    if captured:
        out["kind"] = "captured_text"
        out["captured_text_note"] = (
            "The original file is not held here; this is the text the capture extracted from "
            "it (a scanned page read by OCR is unconfirmed). Layout, images and any page the "
            "extraction left out are not in it.")
    if about["not_read"]:
        out["not_read"] = about["not_read"]
    if about.get("pending"):
        bg = about.get("background")
        out["ocr_pending_pages"] = about["pending"]
        out["ocr_note_more"] = (
            f"{about['ocr_pages']} of {about['scans']} scanned pages were read now; "
            + ("the other " + str(about["pending"]) + " are still being read in the "
               "background: read this file again in a few minutes for them."
               if bg in ("started", "running") else
               "the other " + str(about["pending"]) + " were not read (a request reads at "
               "most " + str(doc_read.REQUEST_OCR_PAGES) + " scanned pages; a file kept as "
               "yours is read on in the background)."))
    if about["ocr_pages"]:
        out["ocr_pages"] = about["ocr_pages"]
        out["ocr_note"] = "pages read by OCR: check odd figures or codes against the page"
    if tabular:
        return _table(out, text, about, citation, key[1], arguments)
    if asked_rows:
        where, sums = arguments.get("where") or [], arguments.get("sum") or []
        if not isinstance(where, list) or not isinstance(sums, list) or not all(
                isinstance(x, str) for x in sums) or not isinstance(
                arguments.get("sheet", ""), str):
            return _error("invalid_input", "where is a list of conditions, sum a list of "
                          "column names, sheet a name")
        header_row = arguments.get("header_row")
        if header_row is not None and (isinstance(header_row, bool)
                                       or not isinstance(header_row, int)):
            return _error("invalid_input", "header_row must be a row number")
        try:
            got = doc_read.query(text, arguments.get("sheet"), where, sums, header_row,
                                 sheets=_sheets_for(text, about))
        except ValueError as why:
            return _error("invalid_input", str(why))
        out.update({k: v for k, v in got.items() if k != "rows"})
        out["content"] = {"untrusted_text": "\n".join(got["rows"])}
        return out
    if words is not None:
        got = doc_read.find(text, words, skip=arguments.get("skip") or 0)
        out.update(got)
        out["content"] = {"untrusted_text": "\n".join(
            f"@{m['offset']} [{m['in'] or '-'}] {m['line']}" for m in got["matches"])}
        return out
    page = doc_read.window(text, offset or 0, chars or doc_read.WINDOW)
    if offset is None:
        out["outline"] = about["outline"]
    out.update({"offset": page["offset"], "next_offset": page["next_offset"],
                "complete": page["next_offset"] is None and page["offset"] == 0
                and not about["not_read"],
                "content": {"untrusted_text": page["text"]}})
    return out


def tool(backend: Any, reader: Any) -> Any:
    from operating.agent_session import LocalTool
    return LocalTool(spec=SPEC, call=lambda a: call(backend, reader, a))
