"""Files and images a person attaches to an Ask: read in memory for that one question.

**What is read.** The kind is decided from the bytes, never from the name or the browser's
type: a PDF (its text, ``pypdf``), a spreadsheet (an ``.xlsx`` workbook's cells, read with
``zipfile`` and ``xml.etree``; a ``.csv``), plain text, a Word document or a PowerPoint deck
(a ``.docx``'s paragraphs and tables, a ``.pptx``'s slides in order: read as text, kind
``text``, the same way), or an image (PNG, JPEG, GIF, WebP: shown to the model as an
image). Anything else, an old ``.xls``, an encrypted PDF, a PDF
with no text layer, or a file over its limit is not read, and the answer says which and
why (``Unread``). Fail closed: an error while reading is an unread file, never a guess.

**Limits.** ``MAX_FILES`` files, each up to ``MAX_BYTES`` (an image up to
``MAX_IMAGE_BYTES``, the model's own limit), together up to ``MAX_TOTAL``; a PDF's first
``MAX_PDF_PAGES`` pages; a workbook's first ``MAX_SHEETS`` sheets and ``MAX_ROWS`` rows
each, its parts unpacked only up to ``MAX_UNZIPPED`` (no zip bomb); ``MAX_TEXT``
characters of text per file and ``MAX_TEXT_TOTAL`` together.

**What is kept.** The text lives only for the question's own answer: no cache or log
holds it, and the saved Ask keeps ``Attached.meta`` (name, kind, size, a digest, whether it
was read), never the content. A Word, Excel or PowerPoint file the person attached is the
one exception: its bytes (``Attached.data``, in memory) are kept by the caller as the
person's own file, theirs only, like the copies the Ask edits for them
(``operator_api.backend_ask._keep_attached``), so the Ask can edit it and hand the edited
copy back (``Attached.kept``: the ``file:`` citation ``plx.edit_document`` takes).

The people exclusion is applied by the caller (``operator_api.backend``): the text read
here is cleaned of excluded people's addresses and ids before a model sees it, and with
the exclusion list unavailable no attachment is read.
"""

from __future__ import annotations

import base64
import binascii
import csv
from dataclasses import dataclass, field
import hashlib
import io
import re
from typing import Any, Iterable, Mapping, Optional, Sequence
import zipfile
from xml.etree import ElementTree

from operating.msg_reader import CFB_MAGIC

MAX_FILES = 5
MAX_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL = 10 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_SHEETS = 10
MAX_ROWS = 2000
MAX_UNZIPPED = 60 * 1024 * 1024
MAX_TEXT = 60_000
MAX_TEXT_TOTAL = 120_000

IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg",
               b"GIF87a": "image/gif", b"GIF89a": "image/gif"}
KINDS = ("pdf", "spreadsheet", "text", "image")

_SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_PRESENTATION_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# the kinds an attached file is kept to be edited as (media type -> the file's extension)
EDITABLE = {_DOCX: "docx", _XLSX: "xlsx", _PPTX: "pptx"}


@dataclass(frozen=True)
class Attached:
    """One file as read: ``text`` (a document's) or ``image`` (base64, with
    ``media_type``); ``unread`` says in words why nothing was read."""
    name: str
    kind: Optional[str]
    size: int
    digest: str
    text: str = ""
    image: Optional[str] = None
    media_type: Optional[str] = None
    unread: Optional[str] = None
    cut: bool = False
    # a .docx/.xlsx/.pptx's own bytes, for the caller to keep as the person's file (never
    # in ``meta``, never shown); ``kept``: that file's ``file:`` citation once kept
    data: Optional[bytes] = field(default=None, repr=False, compare=False)
    kept: Optional[str] = None

    @property
    def meta(self) -> dict[str, Any]:
        """What the saved Ask keeps of it: never its content."""
        return {"name": self.name, "kind": self.kind, "size": self.size,
                "digest": self.digest, "read": self.unread is None, "note": self.unread
                or ("Read in part: it is longer than the Ask reads." if self.cut else None)}


