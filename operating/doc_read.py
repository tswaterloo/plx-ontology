"""A document's whole text from its bytes, for reading it to the end: every sheet and row of
a workbook (dates as dates), every page of a PDF (a page with no text layer read by local
OCR), a Word document's body, tables, headers and footers, a deck's slides and notes, a
CSV's rows, an old binary ``.xls``/``.doc``, an image's words (local OCR).

``read(name, data)`` answers a ``Reading``: ``text`` with visible markers (``[sheet X: N
rows]``, ``[page N]``, ``[slide N]``) so a part can be found and cited, ``kind``, ``parts``
(sheets, pages or slides), and ``unread``: what was not read, in words (a part past a
bound, an OCR failure), never silent. An encrypted or damaged file raises ``Unread``.

A capture's text bound cuts a big workbook at capture (``integrations.xlsx_extract``); this reads
the original whole (only ``MAX_UNZIPPED`` bounds it: a zip bomb) and
 the reader pages through it (``window``) or searches it (``find``).

Workbook cells: shared and inline strings, numbers, booleans and errors; a formula cell is
its cached value (never evaluated); a number in a date format (``styles.xml``: built-in
date formats and custom codes with day, month or year parts) is its ISO date (and time
when it has one), the 1900 or 1904 date system as the workbook says. Nothing here reads
or writes a system: a pure function of the bytes (OCR runs the local poppler and
tesseract, never a network service)."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
import datetime as dt
import io
import re
import sys
from typing import Any, Iterator, Mapping, Optional
import zipfile
from xml.etree import ElementTree

# A document is read whole (owner 11-23: no cap on characters, rows, sheets, pages or OCR
# pages); what a call shows is a window of it, and ``offset`` pages on to the end.
REQUEST_OCR_PAGES = 10        # scanned pages OCRed inside one Ask request (the rest: background, said)
MAX_UNZIPPED = 400 * 1024 * 1024
MAX_FIND = 60                 # lines a find answers
WINDOW = 30_000               # characters a window holds unless asked for fewer
MAX_WINDOW = 40_000

_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
# built-in number formats that show a date or time (ECMA-376 18.8.30 and the East Asian ids)
DATE_FORMAT_IDS = frozenset(range(14, 23)) | frozenset(range(27, 37)) \
    | frozenset(range(45, 48)) | frozenset(range(50, 59))
TIME_ONLY_IDS = frozenset({18, 19, 20, 21, 45, 46, 47})
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00",
               b"MM\x00*", b"BM")


class Unread(ValueError):
    """The file could not be read at all; ``str()`` says why in plain words."""


@dataclass
class Reading:
    kind: str
    text: str
    parts: int = 0
    unread: list[str] = field(default_factory=list)
    ocr_pages: int = 0
    # scanned pages the request's OCR cap left unread (the caller may read them on in the
    # background), and the scanned pages the PDF has when that is known
    ocr_pending: list[int] = field(default_factory=list)
    ocr_total: int = 0
    # the pages whose text came from OCR (not the PDF's text layer): how sure each line is
    ocr_numbers: list[int] = field(default_factory=list)
    # a Word file's tables kept as tables (name, rows (number, cells)): ``query`` reads them
    # like sheets (the text keeps its own ``| a | b |`` lines)
    grids: list[tuple[str, list[tuple[int, list[str]]]]] = field(default_factory=list)


# ------------------------------------------------------------------ package helpers

class _Package:
    """A zip package read part by part within one unpacked-size budget (no zip bomb)."""

    def __init__(self, data: bytes, unzipped: int = MAX_UNZIPPED) -> None:
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            self.names = set(self.zip.namelist())
        except (zipfile.BadZipFile, ValueError, OSError):
            raise Unread("It could not be read: it is damaged or not an Office file.") from None
        self.budget = unzipped

    def raw(self, name: str) -> Optional[bytes]:
        try:
            info = self.zip.getinfo(name)
        except KeyError:
            return None
        self.budget -= info.file_size
        if self.budget < 0:
            raise Unread("It unpacks to more than can be read safely.")
        with self.zip.open(info) as handle:
            raw = handle.read(info.file_size + 1)
        if len(raw) > info.file_size:
            raise Unread("It could not be read: it is damaged.")
        if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
            raise Unread("It could not be read: it is damaged.")
        return raw

    def xml(self, name: str) -> Optional[ElementTree.Element]:
        raw = self.raw(name)
        if raw is None:
            return None
        try:
            return ElementTree.fromstring(raw)
        except ElementTree.ParseError:
            raise Unread("It could not be read: it is damaged.") from None

    def targets(self, rels: str, base: str) -> dict[str, str]:
        root = self.xml(rels)
        out = {}
        for r in (root.iter(f"{_PR}Relationship") if root is not None else ()):
            target = str(r.get("Target") or "")
            out[str(r.get("Id"))] = target.lstrip("/") if target.startswith("/") \
                else _joined(base, target)
        return out


def _joined(base: str, target: str) -> str:
    parts = [p for p in base.split("/") if p]
    for piece in target.split("/"):
        if piece == "..":
            parts = parts[:-1]
        elif piece and piece != ".":
            parts.append(piece)
    return "/".join(parts)


# ------------------------------------------------------------------ workbook

def _date_format_kind(code: str) -> Optional[str]:
    """Whether a custom number format code shows a date ("date"), a time ("time") or
    neither (None): a d, m, y, h or s part outside quoted text, escapes, [colour/locale]
    brackets and alignment padding (General and pure numbers never)."""
    plain = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.', "", code or "")
    if "general" in plain.lower() or not re.search(r"[dmyhs]", plain, re.I):
        return None
    return "time" if not re.search(r"[dy]", plain, re.I) else "date"


def _date_format(code: str) -> bool:
    """Whether a custom number format code shows a date or time."""
    return _date_format_kind(code) is not None


def _styles(pkg: _Package) -> list[Optional[str]]:
    """Per cell style index: "date", "time" or None, from ``xl/styles.xml``."""
    root = pkg.xml("xl/styles.xml")
    if root is None:
        return []
    custom = {}
    for f in root.iter(f"{_S}numFmt"):
        try:
            custom[int(f.get("numFmtId") or -1)] = str(f.get("formatCode") or "")
        except ValueError:
            continue
    out: list[Optional[str]] = []
    xfs = root.find(f"{_S}cellXfs")
    for xf in (list(xfs) if xfs is not None else ()):
        try:
            fid = int(xf.get("numFmtId") or 0)
        except ValueError:
            fid = 0
        if fid in custom:
            out.append(_date_format_kind(custom[fid]))
        elif fid in DATE_FORMAT_IDS:
            out.append("time" if fid in TIME_ONLY_IDS else "date")
        else:
            out.append(None)
    return out


def excel_date(serial: float, kind: str, date1904: bool = False) -> Optional[str]:
    """An Excel date serial as ISO text: a date, a date and time, or a time; None when
    it is no date Excel shows (negative, past year 9999)."""
    if serial < 0 or serial > 2_958_465:
        return None
    if kind == "time" and serial < 1:
        seconds = round(serial * 86400)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}" + (
            f":{seconds % 60:02d}" if seconds % 60 else "")
    base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
    if not date1904 and serial < 61:
        # Excel's 1900 leap-year bug: serials before 1 March 1900 are one day later
        base = dt.datetime(1899, 12, 31)
    moment = base + dt.timedelta(days=serial)
    moment = moment.replace(microsecond=0) + dt.timedelta(seconds=round(moment.microsecond / 1e6))
    if moment.time() == dt.time(0, 0):
        return moment.date().isoformat()
    return moment.strftime("%Y-%m-%d %H:%M" + (":%S" if moment.second else ""))


def _flat(cell: str) -> str:
    """A cell on one row line: its line breaks as spaces, its "|" as "/" (so a row splits
    into its cells again, ``query``)."""
    return re.sub(r"\s*[\r\n]+\s*", " ", cell).replace("|", "/")


def _number(text: str) -> str:
    try:
        value = float(text)
    except ValueError:
        return text
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(round(value, 10)).rstrip("0").rstrip(".") if "e" not in repr(value) else text


def _col(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "":
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(data: bytes, unzipped: int = MAX_UNZIPPED) -> Reading:
    pkg = _Package(data, unzipped)
    if "xl/workbook.xml" not in pkg.names:
        raise Unread("It is not a workbook.")
    shared: list[str] = []
    strings = pkg.xml("xl/sharedStrings.xml")
    for si in (strings.iter(f"{_S}si") if strings is not None else ()):
        shared.append("".join(t.text or "" for t in si.iter(f"{_S}t")
                              if not _inside_phonetic(si, t)))
    styles = _styles(pkg)
    book = pkg.xml("xl/workbook.xml")
    props = book.find(f"{_S}workbookPr") if book is not None else None
    date1904 = props is not None and str(props.get("date1904") or "").lower() in ("1", "true")
    targets = pkg.targets("xl/_rels/workbook.xml.rels", "xl")
    sheets = list(book.iter(f"{_S}sheet")) if book is not None else []
    out: list[str] = []
    unread: list[str] = []
    for sheet in sheets:
        name = re.sub(r"[\x00-\x1f]+", " ", str(sheet.get("name") or "sheet"))
        hidden = str(sheet.get("state") or "") in ("hidden", "veryHidden")
        path = targets.get(str(sheet.get(f"{_R}id")))
        raw = pkg.raw(path) if path else None
        if raw is None:
            unread.append(f"sheet {name} (its part is missing)")
            continue
        lines: list[str] = []
        try:
            for number, cells in _rows(raw, shared, styles, date1904):
                lines.append(f"r{number} | " + " | ".join(_flat(c) for c in cells))
        except ElementTree.ParseError:
            unread.append(f"sheet {name} (damaged)")
        head = f"[sheet {name}: {len(lines)} rows" + (", hidden" if hidden else "") + "]"
        out.append("\n".join([head, *lines]))
    text = "\n\n".join(out)
    if not re.search(r"\w", re.sub(r"\[sheet [^\]]*\]|r\d+ \|", "", text)):
        raise Unread("Its sheets are empty.")
    return Reading("spreadsheet", text, parts=len(out), unread=unread)


def _inside_phonetic(si: ElementTree.Element, t: ElementTree.Element) -> bool:
    for rph in si.iter(f"{_S}rPh"):
        if any(x is t for x in rph.iter(f"{_S}t")):
            return True
    return False


def _rows(raw: bytes, shared: list[str], styles: list[Optional[str]], date1904: bool
          ) -> Iterator[tuple[int, list[str]]]:
    """Each populated row of one sheet's XML (streamed): its number and its cells' shown
    values, empty cells kept as empty so columns stay in place."""
    number = 0
    for _event, el in ElementTree.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag != f"{_S}row":
            continue
        try:
            number = int(el.get("r") or number + 1)
        except ValueError:
            number += 1
        cells: dict[int, str] = {}
        at = -1
        for c in el.iter(f"{_S}c"):
            ref = c.get("r")
            at = _col(ref) if ref else at + 1
            value = _cell(c, shared, styles, date1904)
            if value != "":
                cells[at] = value
        el.clear()
        if cells:
            yield number, [cells.get(i, "") for i in range(max(cells) + 1)]


def _cell(c: ElementTree.Element, shared: list[str], styles: list[Optional[str]],
          date1904: bool) -> str:
    kind = c.get("t")
    v = c.find(f"{_S}v")
    text = (v.text or "") if v is not None else ""
    if kind == "s":
        return shared[int(text)] if text.isdigit() and int(text) < len(shared) else ""
    if kind == "inlineStr":
        node = c.find(f"{_S}is")
        return "".join(t.text or "" for t in node.iter(f"{_S}t")) if node is not None else ""
    if kind in ("str", "e"):
        return text
    if kind == "b":
        return "TRUE" if text == "1" else "FALSE" if text == "0" else text
    if kind == "d":
        return text
    if text == "":
        return ""
    try:
        style = styles[int(c.get("s") or 0)]
    except (ValueError, IndexError):
        style = None
    if style is not None:
        try:
            shown = excel_date(float(text), style, date1904)
        except ValueError:
            shown = None
        if shown is not None:
            return shown
    return _number(text)


# ------------------------------------------------------------------ word, powerpoint

def _paragraph(p: ElementTree.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _word_blocks(body: ElementTree.Element,
                 grids: Optional[list] = None) -> list[str]:
    lines: list[str] = []
    for block in list(body):
        if block.tag == f"{_W}tbl":
            grid: list[tuple[int, list[str]]] = []
            for row in block.iter(f"{_W}tr"):
                cells = [" ".join(x for x in (_paragraph(p) for p in tc.iter(f"{_W}p")) if x)
                         for tc in row.findall(f"{_W}tc")]
                if any(cells):
                    lines.append("| " + " | ".join(cells) + " |")
                    grid.append((len(grid) + 1, cells))
            if grids is not None and grid:
                grids.append((f"table {len(grids) + 1}", grid))
        elif block.tag == f"{_W}p":
            text = _paragraph(block)
            if text:
                lines.append(text)
        elif block.tag == f"{_W}sdt":
            content = block.find(f"{_W}sdtContent")
            if content is not None:
                lines += _word_blocks(content, grids)
    return lines


def _docx(data: bytes, unzipped: int = MAX_UNZIPPED) -> Reading:
    """The body (its paragraphs and tables in order), then each header, footer, footnotes
    and comments part, as the package's [Content_Types].xml names them (the main part is
    not always word/document.xml: ``doc_edit._word_parts``)."""
    from operating import doc_edit
    pkg = _Package(data, unzipped)
    try:
        parts = doc_edit._word_parts(pkg.zip, doc_edit._NOTE_KINDS)
    except doc_edit.Refused:
        raise Unread("It is not a Word document.") from None
    main = [p for p, k in parts.items() if k == "main"]
    root = pkg.xml(sorted(main)[0])
    body = root.find(f"{_W}body") if root is not None else None
    if body is None:
        raise Unread("It is not a Word document.")
    grids: list = []
    lines = _word_blocks(body, grids)
    extra = []
    for name, label in sorted(parts.items()):
        if label == "main":
            continue
        part = pkg.xml(name)
        got = [t for t in (_paragraph(p) for p in part.iter(f"{_W}p")) if t] \
            if part is not None else []
        if got:
            extra.append(f"[{label}]\n" + "\n".join(got))
    text = "\n".join(lines) + ("\n\n" + "\n\n".join(extra) if extra else "")
    if not re.search(r"\w", text):
        raise Unread("It has no text to read.")
    return Reading("text", text, parts=1, grids=grids)


def _pptx(data: bytes, unzipped: int = MAX_UNZIPPED) -> Reading:
    pkg = _Package(data, unzipped)
    deck = pkg.xml("ppt/presentation.xml")
    if deck is None:
        raise Unread("It is not a PowerPoint deck.")
    targets = pkg.targets("ppt/_rels/presentation.xml.rels", "ppt")
    parts = []
    for number, slide in enumerate(deck.iter(f"{_P}sldId"), 1):
        path = targets.get(str(slide.get(f"{_R}id")))
        root = pkg.xml(path) if path else None
        if root is None:
            continue
        lines = [t for t in ("".join(x.text or "" for x in p.iter(f"{_A}t")).strip()
                             for p in root.iter(f"{_A}p")) if t]
        rels = path.rsplit("/", 1)
        notes_path = next((t for t in pkg.targets(f"{rels[0]}/_rels/{rels[1]}.rels",
                                                  rels[0]).values()
                           if "notesSlide" in t), None)
        notes = pkg.xml(notes_path) if notes_path else None
        said = [t for t in ("".join(x.text or "" for x in p.iter(f"{_A}t")).strip()
                            for p in notes.iter(f"{_A}p")) if t] if notes is not None else []
        said = [t for t in said if not t.isdigit()]   # the slide-number placeholder
        parts.append("\n".join([f"[slide {number}]", *lines]
                               + (["[notes]", *said] if said else [])))
    text = "\n\n".join(parts)
    if not re.search(r"\w", re.sub(r"\[slide \d+\]|\[notes\]", "", text)):
        raise Unread("Its slides have no text to read.")
    return Reading("text", text, parts=len(parts))


# ------------------------------------------------------------------ pdf, images

def _pdf(data: bytes, ocr: bool, cap: Optional[int] = None,
         known: Optional[Mapping[int, str]] = None) -> Reading:
    from integrations import document_understanding as du
    reading = du.read_pages(data, "pdf", max_pages=None, max_text=None)
    if reading.failure == "encrypted":
        raise Unread("It is password-protected.")
    if reading.status == "unsupported":
        raise Unread("It could not be read as a PDF.")
    pages = {p.number: p.text.strip() for p in reading.pages} if reading.pages else {}
    if not pages and reading.text.strip():
        pages = {1: reading.text.strip()}
    unread = []
    if reading.skipped_pages:   # the PDF reader's own bound, said
        unread.append(f"pages from {reading.skipped_pages[0]} on (the PDF reader stopped)")
    blank = [n for n, t in sorted(pages.items()) if not re.search(r"\w", t)]
    if not pages:
        blank = []
    ocr_done, pending, total, got = 0, [], len(blank), {}
    if (blank or not pages) and ocr:
        if known is not None:
            # the background reading of this file is done: its pages, nothing read again
            got = {n: known[n] for n in (blank or known) if known.get(n)}
            failed = [f"pages {', '.join(map(str, [n for n in blank if n not in got][:20]))} "
                      "(OCR found no text)"] if any(n not in got for n in blank) else []
            ocr_done = len(got)
        else:
            got, ocr_done, failed, pending = _ocr_pdf(data, blank, cap)
            total = len(blank) or ocr_done + len(pending)
        pages.update(got)
        unread += failed
        if pending:
            unread.append(f"scanned pages {_span(pending)}: OCR read {ocr_done} of the "
                          f"{total} scanned pages in this request (at most {cap} at a time)")
    elif blank:
        unread.append(f"{len(blank)} scanned pages (no text layer; OCR is not on here)")
    text = "\n\n".join(f"[page {n}]\n{t}" for n, t in sorted(pages.items()) if t)
    if not re.search(r"\w", re.sub(r"\[page \d+\]", "", text)):
        raise Unread("It has no text to read (a scan that OCR could not read).")
    return Reading("pdf", text, parts=len(pages), unread=unread, ocr_pages=ocr_done,
                   ocr_pending=pending, ocr_total=total, ocr_numbers=sorted(got))


def ocr_rest(data: bytes) -> dict[int, str]:
    """Every scanned page of a PDF read by OCR with no request cap (the background read of
    what ``read`` left as ``ocr_pending``): text by page number; {} when none could be."""
    from integrations import document_understanding as du
    reading = du.read_pages(data, "pdf", max_pages=None, max_text=None)
    pages = {p.number: p.text.strip() for p in reading.pages} if reading.pages else {}
    blank = [n for n, t in sorted(pages.items()) if not re.search(r"\w", t)]
    if not blank and pages:
        return {}
    got, _count, failed, _pending = _ocr_pdf(data, blank, None)
    if not got:
        # nothing read is a failure to say (no sidecar), never a finished file with no pages
        raise Unread("; ".join(failed) or "OCR read nothing")
    return got


def _span(pages: list[int]) -> str:
    """Page numbers as ranges: 11-20, 25."""
    out, start = [], 0
    for i, n in enumerate(pages):
        if i + 1 == len(pages) or pages[i + 1] != n + 1:
            out.append(str(pages[start]) if start == i else f"{pages[start]}-{n}")
            start = i + 1
    return ", ".join(out[:12]) + (" ..." if len(out) > 12 else "")


def _ocr_pdf(data: bytes, blank: list[int], cap: Optional[int] = None
             ) -> tuple[dict[int, str], int, list[str], list[int]]:
    """The pages without a text layer read by local OCR (``document_understanding``: poppler
    renders, tesseract reads, its own bounds); (texts by page, pages read, what was not,
    the scanned pages left past ``cap`` when the request's OCR cap stopped it)."""
    from integrations import document_understanding as du
    if not du.local_ocr_available():
        return {}, 0, ["scanned pages (no local OCR on this server)"], []
    try:
        if cap:
            with du.ocr_limits(cap, 300):
                reading = du.read_pages(data, "pdf", ocr=True, max_pages=None, max_text=None)
        else:
            reading = du.read_pages(data, "pdf", ocr=True, max_pages=None, max_text=None)
    except Exception:  # noqa: BLE001 - OCR failed: said, never guessed
        return {}, 0, ["scanned pages (OCR failed)"], []
    wanted = set(blank) if blank else None
    got = {p.number: p.text.strip() for p in reading.pages or ()
           if (wanted is None or p.number in wanted) and p.text.strip()}
    failed, pending = [], []
    skipped = reading.skipped_pages
    if skipped and cap and du.skipped_cap(skipped[0])[0] == du.OCR_PAGE_CAP:
        # the request's cap: the blank pages from the first skipped one on are read later
        last = skipped[1]
        pending = [n for n in (blank or range(skipped[0], last + 1))
                   if skipped[0] <= n <= last and n not in got]
    elif skipped:
        failed.append(f"scanned pages from {skipped[0]} on were not OCRed (the OCR reader stopped)")
    missing = [n for n in (blank or ()) if n not in got and n not in pending]
    if missing:
        failed.append(f"pages {', '.join(map(str, missing[:20]))} (OCR found no text)")
    return got, len(got), failed, pending


def _image(data: bytes, ocr: bool) -> Reading:
    if not ocr:
        raise Unread("It is an image: its words are read only with local OCR, not on here.")
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("tesseract"):
        raise Unread("It is an image and this server has no local OCR.")
    with tempfile.TemporaryDirectory(prefix="plx-docread-") as tmp:
        path = f"{tmp}/image"
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            done = subprocess.run(["tesseract", path, "stdout"], capture_output=True,
                                  timeout=120, env={"PATH": "/usr/bin:/bin",
                                                    "OMP_THREAD_LIMIT": "2"})
        except (OSError, subprocess.TimeoutExpired):
            raise Unread("Its words could not be read (OCR failed).") from None
    text = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not re.search(r"\w", text):
        raise Unread("OCR found no words in it.")
    return Reading("image", text, parts=1, ocr_pages=1, ocr_numbers=[1])


# ------------------------------------------------------------------ text, legacy

def _plain(data: bytes) -> Reading:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise Unread("It is not text.")
    if "\x00" in text:
        raise Unread("It is not text.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise Unread("It is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        if text.count("\n") >= 1:
            rows = [r for r in csv.reader(io.StringIO(text), dialect)]
            lines = [f"r{n} | " + " | ".join(_flat(c) for c in r)
                     for n, r in enumerate(rows, 1) if r]
            return Reading("spreadsheet", f"[rows: {len(lines)}]\n" + "\n".join(lines),
                           parts=1, unread=[])
    except csv.Error:
        pass
    return Reading("text", text, parts=1, unread=[])


def _legacy(data: bytes) -> Reading:
    from integrations import legacy_office as lo
    for kind, extract in (("spreadsheet", lambda d: lo.extract_xls_text(d, max_text=sys.maxsize)),
                          ("text", lambda d: lo.extract_doc_text(d, max_text=None))):
        try:
            got = extract(data)
        except Exception:  # noqa: BLE001 - not this format: the next one
            continue
        text, status, warnings = got[0], got[1], got[2]
        if status != "unsupported" and isinstance(text, str) and re.search(r"\w", text):
            return Reading(kind, text, parts=1, unread=[
                str(w) for w in warnings or () if "truncat" in str(w)])
    raise Unread("It is an old Office file (.xls/.doc) that could not be read.")


# ------------------------------------------------------------------ entry points

def kind_of(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return "image"
    if data.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except (zipfile.BadZipFile, ValueError, OSError):
            return "unknown"
        try:
            types = zipfile.ZipFile(io.BytesIO(data)).read("[Content_Types].xml") \
                if "[Content_Types].xml" in names else b""
        except (zipfile.BadZipFile, KeyError, ValueError, OSError):
            types = b""
        if "word/document.xml" in names or b"wordprocessingml.document.main" in types \
                or b"ms-word.document.macroEnabled.main" in types:
            return "docx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "unknown"
    from operating.msg_reader import CFB_MAGIC
    if data.startswith(CFB_MAGIC):
        return "legacy"
    return "text"


def read(name: str, data: bytes, *, ocr: bool = True, unzipped: int = MAX_UNZIPPED,
         ocr_cap: Optional[int] = None, ocr_known: Optional[Mapping[int, str]] = None
         ) -> Reading:
    """The whole document ``data`` (named ``name``, its kind decided from the bytes, never
    the name) as a ``Reading``; ``Unread`` when nothing of it can be read. ``unzipped``:
    the most an Office package may unpack to (a caller's tighter bound). ``ocr_cap``: the
    most scanned pages to OCR now (``REQUEST_OCR_PAGES`` inside an Ask request; the rest are
    ``Reading.ocr_pending``); ``ocr_known``: the pages a background OCR already read
    (nothing is OCRed again)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise Unread("It is empty.")
    kind = kind_of(bytes(data))
    readers = {"pdf": lambda d: _pdf(d, ocr, ocr_cap, ocr_known), "image": lambda d: _image(d, ocr),
               "docx": lambda d: _docx(d, unzipped), "pptx": lambda d: _pptx(d, unzipped),
               "xlsx": lambda d: _xlsx(d, unzipped), "legacy": _legacy, "text": _plain}
    if kind not in readers:
        raise Unread("It is not a file kind that can be read here.")
    try:
        got = readers[kind](bytes(data))
    except Unread:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError, OSError,
            EOFError, IndexError, RuntimeError):
        raise Unread("It could not be read: it is damaged.") from None
    return got


def window(text: str, offset: int = 0, chars: int = WINDOW) -> dict:
    """One page of ``text``: from ``offset`` (moved back to a line start when it falls
    inside a line) up to ``chars`` characters ending on a line end, with where the next
    page starts (None at the end) and the whole text's length."""
    chars = max(1000, min(int(chars), MAX_WINDOW))
    offset = max(0, min(int(offset), len(text)))
    if offset and text[offset - 1] != "\n":
        back = text.rfind("\n", max(0, offset - 2000), offset)
        offset = back + 1 if back >= 0 else offset
    end = min(len(text), offset + chars)
    if end < len(text):
        cut = text.rfind("\n", offset + chars // 2, end)
        end = cut + 1 if cut > 0 else end
    return {"offset": offset, "text": text[offset:end], "total_chars": len(text),
            "next_offset": end if end < len(text) else None}


def find(text: str, words: str, limit: int = MAX_FIND, skip: int = 0) -> dict:
    """The lines of ``text`` holding every one of ``words`` (case and accents as written,
    case-insensitive), each with its offset and the part (sheet, page or slide) it is in:
    ``limit`` of them from the ``skip``-th match on; ``more``: how many further lines
    matched past those, and ``next_skip`` (the ``skip`` that reads on from there)."""
    terms = [w for w in re.split(r"\s+", str(words or "").strip().lower()) if w]
    if not terms:
        return {"matches": [], "more": 0}
    out, more, part, at, seen = [], 0, None, 0, 0
    for line in text.split("\n"):
        start = at
        at += len(line) + 1
        marker = re.match(r"\[(sheet [^\]:]*|page \d+|slide \d+|rows)", line)
        if marker:
            part = marker.group(1)
        low = line.lower()
        if all(t in low for t in terms):
            seen += 1
            if seen <= skip:
                continue
            if len(out) < limit:
                entry: dict[str, Any] = {"offset": start, "in": part, "line": line[:600]}
                if len(line) > 600:
                    entry["truncated"] = True
                out.append(entry)
            else:
                more += 1
    got = {"matches": out, "more": more, "matched_total": seen}
    if more:
        got["next_skip"] = skip + len(out)
    return got


# ------------------------------------------------------------------ rows of a sheet

OPS = ("=", "!=", ">", ">=", "<", "<=", "contains", "not contains", "empty", "not empty")
MAX_QUERY_ROWS = 100


# a plain amount: optional sign or parentheses, a currency symbol or code, digits with
# thousands separators and decimals, an optional percent; a code ("IT-00012", "W1000123")
# or a number written with a leading zero ("00123") is text, never its digits
_AMOUNT = re.compile(r"^(?P<neg>[-+(])?\s*(?:[$€£¥]|[A-Z]{3}\s)?\s*(?P<sign>[-+])?\s*(?P<num>(?:0|[1-9]\d{0,2}"
                     r"(?:,\d{3})+|[1-9]\d?(?: \d{3})+|[1-9]\d*)(?:\.\d+)?|0?\.\d+)\s*(?:%|\s[A-Z]{3})?\)?$")
DATE_FORMS = ('"YYYY-MM-DD", "YYYY/MM/DD", "15 Jan 2025", "Jan 15, 2025", or a slash date '
              'whose day is over 12 such as "13/01/2025"')
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                         "sep", "oct", "nov", "dec"), 1)}


def _amount(text: str) -> Optional[float]:
    """A plain amount's value; None for anything else (a code, a date, words)."""
    t = re.sub(r"[\u00a0\u2009\u202f]", " ", str(text)).replace("\u2212", "-").strip()
    m = _AMOUNT.match(t)
    if not m or (m.group("neg") == "(") != t.endswith(")"):
        return None
    if m.group("neg") in ("-", "+") and m.group("sign"):
        return None                  # a sign on both sides of the currency: not an amount
    value = float(m.group("num").replace(",", "").replace(" ", ""))
    return -value if "-" in (m.group("neg"), m.group("sign")) or m.group("neg") == "(" else value


def _date(text: str) -> Optional[str]:
    """An unambiguous date as ISO text (2025-01-15 or 2025/01/15, any time after it; 15 Jan
    2025; Jan 15, 2025); None for anything else (01/02/2025 could be either month)."""
    t = str(text).strip()
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T].*)?", t)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[ -]([A-Za-z]{3})[a-z]*\.?[ ,-]+(\d{4})", t)
        if m:
            d, mo, y = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
        else:
            m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", t)
            if m:
                mo, d, y = _MONTHS.get(m.group(1).lower()), int(m.group(2)), int(m.group(3))
            else:
                m = re.fullmatch(r"(\d{1,2})([/.])(\d{1,2})\2(\d{4})", t)
                if not m:
                    return None
                a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
                if a > 12 and b <= 12:
                    d, mo = a, b             # the day cannot be a month: day first
                elif b > 12 and a <= 12:
                    mo, d = a, b             # month first
                elif a == b:
                    d = mo = a
                else:
                    return None              # 01/02/2025 could be either
    try:
        return dt.date(y, mo, d).isoformat() if mo else None
    except ValueError:
        return None


