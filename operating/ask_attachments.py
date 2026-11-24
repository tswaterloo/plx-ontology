"""Files and images a person attaches to an Ask: read in memory for that one question.

**What is read.** The kind is decided from the bytes, never from the name or the browser's
type: a PDF (every page's text; a scanned page read by local OCR where the server has
poppler and tesseract), a workbook (every sheet and row of an ``.xlsx``, dates as dates; an
old ``.xls``) and an old Word ``.doc``, all through ``operating.doc_read``; a ``.csv`` or
plain text; a Word document or a PowerPoint deck (a ``.docx``'s paragraphs and tables, a
``.pptx``'s slides in order: read as text, kind ``text``); or an image (PNG, JPEG, GIF,
WebP: shown to the model as an image). Anything else, an encrypted PDF, a scan OCR cannot
read, or a file over its limit is not read, and the answer says which and why
(``Unread``). Fail closed: an error while reading is an unread file, never a guess.

**Limits.** Any number of files of any size (a file arrives streamed to
disk by ``operating.ask_uploads``, its path in the item, or inline as base64); an image over the
model's own limit (``MAX_IMAGE_BYTES``) is scaled down to fit; an Office package is unpacked
only up to ``MAX_UNZIPPED`` (the zip-bomb bound, a security bound that stays). The question
holds the files' text up to the model's context window (``context_window.PROMPT_FILES_CHARS``
together): a file whose text goes on past that is shown to its end of that share and keeps its
whole text (``Attached.whole``) and its bytes for the caller to keep as the person's own file,
which ``plx.read_document`` reads to the end, page by page (``operator_api.doc_read_tool``),
and the answer says so.

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
from dataclasses import dataclass, field
import hashlib
import io
import logging
import os
import re
from typing import Any, Iterable, Mapping, Optional, Sequence
import zipfile
from xml.etree import ElementTree

from operating.msg_reader import CFB_MAGIC

MAX_IMAGE_BYTES = 5 * 1024 * 1024   # the model's own limit on a picture: a larger one is scaled down
MAX_UNZIPPED = 60 * 1024 * 1024   # the zip-bomb bound (security)

IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg",
               b"GIF87a": "image/gif", b"GIF89a": "image/gif"}
KINDS = ("pdf", "spreadsheet", "text", "image")

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
    # a .docx/.xlsx/.pptx's own bytes (any file's when its text was cut), for the caller to
    # keep as the person's file (never in ``meta``, never shown); ``kept``: that file's
    # ``file:`` citation once kept (an editable one's); ``whole``: a cut file's whole text,
    # for the people-exclusion screen only, never shown
    data: Optional[bytes] = field(default=None, repr=False, compare=False)
    kept: Optional[str] = None
    whole: Optional[str] = field(default=None, repr=False, compare=False)
    # scanned pages the request's OCR cap left unread, and the scanned pages in all
    ocr_pending: int = 0
    ocr_total: int = 0

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
        return "legacy", None     # an old binary Office file (.xls, .doc): doc_read
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, None
    if "\x00" in text:
        return None, None
    return "text", "text/plain"


def _clip(text: str) -> tuple[str, bool]:
    """``text`` with its line ends made one; never cut (the share of the prompt it may take
    is ``decode``'s: a file's whole text is kept to read on)."""
    return text.replace("\r\n", "\n").replace("\r", "\n"), False


def _whole(name: str, data: bytes, kind: str
           ) -> tuple[str, bool, Optional[str], str, tuple[int, int]]:
    """A PDF, workbook or old binary Office file read whole (``operating.doc_read``: every
    page, a scanned page by local OCR when the server has it; every sheet and row, dates
    as dates): (the text, whole; whether parts were not read; the whole text when cut; its kind; (scanned pages OCR left for
    later, scanned pages in all): OCR reads at most ``doc_read.REQUEST_OCR_PAGES`` here)."""
    from operating import doc_read
    try:
        reading = doc_read.read(name, data, ocr=True, unzipped=MAX_UNZIPPED,
                                ocr_cap=doc_read.REQUEST_OCR_PAGES)
    except doc_read.Unread as why:
        said = str(why)
        if kind == "legacy":
            said = (said if "old Office file" in said else "It is not a file the Ask reads.") \
                + (" The Ask reads PDFs, spreadsheets (.xlsx, .xls, .csv), Word (.docx, .doc) "
                   "and PowerPoint (.pptx) files, text and images (PNG, JPEG, GIF, WebP); "
                   "save it as one of those.")
        elif "unpacks to more" in said:
            said = "It unpacks to more than the Ask reads."
        elif "not a file kind" in said:
            said = ("It is not a spreadsheet, Word or PowerPoint file: attach a PDF, a "
                    "spreadsheet, a .docx, a .pptx, text or an image.")
        elif kind == "pdf" and "no text" in said:
            said = ("It has no text to read (a scan OCR could not read): attach a screenshot "
                    "of the pages instead.")
        raise _Unread(said) from None
    text = reading.text.replace("\r\n", "\n")
    shown = text
    kind = {"pdf": "pdf", "spreadsheet": "spreadsheet"}.get(reading.kind, "text")
    if reading.unread:
        shown += "\n[not read: " + "; ".join(reading.unread) + "]"
    return (shown, bool(reading.unread), None, kind,
            (len(reading.ocr_pending), reading.ocr_total))


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


class _Unread(Exception):
    """A file not read, with the words the answer says about it."""


def fit_image(data: bytes, limit: int = 0) -> Optional[tuple[bytes, str]]:
    """``data`` (a PNG, JPEG, GIF or WebP) scaled down until it is at most ``limit`` bytes
    (default ``MAX_IMAGE_BYTES``): half the side each time, re-saved as PNG, or as JPEG when it
    has no transparency and PNG stays too large. (bytes, media type); None when Pillow is not
    installed or the picture cannot be read."""
    limit = limit or MAX_IMAGE_BYTES
    try:
        import io
        from PIL import Image
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 - an unreadable picture is said (the caller), not raised
        logging.getLogger("operating.ask_attachments").warning(
            "a picture could not be opened to scale it: %s", type(exc).__name__)
        return None
    scale = 1.0
    for _ in range(12):
        side = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        small = image if scale == 1.0 else image.resize(side)
        flat = small.mode in ("RGB", "L") or "transparency" not in small.info and small.mode != "RGBA"
        for fmt, media in (("PNG", "image/png"),) + ((("JPEG", "image/jpeg"),) if flat else ()):
            out = io.BytesIO()
            try:
                (small.convert("RGB") if fmt == "JPEG" else small).save(out, fmt)
            except Exception as exc:  # noqa: BLE001 - try the next format
                logging.getLogger("operating.ask_attachments").warning(
                    "a picture could not be saved as %s: %s", fmt, type(exc).__name__)
                continue
            if len(out.getvalue()) <= limit:
                return out.getvalue(), media
        scale *= 0.7
    return None


READ_MEMORY_FACTOR = 3   # a file's bytes, the parser's copy and its text


def _memory_available() -> Optional[int]:
    """Bytes of memory free for new use (``MemAvailable``), None where it cannot be read."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError) as exc:
        logging.getLogger("operating.ask_attachments").warning(
            "free memory unreadable: %s", type(exc).__name__)
    return None