@dataclass
class Batch:
    files: list[Attached] = field(default_factory=list)

    @property
    def read(self) -> list[Attached]:
        return [f for f in self.files if f.unread is None]

    @property
    def texts(self) -> list[str]:
        return [f.text for f in self.read if f.text]

    @property
    def images(self) -> list[dict[str, str]]:
        return [{"media_type": f.media_type or "", "data": f.image or ""}
                for f in self.read if f.image]


def _name(value: Any) -> str:
    name = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", str(value or "")).strip()
    return name[:200] or "attachment"


def kind_of(data: bytes) -> tuple[Optional[str], Optional[str]]:
    """(kind, media type) from the bytes alone; (None, None) when not one we read."""
    if data.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    for magic, media in IMAGE_TYPES.items():
        if data.startswith(magic):
            return "image", media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if data.startswith(b"PK\x03\x04"):
        # an Office package: what it is, by the part its package names (never the name);
        # a zip none of them names is read as a workbook and refused as one
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, ValueError, OSError):
            names = set()
        if "word/document.xml" in names and "xl/workbook.xml" not in names:
            return "text", _DOCX
        if "ppt/presentation.xml" in names and "xl/workbook.xml" not in names:
            return "text", _PPTX
        return "spreadsheet", _XLSX
    if data.startswith(CFB_MAGIC[:4]):
        return None, None     # an old binary Office file (.xls, .doc): not read
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, None
    if "\x00" in text:
        return None, None
    return "text", "text/plain"


def _clip(text: str) -> tuple[str, bool]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return (text, False) if len(text) <= MAX_TEXT else (text[:MAX_TEXT], True)


def _pdf_text(data: bytes) -> tuple[str, bool]:
    # The one reading of a PDF (document_understanding.read_pages: pypdf, else Portal's
    # converter or poppler's text layer), on its first MAX_PDF_PAGES pages; no OCR.
    from integrations import document_understanding as du
    reading = du.read_pages(data, "pdf", max_pages=MAX_PDF_PAGES, max_text=None,
                            first_pages=True)
    if reading.failure == "encrypted":
        raise _Unread("It is password-protected: attach an unlocked copy.")
    if reading.status == "unsupported":
        raise ValueError(reading.failure or "pdf_unread")   # not read: the generic reason
    if reading.pages:
        text = "\n\n".join(f"[page {page.number}]\n{page.text.strip()}"
                           for page in reading.pages)
    else:
        text = reading.text.strip()
    if not re.search(r"\w", re.sub(r"\[page \d+\]", "", text)):
        raise _Unread("It has no text to read (a scan): attach a screenshot of the pages "
                      "instead.")
    text, cut = _clip(text)
    return text, cut or reading.skipped_pages is not None


def _xml(archive: zipfile.ZipFile, name: str, budget: list[int]) -> Optional[ElementTree.Element]:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    budget[0] -= info.file_size
    if budget[0] < 0:
        raise _Unread("It unpacks to more than the Ask reads.")
    with archive.open(info) as handle:
        raw = handle.read(info.file_size + 1)
    if len(raw) > info.file_size:
        raise _Unread("It could not be read: it is damaged.")
    if b"<!DOCTYPE" in raw[:2048] or b"<!ENTITY" in raw:
        raise _Unread("It could not be read: it is damaged.")
    return ElementTree.fromstring(raw)


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    number = 0
    for ch in letters.group(0) if letters else "A":
        number = number * 26 + ord(ch) - 64
    return number - 1


