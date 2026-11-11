"""Files and images a person attaches to an Ask: read in memory for that one question.

**What is read.** The kind is decided from the bytes, never from the name or the browser's
type: a PDF (its text, ``pypdf``), a spreadsheet (an ``.xlsx`` workbook's cells, read with
``zipfile`` and ``xml.etree``; a ``.csv``), plain text, or an image (PNG, JPEG, GIF, WebP:
shown to the model as an image). Anything else, an old ``.xls``, an encrypted PDF, a PDF
with no text layer, or a file over its limit is not read, and the answer says which and
why (``Unread``). Fail closed: an error while reading is an unread file, never a guess.

**Limits.** ``MAX_FILES`` files, each up to ``MAX_BYTES`` (an image up to
``MAX_IMAGE_BYTES``, the model's own limit), together up to ``MAX_TOTAL``; a PDF's first
``MAX_PDF_PAGES`` pages; a workbook's first ``MAX_SHEETS`` sheets and ``MAX_ROWS`` rows
each, its parts unpacked only up to ``MAX_UNZIPPED`` (no zip bomb); ``MAX_TEXT``
characters of text per file and ``MAX_TEXT_TOTAL`` together.

**Nothing is kept.** The bytes and the text live only for the question's own answer: no
file, cache or log holds them. What the saved Ask keeps is ``Attached.meta`` (name, kind,
size, a digest, whether it was read), never the content.

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
        return "spreadsheet", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if data.startswith(b"\xd0\xcf\x11\xe0"):
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
    from pypdf import PdfReader       # optional dependency (requirements.txt); absent: unread
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise _Unread("It is password-protected: attach an unlocked copy.")
    pages = reader.pages
    parts = []
    for number, page in enumerate(pages):
        if number >= MAX_PDF_PAGES:
            break
        parts.append(f"[page {number + 1}]\n{(page.extract_text() or '').strip()}")
    text = "\n\n".join(parts)
    if not re.search(r"\w", re.sub(r"\[page \d+\]", "", text)):
        raise _Unread("It has no text to read (a scan): attach a screenshot of the pages "
                      "instead.")
    text, cut = _clip(text)
    return text, cut or len(pages) > MAX_PDF_PAGES


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
        raise _Unread("It could not be read as a workbook.")
    if b"<!DOCTYPE" in raw[:2048] or b"<!ENTITY" in raw:
        raise _Unread("It could not be read as a workbook.")
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
            raise _Unread("It is not a spreadsheet: attach a PDF, a spreadsheet, text or an "
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
            batch.files.append(unread("The Ask reads PDFs, spreadsheets (.xlsx, .csv), text "
                                      "and images (PNG, JPEG, GIF, WebP); save it as one of "
                                      "those."))
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
            text, cut = {"pdf": _pdf_text, "spreadsheet": _xlsx_text,
                         "text": _plain_text}[kind](data)
        except _Unread as why:
            batch.files.append(unread(str(why), kind))
            continue
        except Exception:  # noqa: BLE001 - a damaged or unusual file: not read, never a guess
            batch.files.append(unread("It could not be read: attach it again, or save it "
                                      "as a PDF or a spreadsheet.", kind))
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
                                    media_type=media))
    return batch


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


__all__ = ["Attached", "Batch", "KINDS", "MAX_BYTES", "MAX_FILES", "MAX_IMAGE_BYTES",
           "MAX_TOTAL", "decode", "kind_of", "prompt_block"]