def _bytes(item: Mapping[str, Any]) -> tuple[bytes, Optional[str]]:
    """The item's bytes (``path``: a file streamed to disk; ``data``: base64) and why there
    are none, in words."""
    if item.get("missing"):
        return b"", ("It is no longer held here (an upload is kept for a week, or until you "
                     "remove it): attach it again.")
    path = item.get("path")
    if path:
        try:
            size = os.path.getsize(path)
            # the parsers of a PDF or an Office file hold the document in memory (a copy and
            # its parse): said and refused when this host's free memory cannot, never an OOM
            # that takes the API down with it
            need, have = size * READ_MEMORY_FACTOR, _memory_available()
            if have is not None and need > have:
                return b"", (f"It is {size / 1024 ** 3:.1f} GB: reading it needs about "
                             f"{need / 1024 ** 3:.1f} GB of memory and the server has "
                             f"{have / 1024 ** 3:.1f} GB free right now. Ask again when it "
                             "is quieter, or send it in parts.")
            with open(path, "rb") as f:
                return f.read(), None
        except OSError as exc:
            logging.getLogger("operating.ask_attachments").warning(
                "an upload could not be read: %s", type(exc).__name__)
            return b"", "It could not be read from where it was uploaded: attach it again."
    try:
        return base64.b64decode(str(item.get("data") or ""), validate=True), None
    except (binascii.Error, ValueError) as exc:
        logging.getLogger("operating.ask_attachments").warning(
            "an inline attachment was not base64: %s", type(exc).__name__)
        return b"", None   # decode() says "arrived empty or damaged"