def _xlsx_text(data: bytes) -> tuple[str, bool]:
    budget = [MAX_UNZIPPED]
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _Unread("It could not be read as a workbook.") from None
    with archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise _Unread("It is not a spreadsheet, Word or PowerPoint file: attach a PDF, a "
                          "spreadsheet, a .docx, a .pptx, text or an "
                          "image.")
        shared: list[str] = []
        strings = _xml(archive, "xl/sharedStrings.xml", budget)
        for item in strings.iter(f"{_SPREADSHEET_NS}si") if strings is not None else ():
            shared.append("".join(t.text or "" for t in item.iter(f"{_SPREADSHEET_NS}t")))
        book = _xml(archive, "xl/workbook.xml", budget)
        rels = _xml(archive, "xl/_rels/workbook.xml.rels", budget)
        target = {r.get("Id"): r.get("Target") for r in
                  (rels.iter(f"{_PKG_REL_NS}Relationship") if rels is not None else ())}
        parts, cut = [], False
        sheets = list(book.iter(f"{_SPREADSHEET_NS}sheet")) if book is not None else []
        if len(sheets) > MAX_SHEETS:
            cut, sheets = True, sheets[:MAX_SHEETS]
        for sheet in sheets:
            path = str(target.get(sheet.get(f"{_REL_NS}id")) or "").lstrip("/")
            path = path if path.startswith("xl/") else "xl/" + path
            root = _xml(archive, path, budget)
            if root is None:
                continue
            lines = [f"[sheet {_name(sheet.get('name'))}]"]
            for count, row in enumerate(root.iter(f"{_SPREADSHEET_NS}row")):
                if count >= MAX_ROWS:
                    cut = True
                    break
                cells: dict[int, str] = {}
                for cell in row.iter(f"{_SPREADSHEET_NS}c"):
                    value = cell.find(f"{_SPREADSHEET_NS}v")
                    inline = cell.find(f"{_SPREADSHEET_NS}is")
                    text = (value.text or "") if value is not None else ""
                    if cell.get("t") == "s" and text.isdigit() and int(text) < len(shared):
                        text = shared[int(text)]
                    elif cell.get("t") == "inlineStr" and inline is not None:
                        text = "".join(t.text or "" for t in inline.iter(f"{_SPREADSHEET_NS}t"))
                    if text != "":
                        cells[_column(cell.get("r") or "")] = text
                if cells:
                    lines.append("\t".join(cells.get(i, "") for i in range(max(cells) + 1)))
            parts.append("\n".join(lines))
    text, clipped = _clip("\n\n".join(parts))
    if not re.search(r"\w", re.sub(r"\[sheet [^\]]*\]", "", text)):
        raise _Unread("Its sheets are empty.")
    return text, cut or clipped


