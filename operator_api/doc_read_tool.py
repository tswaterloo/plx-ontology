"""``plx.read_document``: the Ask reads one document whole, from its original, not from
the text a capture or the question's attachment reader cut (``operating.doc_read``): every
row of a big workbook (dates as dates), every page of a PDF (a scanned page read by local
OCR), a Word file's tables, headers and footers, a deck's slides and notes.

What it reads, and as whom:

* ``sharepoint://<drive>/<item>@<version>`` (a ``plx.search`` hit): only when the reader's
  own host opens that citation now (``DocumentEditMixin._pw_doc_bytes``: the host's gates
  decide, then that version's original from the SharePoint capture store; a version the
  store no longer holds, or whose original was dropped, is not read here);
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
import threading
import time
from collections import OrderedDict
from typing import Any, Mapping, Optional

from operating import doc_read

from . import ocr_background

NAME = "plx.read_document"
KEEP_SECONDS = 900
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
        "citation from plx.search or a file: citation of the person's own file. With no "
        "offset or find: the first part and the outline (sheets with row counts, pages). "
        "offset: the next part (next_offset of the last read); find: the lines holding all "
        "the words (an item code, a name, a PO), each with its offset and sheet or page. "
        "Rows read 'r<row> | cell | cell'. To count, total or filter a sheet or CSV, send "
        "where (conditions {column: a header or a letter, op, value}, all must hold; none: "
        "every row) and sum (columns to total): it scans every row and answers matched, "
        "sums and the first 100 rows; sheet names one when there are several, header_row "
        "when the header is not the first row. Never count or total rows from one part."),
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
                       "header_row": {"type": "integer", "minimum": 1}}},
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
            return _error("not_readable", "Its original is not one this reader can open here "
                          "as that version (it changed since, or only its captured text is "
                          "kept): use plx.open's text.")
        return got[0], got[1]
    return _error("unsupported", "Only sharepoint:// and file: citations are read whole "
                  "here; read other items with plx.open.")


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
    asked_rows = any(k in arguments for k in ("where", "sum", "sheet", "header_row"))
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
    about = dict(about, name=name)
    out: dict[str, Any] = {"citation": citation, "name": about["name"], "kind": about["kind"],
                           "total_chars": len(text)}
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
            got = doc_read.query(text, arguments.get("sheet"), where, sums, header_row)
        except ValueError as why:
            return _error("invalid_input", str(why)[:800])
        out.update({k: v for k, v in got.items() if k != "rows"})
        out["content"] = {"untrusted_text": "\n".join(got["rows"])}
        return out
    if words is not None:
        got = doc_read.find(text, words)
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