def decode(items: Iterable[Mapping[str, Any]]) -> Batch:
    """Each ``{"name", "path" | "data" (base64)}`` read (module doc), in order; no file is
    left out for how many or how large they are together."""
    from operating.context_window import PROMPT_FILES_CHARS
    batch, total, text_total = Batch(), 0, 0
    for index, item in enumerate(items):
        name = _name(item.get("name"))
        data, gone = _bytes(item)
        digest = hashlib.sha256(data).hexdigest()[:16]

        def unread(why: str, kind: Optional[str] = None) -> Attached:
            return Attached(name, kind, len(data), digest, unread=why)
        if not data:
            batch.files.append(unread(gone or "It arrived empty or damaged: attach it again."))
            continue
        kind, media = kind_of(data)
        if kind is None:
            batch.files.append(unread("The Ask reads PDFs, spreadsheets (.xlsx, .csv), Word "
                                      "(.docx) and PowerPoint (.pptx) files, text and images "
                                      "(PNG, JPEG, GIF, WebP); save it as one of those."))
            continue
        if kind == "image" and len(data) > MAX_IMAGE_BYTES:
            # the model's own image limit: a larger picture is made smaller to fit, never
            # refused (the original is not kept; the answer says it was scaled down)
            fitted = fit_image(data)
            if fitted is None:
                batch.files.append(unread("The picture is larger than the model reads "
                                          f"({MAX_IMAGE_BYTES // (1024 * 1024)} MB) and could "
                                          "not be scaled down here.", kind))
                continue
            data, media = fitted
        total += len(data)
        if kind == "image":
            batch.files.append(Attached(name, kind, len(data), digest, media_type=media,
                                        image=base64.b64encode(data).decode("ascii")))
            continue
        whole: Optional[str] = None
        pending = (0, 0)
        try:
            if media in (_DOCX, _PPTX):
                text, cut = {_DOCX: _docx_text, _PPTX: _pptx_text}[media](data)
            else:
                text, cut, whole, kind, pending = _whole(name, data, kind)
        except _Unread as why:
            # an old binary Office file it could not read has no kind the answer names
            batch.files.append(unread(str(why), None if kind == "legacy" else kind))
            continue
        except Exception:  # noqa: BLE001 - a damaged or unusual file: not read, never a guess
            batch.files.append(unread("It could not be read: attach it again, or save it "
                                      "as a PDF.", None if kind == "legacy" else kind))
            continue
        room = max(0, PROMPT_FILES_CHARS - text_total)
        if len(text) > room:
            # past the share of the prompt the files may take: the start is shown, the whole
            # text and the bytes are kept to read on (``plx.read_document``, offset pages on)
            whole = whole if whole is not None else text
            text, cut = text[:room], True
        text_total += len(text)
        batch.files.append(Attached(name, kind, len(data), digest, text=text, cut=cut,
                                    media_type=media,
                                    data=data if media in EDITABLE or cut else None,
                                    whole=whole if cut else None,
                                    ocr_pending=pending[0], ocr_total=pending[1]))
    return batch


def screened(files: Sequence[Attached]) -> list[Attached]:
    """``files`` as they are: no file is withheld for its content (the owner dropped the
    Freightcom rule 11-23); kept as the one call point the app and the instruct path use."""
    return list(files)


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


__all__ = ["Attached", "Batch", "EDITABLE", "KINDS", "MAX_IMAGE_BYTES",
           "decode", "kind_of", "prompt_block", "screened"]