def _sheets(text: str) -> list[tuple[str, list[tuple[int, list[str]]]]]:
    """Each sheet (a CSV is one) of a reading's text: its name and its rows (number,
    cells), from the ``r<n> | ...`` lines."""
    out: list[tuple[str, list[tuple[int, list[str]]]]] = []
    for line in text.split("\n"):
        head = re.match(r"\[(?:sheet (.*?): \d+ rows[^\]]*|rows: \d+)\]$", line)
        if head:
            out.append((head.group(1) or "rows", []))
            continue
        row = re.match(r"r(\d+) \| ?(.*)$", line)
        if row and out:
            out[-1][1].append((int(row.group(1)), row.group(2).split(" | ")))
    return out


def line_sheet(reading: "Reading") -> list[tuple[str, list[tuple[int, list[str]]]]]:
    """The lines of a reading that is not a sheet, as one table named ``lines`` for
    ``query``: the header (row 0: part, text, how_sure) and one row per text line, the row
    number being the line's number in the reading. ``part`` is the marker the reading put
    before it (``page 3``, ``slide 2``, ``header``), ``how_sure`` where the words came from:
    ``ocr`` (a scanned page or an image), ``text layer`` (a PDF page's own text) or
    ``document text``. A line is never split into fields here: what a field's value is
    stays for the reader of the line."""
    rows: list[tuple[int, list[str]]] = [(0, ["part", "text", "how_sure"])]
    part, n = "", 0
    for line in reading.text.split("\n"):
        marker = re.fullmatch(r"\[([^\]]{1,60})\]", line.strip())
        if marker:
            part = marker.group(1)
            continue
        if not line.strip():
            continue
        n += 1
        page = re.fullmatch(r"page (\d+)", part)
        if reading.kind == "image" or (page and int(page.group(1)) in reading.ocr_numbers):
            sure = "ocr"
        elif reading.kind == "pdf":
            sure = "text layer"
        else:
            sure = "document text"
        rows.append((n, [part, line, sure]))
    return [("lines", rows)]