def _package(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _Unread("It could not be read: it is damaged.") from None


def _paragraphs(root: ElementTree.Element, para: str, run_text: str) -> list[str]:
    """Each paragraph's text (its runs' text joined), empty ones left out."""
    out = []
    for p in root.iter(para):
        text = "".join(t.text or "" for t in p.iter(run_text)).strip()
        if text:
            out.append(text)
    return out


def _docx_text(data: bytes) -> tuple[str, bool]:
    """A Word document's body in order: paragraphs as lines, a table's rows as tab-joined
    cells (headers, footers, comments and tracked deletions are not read)."""
    budget = [MAX_UNZIPPED]
    with _package(data) as archive:
        root = _xml(archive, "word/document.xml", budget)
    body = root.find(f"{_WORD_NS}body") if root is not None else None
    lines: list[str] = []
    for block in (list(body) if body is not None else ()):
        if block.tag == f"{_WORD_NS}tbl":
            for row in block.iter(f"{_WORD_NS}tr"):
                cells = [" ".join(_paragraphs(c, f"{_WORD_NS}p", f"{_WORD_NS}t"))
                         for c in row.iter(f"{_WORD_NS}tc")]
                if any(cells):
                    lines.append("\t".join(cells))
        elif block.tag == f"{_WORD_NS}p":
            lines.extend(_paragraphs(block, f"{_WORD_NS}p", f"{_WORD_NS}t"))
    text = "\n".join(lines)
    if not re.search(r"\w", text):
        raise _Unread("It has no text to read.")
    return _clip(text)


def _pptx_text(data: bytes) -> tuple[str, bool]:
    """A deck's slides in the deck's order, each ``[slide N]`` and its paragraphs (shapes
    and tables; speaker notes are not read)."""
    budget = [MAX_UNZIPPED]
    parts: list[str] = []
    with _package(data) as archive:
        deck = _xml(archive, "ppt/presentation.xml", budget)
        rels = _xml(archive, "ppt/_rels/presentation.xml.rels", budget)
        target = {r.get("Id"): r.get("Target") for r in
                  (rels.iter(f"{_PKG_REL_NS}Relationship") if rels is not None else ())}
        ids = list(deck.iter(f"{_PRESENTATION_NS}sldId")) if deck is not None else []
        for number, slide in enumerate(ids, 1):
            path = str(target.get(slide.get(f"{_REL_NS}id")) or "").lstrip("/")
            path = path if path.startswith("ppt/") else "ppt/" + path
            root = _xml(archive, path, budget)
            if root is None:
                continue
            lines = _paragraphs(root, f"{_DRAWING_NS}p", f"{_DRAWING_NS}t")
            parts.append("\n".join([f"[slide {number}]", *lines]))
    text = "\n\n".join(parts)
    if not re.search(r"\w", re.sub(r"\[slide \d+\]", "", text)):
        raise _Unread("Its slides have no text to read.")
    return _clip(text)


def _plain_text(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8-sig")
    if not text.strip():
        raise _Unread("It is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        if dialect.delimiter != "\t" and text.count("\n") >= 1:
            rows = csv.reader(io.StringIO(text), dialect)
            text = "\n".join("\t".join(row) for _, row in zip(range(MAX_ROWS * 5), rows))
    except csv.Error:
        pass     # plain text, as written
    return _clip(text)


class _Unread(Exception):
    """A file not read, with the words the answer says about it."""


def decode(items: Iterable[Mapping[str, Any]]) -> Batch:
    """Each ``{"name", "data" (base64)}`` read under the limits (module doc), in order;
    the files past ``MAX_FILES`` or past ``MAX_TOTAL`` are listed as not read."""
    batch, total, text_total = Batch(), 0, 0
    for index, item in enumerate(items):
        name = _name(item.get("name"))
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            data = b""
        digest = hashlib.sha256(data).hexdigest()[:16]

        def unread(why: str, kind: Optional[str] = None) -> Attached:
            return Attached(name, kind, len(data), digest, unread=why)
        if index >= MAX_FILES:
            batch.files.append(unread(f"Only the first {MAX_FILES} attachments are read."))
            continue
        if not data:
            batch.files.append(unread("It arrived empty or damaged: attach it again."))
            continue
        kind, media = kind_of(data)
        if kind is None:
            batch.files.append(unread("The Ask reads PDFs, spreadsheets (.xlsx, .csv), Word "
                                      "(.docx) and PowerPoint (.pptx) files, text and images "
                                      "(PNG, JPEG, GIF, WebP); save it as one of those."))
            continue
        limit = MAX_IMAGE_BYTES if kind == "image" else MAX_BYTES
        if len(data) > limit:
            batch.files.append(unread(f"It is larger than {limit // (1024 * 1024)} MB.", kind))
            continue
        if total + len(data) > MAX_TOTAL:
            batch.files.append(unread(f"The attachments together are larger than "
                                      f"{MAX_TOTAL // (1024 * 1024)} MB.", kind))
            continue
        total += len(data)
        if kind == "image":
            batch.files.append(Attached(name, kind, len(data), digest, media_type=media,
                                        image=base64.b64encode(data).decode("ascii")))
            continue
        try:
            read = {"pdf": _pdf_text, "spreadsheet": _xlsx_text, "text": _plain_text}[kind]
            read = {_DOCX: _docx_text, _PPTX: _pptx_text}.get(media or "", read)
            text, cut = read(data)
        except _Unread as why:
            batch.files.append(unread(str(why), kind))
            continue
        except Exception:  # noqa: BLE001 - a damaged or unusual file: not read, never a guess
            batch.files.append(unread("It could not be read: attach it again, or save it "
                                      "as a PDF.", kind))
            continue
        room = MAX_TEXT_TOTAL - text_total
        if room <= 0:
            batch.files.append(unread("The attachments hold more text than the Ask reads "
                                      "at once.", kind))
            continue
        if len(text) > room:
            text, cut = text[:room], True
        text_total += len(text)
        batch.files.append(Attached(name, kind, len(data), digest, text=text, cut=cut,
                                    media_type=media,
                                    data=data if media in EDITABLE else None))
    return batch


WITHHELD = ("Its text is not given to the assistant: it names a party whose material the "
            "assistant may not read.")


def image_words(image: Optional[str]) -> str:
    """The words an image file carries beside its pixels, for the Freightcom screen: every
    run of 4+ printable ASCII or UTF-16LE characters in its bytes (EXIF, XMP, PNG text,
    comments, an embedded file name) and the text of PNG's compressed text chunks. Pixel
    data's chance runs are noise the rule never matches by accident ("freight" + "com")."""
    import zlib
    raw = base64.b64decode(image or "", validate=False)
    words = [m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{4,}", raw)]
    words += [m.decode("utf-16-le") for m in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", raw)]
    at = 8 if raw.startswith(b"\x89PNG\r\n\x1a\n") else len(raw)
    while at + 8 <= len(raw):
        size = int.from_bytes(raw[at:at + 4], "big")
        kind, body = raw[at + 4:at + 8], raw[at + 8:at + 8 + size]
        at += 12 + size
        packed = None
        if kind == b"zTXt" and b"\x00" in body:
            packed = body.split(b"\x00", 1)[1][1:]
        elif kind == b"iTXt" and body.count(b"\x00") >= 1:
            key, rest = body.split(b"\x00", 1)
            if rest[:1] == b"\x01":
                packed = rest[2:].split(b"\x00", 2)[-1]
        if packed is not None:
            words.append(zlib.decompressobj().decompress(packed, 1 << 20)
                         .decode("utf-8", "replace"))
    return "\n".join(words)


def screened(files: Sequence[Attached]) -> list[Attached]:
    """``files`` with the owner's no-AI rule applied: a read file whose name or text, or
    an image whose name or carried words (``image_words``), names Freightcom
    (``image_ocr_gpu.touches_freightcom``, the detection every read uses; over-matching
    withholds) keeps no text, image or bytes and says why (``WITHHELD``); an error while
    deciding withholds too. What an image SHOWS is not read here (no OCR before the model
    sees it): a photo of a Freightcom label passes as the person's own upload."""
    import dataclasses
    from integrations.image_ocr_gpu import touches_freightcom
    out = []
    for f in files:
        try:
            named = touches_freightcom(f.name or "", f.text or "",
                                       image_words(f.image) if f.image else "")
        except Exception:  # noqa: BLE001 - undecidable: withheld
            named = True
        out.append(dataclasses.replace(f, text="", image=None, data=None, unread=WITHHELD)
                   if named else f)
    return out


def prompt_block(files: Sequence[Attached]) -> str:
    """The read files' text for the model, each between markers it is told hold data."""
    out = []
    for number, f in enumerate(files, 1):
        if f.image:
            out.append(f"<<attachment {number}: {f.name} (an image, shown above)>>")
        elif f.text:
            out.append(f"<<attachment {number}: {f.name}>>\n{f.text}\n<<end of attachment "
                       f"{number}>>")
    return "\n\n".join(out)


__all__ = ["Attached", "Batch", "EDITABLE", "KINDS", "WITHHELD", "MAX_BYTES", "MAX_FILES", "MAX_IMAGE_BYTES",
           "MAX_TOTAL", "decode", "kind_of", "prompt_block", "screened"]