def sheets_of(reading: "Reading") -> list[tuple[str, list[tuple[int, list[str]]]]]:
    """Every table a reading holds, for ``query``: a workbook's or CSV's sheets, a Word
    file's tables (``table 1``...), then the ``lines`` of any other text."""
    return _sheets(reading.text) or list(reading.grids) or line_sheet(reading)


def query(text: str, sheet: Optional[str] = None, where: Any = (), sums: Any = (),
          header_row: Optional[int] = None, keep: bool = False,
          sheets: Optional[list] = None) -> dict:
    """Rows of one sheet of a workbook or CSV reading that meet every ``where`` condition
    ({"column": a header or a column letter, "op": one of ``OPS``, "value"}; = and != compare
    two plain amounts as amounts and anything else as whole text, case-insensitive, so a
    code matches only itself; > >= < <= compare plain amounts or unambiguous dates, and a
    row whose cell is neither is counted in ``not_compared``, never silently false), counted over
    the whole sheet, with the sums of ``sums`` columns over them and the first
    ``MAX_QUERY_ROWS`` (``keep``: every matched row too, as ``table``: (row number, cells)). The header is the sheet's first row (or ``header_row``). An unknown
    sheet, column or op is refused with what there is (``ValueError``)."""
    sheets = _sheets(text) if sheets is None else sheets
    if not sheets:
        raise ValueError("it has no sheet of rows")
    if sheet is None:
        if len(sheets) > 1:
            raise ValueError("name the sheet: " + ", ".join(n for n, _ in sheets)[:400])
        name, rows = sheets[0]
    else:
        found = [s for s in sheets if s[0].strip().lower() == str(sheet).strip().lower()]
        if not found:
            raise ValueError("no such sheet; there are: " + ", ".join(n for n, _ in sheets)[:400])
        name, rows = found[0]
    if not rows:
        return {"sheet": name, "header": [], "rows_scanned": 0, "matched": 0, "rows": []}
    at = next((k for k, (n, _) in enumerate(rows) if n == header_row), None) \
        if header_row is not None else 0
    if at is None:
        raise ValueError(f"row {header_row} is not in the sheet")
    header = rows[at][1]
    body = rows[at + 1:]

    def column(ref: Any) -> int:
        ref = str(ref or "").strip()
        names = [h.strip().lower() for h in header]
        if ref.lower() in names and names.count(ref.lower()) == 1:
            return names.index(ref.lower())
        if re.fullmatch(r"[A-Za-z]{1,3}", ref):
            return _col(ref.upper())
        raise ValueError(f"no column {ref!r}; the header is: " + " | ".join(header)[:600])

    conds = []
    for c in where or ():
        if not isinstance(c, dict) or c.get("op") not in OPS:
            raise ValueError("each condition is {column, op, value}; op is one of "
                             + ", ".join(OPS))
        conds.append((column(c.get("column")), c["op"], c.get("value")))
    totals = [(str(s), column(s)) for s in (sums or ())]

    # an ordering condition compares amounts or dates, as its value is one
    for col, op, value in conds:
        if op in (">", ">=", "<", "<=") and _amount(str(value)) is None \
                and _date(str(value)) is None:
            raise ValueError(f"{value!r} is neither an amount nor a date I can compare "
                             f"with {op}; dates are accepted as {DATE_FORMS}")
    not_compared: dict[str, int] = {}

    def holds(cells: list[str], col: int, op: str, value: Any) -> bool:
        cell = cells[col].strip() if col < len(cells) else ""
        if op in ("empty", "not empty"):
            return (cell == "") == (op == "empty")
        if op in ("contains", "not contains"):
            return (str(value).lower() in cell.lower()) == (op == "contains")
        a, b = _amount(cell), _amount(str(value))
        if op in ("=", "!="):
            same = a == b if a is not None and b is not None else \
                cell.lower() == str(value).strip().lower()
            return same == (op == "=")
        x, y = (a, b) if b is not None else (_date(cell), _date(str(value)))
        if x is None:
            if cell:
                head = header[col] if col < len(header) else f"column {col + 1}"
                not_compared[head] = not_compared.get(head, 0) + 1
            return False
        return {">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}[op]

    matched, sums_out, shown, not_numbers = 0, {n: 0.0 for n, _ in totals}, [], {}
    table: list[tuple[int, list[str]]] = []
    for number, cells in body:
        if all(holds(cells, col, op, value) for col, op, value in conds):
            matched += 1
            if keep:
                table.append((number, cells))
            for n, col in totals:
                got = _amount(cells[col]) if col < len(cells) else None
                if got is None:
                    not_numbers[n] = not_numbers.get(n, 0) + (
                        1 if col < len(cells) and cells[col].strip() else 0)
                else:
                    sums_out[n] += got
            if len(shown) < MAX_QUERY_ROWS:
                shown.append(f"r{number} | " + " | ".join(cells))
    out = {"sheet": name, "header": header, "rows_scanned": len(body), "matched": matched,
           "rows": shown, "more": max(0, matched - len(shown))}
    if keep:
        out["table"] = table
    if totals:
        out["sums"] = {n: round(v, 6) for n, v in sums_out.items()}
        if any(not_numbers.values()):
            out["not_numbers"] = not_numbers
    if not_compared:
        # rows whose cell is no amount or unambiguous date: not counted, and said so
        out["not_compared"] = not_compared
        out["not_compared_note"] = ("these rows' cells could not be compared (not an "
                                    "amount, or a date written ambiguously such as "
                                    "01/02/2025): they are not in matched")
    return out


# ------------------------------------------------------------------ zip archives

def zip_members(data: bytes, unzipped: Optional[int] = None) -> Optional[list[dict]]:
    """The members of a plain zip (not an Office package: those are read as documents) as
    ``{name, size, kind}``, in the archive's order, every one listed (no cap on the count);
    None when ``data`` is no zip or is one of the Office kinds. ``ValueError`` when the
    archive unpacks to more than ``unzipped`` bytes (a bomb: nothing of it is opened)."""
    unzipped = MAX_UNZIPPED if unzipped is None else unzipped
    if not data.startswith(b"PK\x03\x04") or kind_of(data) != "unknown":
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = [i for i in z.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    if sum(i.file_size for i in infos) > unzipped:
        raise ValueError("the archive unpacks to more than the limit; nothing of it is opened")
    out = []
    for i in infos:
        try:
            kind = kind_of(z.read(i)) if i.file_size <= 50_000_000 else "unknown"
            if kind == "unknown" and zip_members_ok(z, i):
                kind = "zip"
        except (zipfile.BadZipFile, ValueError, OSError, RuntimeError):
            kind = "unreadable"
        out.append({"name": i.filename, "size": i.file_size, "kind": kind})
    return out


def zip_members_ok(z: "zipfile.ZipFile", info: "zipfile.ZipInfo") -> bool:
    """Whether a member is itself a zip archive."""
    try:
        with z.open(info) as f:
            return f.read(4) == b"PK\x03\x04"
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return False


def zip_member(data: bytes, path: str, unzipped: Optional[int] = None) -> tuple[str, bytes]:
    """(name, bytes) of the member ``path`` of a zip; ``a.zip!b.xlsx`` goes into a zip held
    inside it, at any depth (each level inside the ``unzipped`` budget). ``ValueError`` says
    what there is when the member is not found."""
    unzipped = MAX_UNZIPPED if unzipped is None else unzipped
    name, blob = "", data
    for step in [p for p in path.split("!") if p]:
        members = zip_members(blob, unzipped)
        if members is None:
            raise ValueError(f"{name or 'the file'} is not a plain zip archive")
        names = [m["name"] for m in members]
        if step not in names:
            raise ValueError("no such member; the archive's members are listed by reading it "
                             f"without member ({len(names)} of them)")
        try:
            info = zipfile.ZipFile(io.BytesIO(blob)).getinfo(step)
            if info.file_size > unzipped:
                raise ValueError("that member unpacks to more than the limit")
            blob = zipfile.ZipFile(io.BytesIO(blob)).read(step)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise ValueError("that member could not be opened") from exc
        name = step
    return name, blob
