"""Closed-world accounting of every part of one delivered use-case artifact.

One invariant replaces per-format special cases: every part of a captured
artifact is accounted exactly once, as

* ``extracted``: typed text was actually read by the reader named in ``method``
  (``chars`` may be 0: the reader parsed the part and it holds no text);
* ``unread``: with its ``kind`` and a ``reason`` from ``REASONS``;
* ``skipped_by_cap``: with the ``cap`` (``CAPS``) and its ``limit`` (pages of a PDF
  also with their range, ``first_page`` to ``last_page``).

A container is enumerated first and then decomposed into its parts: a zip
(members, recursively; a member that is itself a zip, an Office package, a
mail or a PDF is decomposed in turn), an OOXML package (every package part), a
MIME message (headers, every leaf entity, preamble/epilogue; attached messages
recursively), an Outlook ``.msg`` (every stream and storage of the compound
file; attachments and attached messages recursively) and a PDF (document
information, every catalog entry, every page's text, images and annotations,
every embedded file recursively). The enumeration is structural and never
asks a reader: a MIME entity is classified by its main type, never by an
enumerated subtype (a ``multipart/*`` entity recurses into its entities; a
``message/*`` entity carrying parsed messages, e.g. rfc822, global, a read
receipt's disposition-notification or a bounce's delivery-status blocks, gives
each carried header block its own part and recurses into its payload; every
other entity is a leaf), an OOXML package by its part names, a zip by its
members (directory entries holding data and member comments included), a
compound file by its directory. Only then is each
part handed to a reader,
and only a part whose kind is in the reader table below is read. Every other
part, including kinds nobody has seen yet, is ``unread`` by default, so a new
kind can never be counted as extracted. Inside an OOXML part that was read,
every element kind that carries text the reader did not consume (tracked
deletions, unknown markup) is its own ``unread`` part (``element_not_read``),
so a reader gap shows up as unread, never as silent omission. ``complete`` is
true only when nothing
is ``unread`` or ``skipped_by_cap``.

Readers (what ``extracted`` means for each):

* utf-8 text (md/txt/csv), HTML (visible text, script/style dropped);
* OOXML: package relationships and content types (external link targets),
  WordprocessingML body/headers/footers/notes/comments (``w:t`` incl. text
  boxes, math ``m:t``, field codes ``w:instrText``, alt text), DrawingML slides/layouts/masters/notes and spreadsheet
  drawings (``a:t``, alt text), SpreadsheetML workbook, cells (shared strings
  resolved, inline strings, values, formulas, header/footer) and shared
  strings, and the formatting/metadata parts whose text is character data
  (styles, settings, themes, ``docProps/core.xml``/``app.xml``, cell comments);
* mail: RFC 822 headers, text/plain and text/html bodies (an empty plain body
  next to an HTML one leaves both read: the HTML carries the text);
* PDF: page text comes from the one reader
  (``integrations.document_understanding.read_pages``, the only place that chooses
  how a PDF is read): optional ``pypdf`` page by page (``pypdf-text``) first, when it
  reads every page and some text; else MarkItDown (Portal's converter, optional;
  method ``markitdown``) when installed, its pages split at its form feeds when they
  match the page count (MarkItDown converts whole files, so it runs only on a PDF
  whose page count, ``pypdf``'s or poppler's, is known and within the bound); else
  ``pypdf``'s partial or empty reading; else, when ``pypdf`` cannot be imported or
  panics, each page's text layer through local poppler (``pdftotext-text``, no OCR).
  So ``markitdown`` reads only the PDFs ``pypdf`` does not. When MarkItDown gives no
  page boundaries its text is one unpaged part (``<pdf>!/text``): no second reader
  reads the whole PDF again. The page count is bounded before a page is read: the
  first pages are read, at most ``MAX_PAGES`` of
  ``integrations.document_understanding`` and no more than the parts room left, and
  the rest are ``skipped_by_cap`` page ranges (``<pdf>!/page/<first>-<last>``): past ``MAX_PAGES`` cap ``pages``, else ``parts``, so the record
  is never complete. Every page part records the PDF's ``source_sha256`` and its
  ``method`` (MarkItDown text also the converter record's identity). The rest of the
  document comes from the reader ``pypdf`` opened: document information, XMP
  metadata, outline titles, link targets and markup-annotation text. Page images
  are image parts (below); form fields and every other catalog entry, name tree and annotation
  are ``unread`` (``pdf_entry_not_parsed``) with their ``entry``
  (``PDF_ENTRIES``: where it is and its PDF name, e.g. ``catalog:/AcroForm``,
  ``names:/JavaScript``, ``annotation:/Widget``; a name the PDF standard does not
  define is ``<where>:other``, never the file's own name). An encrypted, malformed,
  image-only or over-cap PDF or entry has its own reason (``encrypted``,
  ``container_unreadable``/``unreadable``, ``image_only``, ``skipped_by_cap``).
  ``pypdf`` loads native (Rust/pyo3) code, ``cryptography``, on import and for encrypted files: its
  panic (``pyo3_runtime.PanicException``) derives from ``BaseException``, so
  each ``pypdf`` call also contains it and the part is ``unread``
  (``parser_panicked``); any other ``BaseException`` still ends the run. When
  ``pypdf`` did not open the document, the rest of it (information, catalog,
  images, annotations, embedded files) is one ``unread`` part
  (``pdf_text_layer_only``). A page without text there is ``unread``
  (``image_only``): its images are not enumerated there, so an empty page is never
  counted as read. A PDF that needs a password is ``encrypted``; one over the text
  bound of the poppler path is ``skipped_by_cap`` (``text_chars``: its ``MAX_TEXT``),
  and one over the page bound whose first pages no page-by-page reader reads is
  ``skipped_by_cap`` (``pages``) whole;
  the PDF is ``parser_panicked`` (or
  ``pdf_parser_unavailable``) only when every reader fails otherwise, and
  ``container_unreadable`` when ``pypdf`` finds it malformed and MarkItDown did not
  read it.

* ``.msg`` (``operating.msg_reader``): every property table (sent/received
  times, flags, sizes), string properties (subject, sender, recipients, body,
  ...), the HTML and compressed RTF bodies, the named-property map,
  identifier properties (parsed, no text; the part's ``property`` is its
  MS-OXPROPS name) and the binary properties that hold an address (search keys,
  one-off and address book entry ids, reply-to entries: method ``msg-address``,
  the address as text, or unread when it does not decode); any other binary
  (``0102``) property that is protocol metadata, a MAPI-defined one (id below
  ``0x6800``; the body, attachment, identifier and address binaries are read above)
  or a named one in a Microsoft property set (``MAPI_METADATA_SETS``), is
  ``unread`` (``non_content_metadata``) with its ``tag``: kept and counted, outside
  the text denominator, never read as text; a message-class-defined id
  (``0x6800``-``0x7FFF``) or a named binary of any other set may hold an
  application's content and is not metadata. Every other property
  is ``unread`` (``msg_property_not_decoded``) with its ``tag`` (property type and
  id, a named property by its numeric name: schema only, never a value; a
  property table's entries that decode are read and those that do not are one
  ``<table>#undecoded`` part whose ``tag`` lists each of them, the whole table
  unread with that ``tag`` only when no part room is left to split it);
* images (PNG, JPEG, GIF, TIFF, BMP, WebP; also images inside PDF pages):
  OCR by the caller's ``ocr`` reader (``local_image_ocr``: the one local tesseract
  path, ``integrations.document_understanding.local_image_text``,
  method ``tesseract-ocr``, machine-read text). Without one an image is unread
  (``ocr_unavailable``); a vector or unknown image format is unread
  (``image_format_unsupported``); OCR time per artifact is capped
  (``ocr_seconds``), and a caller's own deadline (``remaining``) bounds each OCR
  and starts none once it has passed (``budget_seconds``).

Every part carries the ``format`` (``FORMATS``) of the innermost file it belongs
to (a docx inside a zip: ``docx``; an image inside that docx: ``image``), and
``METHOD_FAMILIES`` groups the reading methods by converter (``stdlib``,
``pypdf``, ``pdftotext``, ``ocr``, ``markitdown``), so a per-format denominator is derived from
the parts themselves. A part recorded before parts carried a format gets the format
of its own kind and path (:func:`part_format`), never the outer file's.

The joined text holds each
extracted part after a display-only ``[Part: <path>]`` marker; each part's
``start``/``end`` span is authoritative (part text may contain a forged
marker line). Source content is data: it is transcribed, never interpreted.
Nothing here fetches or writes except through the caller's ``pin``.

:func:`members` is the same enumeration for the projection (``integrations
.captured_text.container_members``): the file members of a container (a zip that
is not an OOXML package, a MIME message, an Outlook ``.msg``), recursively through
nested containers, with their bytes, and every part of the containers' structure
that was not read (an encrypted or over-cap member, an undecoded ``.msg``
property). A member file is not decomposed or read here: its text is the one
reading of captured files (``integrations.captured_text``), so a member is read
like any other captured file. It is the one walk of a mail's attachments, so each
part is read once: a mail's own text (its headers and the body entities of
``integrations.document_understanding.mail_body``; a ``.msg``'s properties) is the
container's text, read with the container itself (a body that does not decode is a
gap, ``text_encoding_unsupported``), and every other MIME leaf (inline text included)
and every ``.msg`` attachment is a member. A nested mail is one rule in both formats:
it is a container member, its own text its reading and its attachments its members
(under the same depth bound), whether attached as a file (an ``.eml`` or ``.msg``) or
carried: a ``message/rfc822`` or ``message/global`` entity is a member of its bytes
(the entity's body, written out with the container's line ends: the source's bytes
for a canonically written message; a body stated ``base64`` or ``quoted-printable`` is
the source's raw body decoded before any of it is parsed), named by the entity's file
name, else ``<Subject>.eml`` (the subject shown, the stated type the only suffix); an
embedded ``.msg`` message (a storage) a member
of the ``.msg`` it is saved as (``operating.msg_reader.embedded_message``). Any other
``message/*`` entity (a bounce's delivery-status blocks, a read receipt) carries no
message: it is a text member of its body. A carried message whose bytes cannot be
had is one gap (``carried_message_not_read``); an embedded ``.msg`` one is then
walked in place, so its attachments are still members.
"""

from __future__ import annotations

from base64 import b64decode
from contextlib import contextmanager
from email import policy
from email.generator import BytesGenerator
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
import quopri
import re
import struct
import time
from typing import Any, Callable, Iterable, Iterator
from uuid import UUID
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
import zlib

from operating import msg_reader


ACCOUNTING_SCHEMA = "plx-use-case-parts/v1"
EXTRACTED, UNREAD, SKIPPED = "extracted", "unread", "skipped_by_cap"
STATES = (EXTRACTED, UNREAD, SKIPPED)
MAX_PART_BYTES = 20_000_000  # one part held in memory
MAX_PARTS = 5_000  # parts of one artifact
MAX_EXPANDED_BYTES = 400_000_000  # decompressed archive bytes of one artifact
MAX_DEPTH = 8  # nested containers
MAX_TEXT_CHARS = 2_000_000  # extracted text kept per artifact
MAX_OCR_SECONDS = 600  # OCR time of one artifact
OCR_TIMEOUT_SECONDS = 60  # one image
REASONS = ("ocr_unavailable", "ocr_failed", "image_format_unsupported", "unknown_kind",
           "ooxml_part_not_parsed", "xml_unparseable", "container_unreadable", "encrypted",
           "unreadable", "text_encoding_unsupported", "pdf_parser_unavailable",
           "pdf_entry_not_parsed", "msg_property_not_decoded", "msg_storage_not_parsed",
           "element_not_read", "parser_panicked", "pdf_text_layer_only",
           "carried_message_not_read", "image_only", "non_content_metadata")
CAPS = ("original_bytes", "part_bytes", "parts", "expanded_bytes", "depth", "text_chars",
        "ocr_seconds", "pages", "budget_seconds")
KINDS = ("original", "text", "html", "image", "unknown", "zip", "zip_member", "zip_comment",
         "ooxml_part", "ooxml_element", "mail_headers", "mail_body", "mime_text", "cfb", "msg_property",
         "msg_storage", "pdf", "pdf_info", "pdf_page", "pdf_catalog_entry", "pdf_annotation",
         "pdf_form_field", "container")
METHODS = ("utf8-text", "html-text", "zip-comment", "opc-relationships", "wordml-text",
           "drawingml-text", "sml-workbook", "sml-cells", "sml-shared-strings", "xml-chardata",
           "rfc822-headers", "mime-text", "msg-property", "msg-property-table", "msg-rtf",
           "msg-identifier", "msg-address", "msg-named-properties", "pypdf-text", "pdf-info", "pdf-outline",
           "pdf-annotation", "tesseract-ocr", "pdftotext-text", "markitdown")
FORMATS = ("pdf", "docx", "pptx", "xlsx", "vsdx", "ooxml", "msg", "eml", "zip", "image",
           "text", "html", "other")
# Converter of each reading method; every other method is stdlib code of this module.
FAMILIES = ("stdlib", "pypdf", "pdftotext", "ocr", "markitdown")
METHOD_FAMILIES = {"pypdf-text": "pypdf", "pdf-info": "pypdf", "pdf-outline": "pypdf",
                   "pdf-annotation": "pypdf", "pdftotext-text": "pdftotext",
                   "tesseract-ocr": "ocr", "markitdown": "markitdown"}
# The PDF names (ISO 32000-2 tables 29, 32 and 171) a ``pdf_entry_not_parsed`` part is
# typed by, per place: catalog entries, name trees and annotation subtypes no reader
# here reads (the ones read, or structure only, are not listed).
_PDF_UNPARSED = {
    "catalog": ("/Extensions", "/Dests", "/Threads", "/OpenAction", "/AA", "/URI",
                "/AcroForm", "/StructTreeRoot", "/SpiderInfo", "/OutputIntents",
                "/PieceInfo", "/OCProperties", "/Perms", "/Legal", "/Requirements",
                "/Collection", "/NeedsRendering", "/DSS", "/AF", "/DPartRoot"),
    "names": ("/AP", "/JavaScript", "/Pages", "/Templates", "/IDS", "/URLS",
              "/AlternatePresentations", "/Renditions"),
    "annotation": ("/Widget", "/Sound", "/Movie", "/Screen", "/PrinterMark", "/TrapNet",
                   "/Watermark", "/3D", "/Redact", "/Projection", "/RichMedia")}
PDF_ENTRIES = tuple(f"{where}:{name}" for where, names in _PDF_UNPARSED.items()
                    for name in (*names, "other"))
VOCABULARY = frozenset((ACCOUNTING_SCHEMA, *STATES, *REASONS, *CAPS, *KINDS, *METHODS,
                        *FORMATS, *FAMILIES, *PDF_ENTRIES, *msg_reader.OPAQUE_BINARY.values(),
                        *msg_reader.ADDRESS_BINARY.values()))

_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00",
                b"MM\x00*")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emf",
                   ".wmf", ".svg", ".webp"}
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv"}
_SUFFIX_FORMATS = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx",
                   ".vsdx": "vsdx", ".msg": "msg", ".eml": "eml", ".zip": "zip",
                   ".html": "html", ".htm": "html", **dict.fromkeys(_IMAGE_SUFFIXES, "image"),
                   **dict.fromkeys(_TEXT_SUFFIXES, "text")}
_ZIP_ERRORS = (BadZipFile, NotImplementedError, OSError, EOFError, RuntimeError, zlib.error,
               ValueError)


class _Ledger:
    """The parts of one artifact, in accounting order, and their joined text."""

    def __init__(self, pin: Callable[[bytes], str] | None,
                 ocr: Callable[[bytes], str] | None = None,
                 files: list[dict[str, Any]] | None = None,
                 part_bytes: int | None = None,
                 remaining: Callable[[], float] | None = None) -> None:
        self.ocr, self.ocr_seconds = ocr, 0.0
        self.remaining = remaining  # seconds left of the caller's deadline
        # one nested file or zip member held in memory (the module bound when not given)
        self.part_bytes = MAX_PART_BYTES if part_bytes is None else part_bytes
        self.format = "other"  # of the file being decomposed (``FORMATS``)
        # Members mode (:func:`members`): every member file is kept here, undecomposed
        # unless it is a container, and no part text is kept.
        self.files = files
        self.parts: list[dict[str, Any]] = []
        self._paths: set[str] = set()
        self._chunks: list[str] = []
        self.length = 0
        self.expanded = 0
        self.reserved = 0  # parts room held for parts still to be written
        self._pin = pin

    def left(self) -> int:
        """The parts room left, less what is reserved."""
        return MAX_PARTS - len(self.parts) - self.reserved

    def room(self, count: int) -> bool:
        return count <= self.left()

    @contextmanager
    def reserve(self, count: int) -> Iterator[None]:
        """Hold room for ``count`` parts written after this block: inside it every
        room check sees that much less."""
        self.reserved += count
        try:
            yield
        finally:
            self.reserved -= count

    def _add(self, path: str, kind: str, data: bytes | None, size: int | None, state: str,
             **fields: Any) -> dict[str, Any]:
        unique, n = path, 1
        while unique in self._paths:  # a zip may hold one name twice: both are parts
            n += 1
            unique = f"{path}~{n}"
        self._paths.add(unique)
        part = {"path": unique, "kind": kind,
                "size": len(data) if data is not None else size,
                "sha256": sha256(data).hexdigest() if data is not None else None,
                "state": state, "format": self.format, **fields}
        self.parts.append(part)
        return part

    def read(self, path: str, kind: str, data: bytes | None, text: str, method: str,
             **fields: Any) -> None:
        if self.files is not None:
            self._add(path, kind, data, None, EXTRACTED, method=method, chars=len(text),
                      **fields)
            return
        marker = ("\n[Part: " + re.sub(r"[\]\r\n]", "_", path) + "]\n") if text else ""
        if self.length + len(marker) + len(text) > MAX_TEXT_CHARS:
            self.skipped(path, kind, data, None, "text_chars", MAX_TEXT_CHARS)
            return
        part = self._add(path, kind, data, None, EXTRACTED, method=method, chars=len(text),
                         **fields)
        if text:
            part["start"] = self.length + len(marker)
            part["end"] = part["start"] + len(text)
            self._chunks.append(marker + text)
            self.length += len(marker) + len(text)

    def unread(self, path: str, kind: str, data: bytes | None, reason: str,
               size: int | None = None, **fields: Any) -> None:
        part = self._add(path, kind, data, size, UNREAD, reason=reason, **fields)
        # What was not read is kept as its own original: a person can open it.
        if data is not None and self._pin is not None:
            part["stored"] = self._pin(data)

    def skipped(self, path: str, kind: str, data: bytes | None, size: int | None, cap: str,
                limit: int, **fields: Any) -> None:
        self._add(path, kind, data, size, SKIPPED, cap=cap, limit=limit, **fields)

    def result(self) -> dict[str, Any]:
        counts = {state: sum(p["state"] == state for p in self.parts) for state in STATES}
        return {"schema": ACCOUNTING_SCHEMA, "parts": self.parts, "counts": counts,
                "complete": counts[UNREAD] == 0 and counts[SKIPPED] == 0,
                "text": "".join(self._chunks)}


def account(content: bytes, name: str, *, pin: Callable[[bytes], str] | None = None,
            ocr: Callable[[bytes], str] | None = None,
            max_part_bytes: int | None = None,
            remaining: Callable[[], float] | None = None) -> dict[str, Any]:
    """Every part of ``content`` (named ``name``) accounted exactly once, plus its text.

    ``pin`` stores the bytes of each ``unread`` part (returns its stored path);
    ``ocr`` reads the text of one image (``local_image_ocr``) or raises ``ValueError``;
    ``max_part_bytes`` bounds one nested file or zip member (``part_bytes`` cap;
    default ``MAX_PART_BYTES``); ``remaining`` returns the seconds left of the
    caller's deadline: each OCR is then called with ``timeout=`` at most that, and an
    image met after the deadline is ``skipped_by_cap`` (``budget_seconds``)."""
    ledger = _Ledger(pin, ocr, part_bytes=max_part_bytes, remaining=remaining)
    _part(ledger, content, name or "original", name or "", 0)
    return ledger.result()


def members(content: bytes, name: str, hint: str | None = None, *,
            max_part_bytes: int | None = None) -> dict[str, Any] | None:
    """The file members of a container (module docstring), or None when ``content`` is
    not one (``hint``: its MIME type, which types a mail without an ``.eml`` name).
    ``files``: per member file, in enumeration order, its ``path`` (relative to the
    container, e.g. ``inner.zip!/quote.pdf``), ``name``, MIME ``hint`` (a mail entity's
    content type, else None), ``data`` and ``sha256``, and ``container`` (it was
    decomposed: its own members follow). ``gaps``: every structural part that was not
    read (``unread`` with its ``reason``, ``skipped_by_cap`` with its ``cap``), paths
    relative the same way (``.``: the container itself, e.g. a zip that cannot be
    opened). ``max_part_bytes`` as in :func:`account`."""
    top = name or "original"
    if not is_container(content, name, hint):
        return None
    files: list[dict[str, Any]] = []
    ledger = _Ledger(None, None, files, part_bytes=max_part_bytes)
    _part(ledger, content, top, name or "", 0, hint)
    cut = len(top) + 2   # "<top>!/"

    def relative(path: str) -> str:   # "." is the container itself
        return path[cut:] if path.startswith(top + "!/") else path[len(top):] or "."

    for item in files:
        item["path"] = relative(item["path"])
    gaps = [{**part, "path": relative(part["path"])} for part in ledger.parts
            if part["state"] != EXTRACTED]
    return {"files": files, "gaps": gaps}


def is_container(content: bytes, name: str, hint: str | None = None) -> bool:
    """Whether :func:`members` reads ``content`` member by member (a mail, a zip that is
    not an Office package, an Outlook ``.msg``), without reading its members."""
    kind = _sniff(content, name, hint)
    return (kind == "mail" or (kind == "zip" and not _is_package(content))
            or (kind == "cfb" and _is_msg(content)))


def _is_package(data: bytes) -> bool:
    try:
        with ZipFile(BytesIO(data)) as archive:
            return any(i.filename == "[Content_Types].xml" for i in archive.infolist())
    except _ZIP_ERRORS:
        return False


def _is_msg(data: bytes) -> bool:
    try:
        cfb = msg_reader.Cfb(data)
        return any(cfb.entries[i][0] == msg_reader.PROPERTIES for i in cfb.children(0))
    except _CFB_ERRORS:
        return False


def capped(name: str, size: int | None, limit: int) -> dict[str, Any]:
    """The accounting of an original that was not read at all (over the intake cap)."""
    ledger = _Ledger(None)
    ledger.format = _name_format(name, "other")
    ledger.skipped(name or "original", "original", None, size, "original_bytes", limit)
    return ledger.result()


def _name_format(path: str, default: str) -> str:
    """The format the file name of a part path states (its last ``!/`` segment, less a
    ``~n`` duplicate mark or a ``#`` sub-part when the name does not end in a suffix)."""
    name = path.rsplit("!/", 1)[-1]
    while True:
        found = _SUFFIX_FORMATS.get(PurePosixPath(re.sub(r"~\d+$", "", name)).suffix.lower())
        if found or "#" not in name:
            return found or default
        name = name.rsplit("#", 1)[0]


_KIND_OWN_FORMATS = {
    "image": "image", "text": "text", "html": "html", "unknown": "other",
    "mail_headers": "eml", "mail_body": "eml", "mime_text": "eml",
    "cfb": "msg", "msg_property": "msg", "msg_storage": "msg",
    **{kind: "pdf" for kind in KINDS if kind.startswith("pdf")}}
_PACKAGE_SUFFIX_FORMATS = ("docx", "pptx", "xlsx", "vsdx")


# A record's sniffed ``detected_type`` (``operating.use_case_intake.TYPES``) as a format.
_TYPE_FORMATS = {"png": "image", "jpg": "image", "md": "text", "txt": "text"}


def part_format(part: dict[str, Any], detected_type: str | None = None) -> str:
    """The format (``FORMATS``) of the file a part belongs to: its recorded ``format``.
    A part recorded before parts carried one gets it from itself, never from the
    outer file it sits in: its kind, else (a whole file: ``original``, ``container``,
    ``zip``) its own name, else (a part of a zip or an OOXML package) the name of the
    file holding it. When that file is the record's own (its path has no ``!/``) and its
    name states no format, it is the record's sniffed ``detected_type``."""
    if part.get("format") is not None:
        return part["format"] if part["format"] in FORMATS else "other"
    kind, path = part.get("kind"), str(part.get("path") or "")
    if not isinstance(kind, str):
        return "other"
    if kind in _KIND_OWN_FORMATS:
        return _KIND_OWN_FORMATS[kind]
    sniffed = _TYPE_FORMATS.get(str(detected_type), detected_type)
    sniffed = sniffed if sniffed in FORMATS else "other"
    if kind in ("original", "container", "zip"):
        return _name_format(path, "zip" if kind == "zip" else
                            "other" if "!/" in path else sniffed)
    holder = path.rsplit("!/", 1)[0]
    holder = _name_format(holder, "other" if "!/" in holder else sniffed)
    package = holder if holder in _PACKAGE_SUFFIX_FORMATS else None
    if kind in ("ooxml_part", "ooxml_element"):
        return package or "ooxml"
    if kind in ("zip_member", "zip_comment"):
        return package or "zip"
    return "other"  # a kind nobody listed


# ------------------------------------------------------------------ dispatch

def _sniff(data: bytes, name: str, hint: str | None) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"
    if data.startswith(msg_reader.CFB_MAGIC):
        return "cfb"
    if data.startswith(_IMAGE_MAGIC) or suffix in _IMAGE_SUFFIXES or (
            hint or "").startswith("image/"):
        return "image"
    if (hint or "").startswith("message/") or suffix == ".eml":
        return "mail"
    if hint == "text/html" or suffix in (".html", ".htm"):
        return "html"
    if suffix in _TEXT_SUFFIXES or hint == "text/plain":
        return "text"
    return "unknown"


def _part(ledger: _Ledger, data: bytes, path: str, name: str, depth: int,
          hint: str | None = None) -> None:
    if depth and len(data) > ledger.part_bytes:  # the original itself has the intake bound
        ledger.skipped(path, "container", None, len(data), "part_bytes", ledger.part_bytes)
        return
    kind = _sniff(data, name, hint)
    outer = ledger.format
    ledger.format = _KIND_FORMATS.get(kind, "other")
    try:
        _read_part(ledger, data, path, name, depth, hint, kind)
    finally:
        ledger.format = outer


_KIND_FORMATS = {"pdf": "pdf", "zip": "zip", "cfb": "msg", "mail": "eml", "image": "image",
                 "text": "text", "html": "html"}
_PACKAGE_FORMATS = {"word/document.xml": "docx", "xl/workbook.xml": "xlsx",
                    "ppt/presentation.xml": "pptx", "visio/document.xml": "vsdx"}


def _read_part(ledger: _Ledger, data: bytes, path: str, name: str, depth: int,
               hint: str | None, kind: str) -> None:
    if ledger.files is not None and depth:
        # Members mode: a member file is kept with its bytes; only a container (a
        # plain zip, a mail, a .msg) is decomposed, everything else is read elsewhere.
        container = (kind == "mail" or (kind == "zip" and not _is_package(data))
                     or (kind == "cfb" and _is_msg(data)))
        # Accounted as handed over; its unique path (a zip may hold one name twice).
        part = ledger._add(path, "container" if container else kind, data, None, EXTRACTED,
                           method="member")
        ledger.files.append({"path": part["path"],
                             "name": PurePosixPath(name).name if name else "",
                             "hint": hint, "data": data, "sha256": part["sha256"],
                             "container": container})
        if not container:
            return
    if kind in ("zip", "mail", "cfb", "pdf") and depth >= MAX_DEPTH:
        ledger.skipped(path, "container", data, None, "depth", MAX_DEPTH)
    elif kind == "zip":
        _zip(ledger, data, path, depth)
    elif kind == "mail":
        _mail(ledger, data, path, depth)
    elif kind == "cfb":
        _msg(ledger, data, path, depth)
    elif kind == "pdf":
        _pdf(ledger, data, path, depth)
    elif kind in ("text", "html"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            ledger.unread(path, kind, data, "text_encoding_unsupported")
            return
        if kind == "html":
            ledger.read(path, kind, data, _html(text), "html-text")
        else:
            ledger.read(path, kind, data, text, "utf8-text")
    elif kind == "image":
        _image(ledger, data, path)
    else:
        ledger.unread(path, "unknown", data, "unknown_kind")


_OCR_FORMATS = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00",
                b"MM\x00*", b"BM")


def _image(ledger: _Ledger, data: bytes, path: str) -> None:
    if not (data.startswith(_OCR_FORMATS) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        ledger.unread(path, "image", data, "image_format_unsupported")  # EMF, WMF, SVG, ...
    elif ledger.ocr is None:
        ledger.unread(path, "image", data, "ocr_unavailable")
    elif ledger.ocr_seconds >= MAX_OCR_SECONDS:
        ledger.skipped(path, "image", data, None, "ocr_seconds", MAX_OCR_SECONDS)
    elif ledger.remaining is not None and ledger.remaining() <= 0:
        ledger.skipped(path, "image", data, None, "budget_seconds", 0)
    else:
        started = time.monotonic()
        try:
            if ledger.remaining is None:
                text = ledger.ocr(data)
            else:
                text = ledger.ocr(data, timeout=min(OCR_TIMEOUT_SECONDS, ledger.remaining()))
        except ValueError:
            if ledger.remaining is not None and ledger.remaining() <= 0:
                # stopped by the caller's deadline, not unreadable
                ledger.skipped(path, "image", data, None, "budget_seconds", 0)
            else:
                ledger.unread(path, "image", data, "ocr_failed")
            return
        finally:
            ledger.ocr_seconds += time.monotonic() - started
        ledger.read(path, "image", data, text, "tesseract-ocr")


def local_image_ocr() -> Callable[..., str] | None:
    """OCR of one image through the one local OCR path
    (``integrations.document_understanding.local_image_text``: tesseract, never a
    network service), or None when it is not installed. A failure raises ``ValueError``;
    an optional ``timeout`` shortens the per-image ``OCR_TIMEOUT_SECONDS``."""
    from integrations import document_understanding

    if not document_understanding.local_image_ocr_available():
        return None
    return lambda data, timeout=OCR_TIMEOUT_SECONDS: document_understanding.local_image_text(
        data, timeout=min(timeout, OCR_TIMEOUT_SECONDS))


def _html(source: str) -> str:
    from integrations.document_understanding import html_text

    return html_text(source)


# ------------------------------------------------------------------ zip and OOXML

def _zip(ledger: _Ledger, data: bytes, path: str, depth: int) -> None:
    try:
        archive = ZipFile(BytesIO(data))
    except _ZIP_ERRORS:
        ledger.unread(path, "zip", data, "container_unreadable")
        return
    with archive:
        # A directory entry is a part only when it holds data (it is never empty
        # by accident: a writer put bytes there).
        infos = [info for info in archive.infolist() if not info.is_dir() or info.file_size]
        comments = sum(bool(info.comment.strip()) for info in infos)
        if not ledger.room(len(infos) + comments + 1):
            ledger.skipped(path, "zip", data, None, "parts", MAX_PARTS)
            return
        package = any(info.filename == "[Content_Types].xml" for info in infos)
        if package:
            ledger.format = next((_PACKAGE_FORMATS[i.filename] for i in infos
                                  if i.filename in _PACKAGE_FORMATS), "ooxml")
        members: list[tuple[str, str, bytes]] = []
        later = len(infos) + comments + 1  # the room checked: held for the parts after each
        for info in infos:
            later -= 1 + bool(info.comment.strip())
            child = f"{path}!/{info.filename}"
            if info.comment.strip():
                ledger.read(f"{child}#comment", "zip_comment", info.comment,
                            info.comment.decode("utf-8", "replace"), "zip-comment")
            body = _member(ledger, archive, info, child)
            if body is None:
                continue
            if package:
                members.append((info.filename, child, body))
            else:
                with ledger.reserve(later):
                    _part(ledger, body, child, info.filename, depth + 1)
        if archive.comment.strip():
            ledger.read(f"{path}#comment", "zip_comment", archive.comment,
                        archive.comment.decode("utf-8", "replace"), "zip-comment")
    if package:
        _package(ledger, members, depth)


def _member(ledger: _Ledger, archive: ZipFile, info: Any, path: str) -> bytes | None:
    """The bytes of one member, or None when it was accounted as unread/skipped."""
    if info.flag_bits & 1:
        ledger.unread(path, "zip_member", None, "encrypted", size=info.file_size)
        return None
    if info.file_size > ledger.part_bytes:
        ledger.skipped(path, "zip_member", None, info.file_size, "part_bytes", ledger.part_bytes)
        return None
    if ledger.expanded + info.file_size > MAX_EXPANDED_BYTES:
        ledger.skipped(path, "zip_member", None, info.file_size, "expanded_bytes",
                       MAX_EXPANDED_BYTES)
        return None
    try:
        with archive.open(info) as handle:
            body = handle.read(ledger.part_bytes + 1)
    except _ZIP_ERRORS:  # bad CRC, broken header, unsupported compression
        ledger.unread(path, "zip_member", None, "unreadable", size=info.file_size)
        return None
    if len(body) > ledger.part_bytes:  # the declared size understated the member
        ledger.skipped(path, "zip_member", None, len(body), "part_bytes", ledger.part_bytes)
        return None
    ledger.expanded += len(body)
    return body


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
SML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
# Element kinds whose character data is drawing layout (anchor offsets), not text.
_LAYOUT = frozenset({"posOffset", "align", "pctWidth", "pctHeight", "col", "colOff", "row",
                     "rowOff"})
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_SHARED_STRINGS = "xl/sharedStrings.xml"


def _xml(body: bytes) -> ET.Element | None:
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        return None  # package parts never declare entities
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def _alt(node: ET.Element) -> str | None:
    if node.tag.rsplit("}", 1)[-1] not in ("docPr", "cNvPr"):
        return None
    alt = " ".join(v for v in (node.get("title"), node.get("descr")) if v)
    return f"[Alt: {alt}]" if alt else None


def _runs(root: ET.Element, para: str, texts: dict[str, str], tab: str,
          breaks: tuple[str, ...]) -> str:
    """Paragraph text of a WordprocessingML/DrawingML part (nested paragraphs, e.g. a
    text box inside a paragraph, become their own lines; markup-compatibility
    fallbacks repeat their choice and are skipped)."""
    lines: list[str] = []
    line: list[str] = []

    def flush() -> None:
        if line:
            lines.append("".join(line))
            line.clear()

    def walk(node: ET.Element) -> None:
        if node.tag == _MC_FALLBACK:
            return
        if node.tag in (para, *breaks):
            flush()
        elif node.tag in texts:
            line.append(texts[node.tag].format(node.text or ""))
        elif node.tag == tab:
            line.append("\t")
        alt = _alt(node)
        if alt:
            flush()
            lines.append(alt)
        for child in node:
            walk(child)
        if node.tag == para:
            flush()

    walk(root)
    flush()
    return "\n".join(lines)


def _opc(root: ET.Element, shared: list[str], used: set[int]) -> str:
    return "\n".join(f"[Link: {node.get('Target')}]" for node in root.iter()
                     if node.get("TargetMode") == "External" and node.get("Target"))


_WORD_TEXT = {W + "t": "{}", M + "t": "{}", W + "instrText": "[Field: {}]"}


def _wordml(root: ET.Element, shared: list[str], used: set[int]) -> str:
    return _runs(root, W + "p", _WORD_TEXT, W + "tab", (W + "br", W + "cr"))


def _drawingml(root: ET.Element, shared: list[str], used: set[int]) -> str:
    return _runs(root, A + "p", {A + "t": "{}"}, A + "tab", (A + "br",))


def _workbook(root: ET.Element, shared: list[str], used: set[int]) -> str:
    lines = [f"[Sheet: {sheet.get('name')}]" for sheet in root.iter(SML + "sheet")]
    lines += [f"{name.get('name')}={name.text or ''}" for name in root.iter(SML + "definedName")]
    return "\n".join(lines)


def _cells(root: ET.Element, shared: list[str], used: set[int]) -> str:
    lines = []
    for cell in root.iter(SML + "c"):
        value_node, formula = cell.find(SML + "v"), cell.find(SML + "f")
        if cell.get("t") == "s" and value_node is not None:
            index = int(value_node.text or "")
            value = shared[index]  # an unresolved reference fails the read: unread
            used.add(index)
        elif cell.get("t") == "inlineStr":  # a <v> next to the inline string is read too
            value = " ".join(v for v in ("".join(t.text or "" for t in cell.iter(SML + "t")),
                                         value_node.text if value_node is not None else "")
                             if v)
        else:
            value = value_node.text or "" if value_node is not None else ""
        if formula is not None and formula.text:
            value = f"{value} (={formula.text})"
        if value:
            lines.append(f"{cell.get('r') or '?'}={value}")
    for node in root.iter(SML + "headerFooter"):
        lines += [child.text for child in node if child.text]
    return "\n".join(lines)


def _chardata(root: ET.Element, shared: list[str], used: set[int]) -> str:
    return "\n".join(text.strip() for text in root.itertext() if text.strip())


_HEADER_FOOTER = frozenset(SML + tag for tag in ("oddHeader", "oddFooter", "evenHeader",
                                                  "evenFooter", "firstHeader", "firstFooter"))
# (part name pattern, method, reader, element kinds whose text the reader consumes;
# None: every character datum). In a read part, every other element that carries
# text is its own unread part (``element_not_read``): content a reader does not
# consume is accounted, never silently dropped.
_PACKAGE_READERS = tuple((re.compile(pattern), method, reader, consumed)
                         for pattern, method, reader, consumed in (
    (r"\[Content_Types\]\.xml|(?:.*/)?_rels/[^/]*\.rels", "opc-relationships", _opc,
     frozenset()),
    (r"word/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml",
     "wordml-text", _wordml, frozenset(_WORD_TEXT)),
    (r"ppt/(?:slides|slideLayouts|slideMasters|notesSlides|notesMasters|handoutMasters)/"
     r"[^/]+\.xml|xl/drawings/[^/]+\.xml", "drawingml-text", _drawingml, frozenset({A + "t"})),
    (r"xl/workbook\.xml", "sml-workbook", _workbook, frozenset({SML + "definedName"})),
    (r"xl/worksheets/[^/]+\.xml", "sml-cells", _cells,
     frozenset({SML + "v", SML + "f", SML + "t"}) | _HEADER_FOOTER),
    (r"docProps/(?:core|app)\.xml|xl/comments\d*\.xml|(?:word|xl|ppt)/(?:styles|settings|"
     r"fontTable|webSettings|numbering|presProps|viewProps|tableStyles|calcChain|presentation)"
     r"\.xml|(?:word|xl|ppt)/theme/[^/]+\.xml", "xml-chardata", _chardata, None)))


def _unconsumed(root: ET.Element, consumed: frozenset[str]) -> list[str]:
    """Element kinds (local names) in ``root`` that carry text the reader did not consume."""
    found: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.tag == _MC_FALLBACK:  # the reader skips it: it repeats its choice
            continue
        local = node.tag.rsplit("}", 1)[-1]
        if node.tag not in consumed and local not in _LAYOUT and (node.text or "").strip():
            found.add(local)
        stack.extend(node)
    return sorted(found)


def _package(ledger: _Ledger, members: list[tuple[str, str, bytes]], depth: int) -> None:
    """Every part of an OOXML package: read by the reader table, else unread. Each
    member is written with room held for the ones after it (the zip checked room for
    all of them); a read part whose unconsumed elements do not fit with it is skipped
    whole."""
    shared: list[str] = []
    used: set[int] = set()
    shared_part = next((m for m in members if m[0] == _SHARED_STRINGS), None)
    shared_root = _xml(shared_part[2]) if shared_part else None
    if shared_root is not None:
        shared = ["".join(t.text or "" for t in si.iter(SML + "t"))
                  for si in shared_root.iter(SML + "si")]
    # A second copy of the shared-strings name is a part like any other.
    order = [m for m in members if m is not shared_part] + ([shared_part] if shared_part else [])
    for number, member in enumerate(order, 1):
        with ledger.reserve(len(order) - number):
            if member is shared_part:
                _shared_strings(ledger, shared_part, shared_root, shared, used)
            else:
                _package_member(ledger, member, shared, used, depth)


def _read_elements(ledger: _Ledger, path: str, body: bytes, text: str, method: str,
                   unconsumed: list[str]) -> None:
    """A read package part and an unread part per element kind its reader left; all
    skipped as one when they do not fit."""
    if not ledger.room(1 + len(unconsumed)):
        ledger.skipped(path, "ooxml_part", body, None, "parts", MAX_PARTS)
        return
    ledger.read(path, "ooxml_part", body, text, method)
    for local in unconsumed:
        ledger.unread(f"{path}#{local}", "ooxml_element", None, "element_not_read")


def _package_member(ledger: _Ledger, member: tuple[str, str, bytes], shared: list[str],
                    used: set[int], depth: int) -> None:
    name, path, body = member
    match = next(((method, reader, consumed)
                  for pattern, method, reader, consumed in _PACKAGE_READERS
                  if pattern.fullmatch(name)), None)
    if match is None:
        if name.lower().endswith((".xml", ".rels", ".vml")):
            ledger.unread(path, "ooxml_part", body, "ooxml_part_not_parsed")
        else:  # media, embedded packages and objects: by their own kind
            _part(ledger, body, path, PurePosixPath(name).name, depth + 1)
        return
    root = _xml(body)
    try:
        text = match[1](root, shared, used) if root is not None else None
    except (ValueError, IndexError, RecursionError):
        text = None
    if text is None:
        ledger.unread(path, "ooxml_part", body, "xml_unparseable")
        return
    _read_elements(ledger, path, body, text, match[0],
                   _unconsumed(root, match[2]) if match[2] is not None else [])


def _shared_strings(ledger: _Ledger, part: tuple[str, str, bytes], root: ET.Element | None,
                    shared: list[str], used: set[int]) -> None:
    """The shared strings no cell used (the rest are read in their cells)."""
    if root is None:
        ledger.unread(part[1], "ooxml_part", part[2], "xml_unparseable")
        return
    _read_elements(ledger, part[1], part[2], "\n".join(
        s for i, s in enumerate(shared) if i not in used and s), "sml-shared-strings",
        _unconsumed(root, frozenset({SML + "t"})))


# ------------------------------------------------------------------ mail (RFC 822)

def _mail(ledger: _Ledger, data: bytes, path: str, depth: int) -> None:
    """A MIME message: its tree is enumerated first, then each node is read by role.

    The enumeration is structural and closed-world: an entity whose payload is a
    list is a container and each list item is a child. Under any non-multipart
    entity (every ``message/*`` subtype, known or not) each child is a carried
    message: its header block is its own part, then its payload is walked in turn.
    Every other entity is a body part, read by kind or ``unread``. Reading never
    decides structure, so a reader can only fail to ``unread``."""
    try:
        message = BytesParser(policy=_MAIL_POLICY).parsebytes(data)
    except Exception:  # noqa: BLE001 - any parser failure: the container is unread
        ledger.unread(path, "container", data, "container_unreadable")
        return
    nodes: list[tuple[str, str, Any]] = []
    members = ledger.files is not None
    _mime_tree(message, f"{path}!", f"{path}#headers", True, depth + 1, nodes,
               members=members)
    if not ledger.room(len(nodes)):
        ledger.skipped(path, "container", data, None, "parts", MAX_PARTS)
        return
    own = None
    if members:
        # Members mode (module docstring): the body entities of the message are its own
        # text, not members; a carried message is a member of its own bytes.
        from integrations.document_understanding import mail_body
        try:
            own = {id(body) for _, role, entity in nodes if role == "headers"
                   for body in mail_body(entity)}
        except Exception:  # noqa: BLE001 - as a parser failure (e.g. recursion depth)
            ledger.unread(path, "container", data, "container_unreadable")
            return
    linesep = "\r\n" if b"\r\n" in data else "\n"
    for index, (node_path, role, entity) in enumerate(nodes, 1):
        with ledger.reserve(len(nodes) - index):  # the room checked, held for later nodes
            if role == "message" or (members and role == "body" and _encoded_carrier(entity)):
                _carried(ledger, node_path, entity, depth + 1, linesep)
            else:
                _mime_node(ledger, node_path, role, entity, depth + 1, own)


_ENCODINGS = ("base64", "quoted-printable")


def _encoded_carrier(entity: Any) -> bool:
    """A carried message (``message/rfc822``, ``message/global``) whose body the entity
    states is transfer-encoded, as several mailers write a forward."""
    return (entity.get_content_type() in ("message/rfc822", "message/global") and
            str(entity.get("Content-Transfer-Encoding", "")).strip().lower() in _ENCODINGS)


class _MailEntity(EmailMessage):
    """Python's parser parses a carried message's body as a message before (and
    without) removing its transfer encoding, so an encoded forward would be parsed as
    its own encoding (a quoted-printable soft line break in its headers makes the rest
    body text). Such an entity reports no ``message`` main type, so the parser keeps
    its body as the source's raw text, which :func:`_carried` decodes and reads."""

    def get_content_maintype(self) -> str:
        return "application" if _encoded_carrier(self) else super().get_content_maintype()


_MAIL_POLICY = policy.default.clone(message_factory=_MailEntity)


def _mime_tree(entity: Any, base: str, headers: str | None, message: bool, depth: int,
               nodes: list[tuple[str, str, Any]], carried: bool = False,
               members: bool = False) -> None:
    if headers is not None:
        nodes.append((headers, "carried" if carried else "headers", entity))
    payload = entity.get_payload()
    if not isinstance(payload, list):
        nodes.append((f"{base}/1" if message else base, "body", entity))
        return
    if members and entity.get_content_maintype() != "multipart":
        nodes.append((f"{base}/1" if message else base, "message", entity))   # own bytes
        return
    if depth > MAX_DEPTH:
        nodes.append((base, "depth", entity))
        return
    for edge in ("preamble", "epilogue"):
        if (getattr(entity, edge, None) or "").strip():
            nodes.append((f"{base}#{edge}", edge, entity))
    # By main type, never by subtype: a subtype nobody listed still has its headers read.
    carries_message = entity.get_content_maintype() != "multipart"
    for index, child in enumerate(payload, 1):
        child_path = f"{base}/{index}"
        _mime_tree(child, child_path, f"{child_path}#headers" if carries_message else None,
                   carries_message, depth + 1, nodes, True, members)


def _carried(ledger: _Ledger, path: str, entity: Any, depth: int, linesep: str) -> None:
    """Members mode: a ``message/*`` entity is a member of its body's bytes (module
    docstring), a mail when it carries a message, else text. An encoded carried
    message (:class:`_MailEntity`) is the source's raw body with its stated transfer
    encoding removed before any of it is parsed; one that does not decode keeps the
    ``carried_message_not_read`` gap. A nameless carried message is named
    ``<Subject>.eml``: the subject is shown, while the suffix (the type the entity
    states) is all a reader or the sniff can take from the name."""
    try:
        if _encoded_carrier(entity):
            body = _decoded(entity.get_payload().encode("ascii", "surrogateescape"),
                            entity.get("Content-Transfer-Encoding"))
        else:
            buffer = BytesIO()
            BytesGenerator(buffer, mangle_from_=False, policy=entity.policy.clone(
                linesep=linesep, refold_source="none")).flatten(entity)
            body = buffer.getvalue().split((linesep * 2).encode(), 1)[1]
    except Exception:  # noqa: BLE001 - a message that cannot be written out has no bytes
        ledger.unread(path, "mail_headers", None, "carried_message_not_read")
        return
    carries = entity.get_content_subtype() in ("rfc822", "global")
    subject = _subject_name(body) if carries else ""
    name = entity.get_filename() or (f"{subject}.eml" if subject else "")
    _part(ledger, body, path, name, depth,
          hint="message/rfc822" if carries else "text/plain")


def _decoded(body: bytes, encoding: Any) -> bytes:
    """``body`` with its stated transfer encoding removed; base64 that does not decode
    raises (the caller's gap)."""
    stated = str(encoding or "").strip().lower()
    if stated == "base64":
        packed = re.sub(rb"\s", b"", body)
        decoded = b64decode(packed + b"=" * (-len(packed) % 4))
        if body.strip() and not decoded:  # nothing of the body was base64: no bytes
            raise ValueError("stated base64 body does not decode")
        return decoded
    if stated == "quoted-printable":
        return quopri.decodestring(body)
    return body


MAX_NAME_CHARS = 200  # a display name taken from a header


def _subject_name(body: bytes) -> str:
    """A carried message's ``Subject`` on one line, bounded, with path separators as
    spaces (it is shown, never resolved), else ""."""
    try:
        subject = BytesHeaderParser(policy=policy.default).parsebytes(body).get("Subject")
        return re.sub(r"[\s/\\]+", " ", str(subject or "")).strip()[:MAX_NAME_CHARS]
    except Exception:  # noqa: BLE001 - an unreadable header block names nothing
        return ""


def _mime_node(ledger: _Ledger, path: str, role: str, entity: Any, depth: int,
               own: set[int] | None = None) -> None:
    """One MIME node; ``own``: in members mode, the body entities that are not members."""
    if role == "depth":
        ledger.skipped(path, "container", None, None, "depth", MAX_DEPTH)
    elif role in ("headers", "carried"):
        lines = []
        for key in entity.keys():
            try:
                lines.extend(f"{key}: {value}" for value in entity.get_all(key) or [])
            except Exception:  # noqa: BLE001 - a malformed header value is kept raw
                lines.append(f"{key}: (unparsed)")
        ledger.read(path, "mail_headers", None, "\n".join(lines), "rfc822-headers")
    elif role in ("preamble", "epilogue"):
        ledger.read(path, "mime_text", None, getattr(entity, role).strip(), "mime-text")
    else:
        _mime_body(ledger, path, entity, depth, own)


def _mime_body(ledger: _Ledger, path: str, entity: Any, depth: int,
               own: set[int] | None = None) -> None:
    content_type = entity.get_content_type()
    payload = entity.get_payload(decode=True) or b""
    if own is not None:
        # Members mode: the message's body (read with it, a gap when it does not decode)
        # or an empty inline leaf, else a member.
        from integrations.document_understanding import body_text
        if id(entity) in own and body_text(entity) is None:
            ledger.unread(path, "mail_body", payload, "text_encoding_unsupported")
        elif id(entity) in own or not (payload or entity.is_attachment()):
            ledger.read(path, "mail_body", payload, "", "mime-text")
        else:
            _part(ledger, payload, path, entity.get_filename() or "", depth, hint=content_type)
        return
    if content_type in ("text/plain", "text/html") and not entity.is_attachment():
        try:
            text = entity.get_content()
        except (LookupError, UnicodeError, ValueError):
            ledger.unread(path, "mail_body", payload, "text_encoding_unsupported")
            return
        method = "html-text" if content_type == "text/html" else "mime-text"
        ledger.read(path, "mail_body", payload,
                    _html(text) if content_type == "text/html" else text, method)
        return
    _part(ledger, payload, path, entity.get_filename() or "", depth, hint=content_type)


# ------------------------------------------------------------------ Outlook .msg (MS-CFB)

_MSG_STORAGES = (msg_reader.RECIPIENT_PREFIX, msg_reader.ATTACHMENT_PREFIX)
_CFB_ERRORS = (ValueError, struct.error, IndexError)


def _msg(ledger: _Ledger, data: bytes, path: str, depth: int) -> None:
    try:
        cfb = msg_reader.Cfb(data)
        names = {cfb.entries[i][0] for i in cfb.children(0)}
    except _CFB_ERRORS:
        ledger.unread(path, "cfb", data, "container_unreadable")
        return
    if msg_reader.PROPERTIES not in names:  # a legacy Office file, not a message
        ledger.format = "other"
        ledger.unread(path, "cfb", data, "unknown_kind")
        return
    named: dict[int, str] = {}
    nameid = cfb.child(0, msg_reader.NAMEID)
    if nameid is not None:  # labels only: its streams are accounted with the rest
        try:
            named = msg_reader.named_properties(*(
                cfb.stream(cfb.child(nameid, f"__substg1.0_000{n}0102"), MAX_PART_BYTES)
                if cfb.child(nameid, f"__substg1.0_000{n}0102") is not None else b""
                for n in (2, 3, 4)))
        except (*_CFB_ERRORS, OverflowError):
            named = {}
    _msg_storage(ledger, cfb, 0, f"{path}!", depth + 1, msg_reader.HEADER_TOP, named)


def _msg_storage(ledger: _Ledger, cfb: msg_reader.Cfb, index: int, path: str, depth: int,
                 header: int, named: dict[int, str]) -> None:
    children = cfb.children(index)
    if depth > MAX_DEPTH or not ledger.room(len(children)):
        ledger.skipped(path, "msg_storage", None, None,
                       "depth" if depth > MAX_DEPTH else "parts",
                       MAX_DEPTH if depth > MAX_DEPTH else MAX_PARTS)
        return
    by_name = {cfb.entries[i][0]: i for i in children}
    for number, child in enumerate(children, 1):
        with ledger.reserve(len(children) - number):  # the room checked, held for later ones
            _msg_child(ledger, cfb, by_name, child, index, path, depth, header, named)


def _msg_child(ledger: _Ledger, cfb: msg_reader.Cfb, by_name: dict[str, int], child: int,
               index: int, path: str, depth: int, header: int, named: dict[int, str]) -> None:
    """One child of the storage ``index``: a storage walked in turn, else a stream."""
    name, kind = cfb.entries[child][0], cfb.entries[child][1]
    child_path = f"{path}/{name}"
    if kind == msg_reader.STORAGE:
        if name.startswith(_MSG_STORAGES):
            _msg_storage(ledger, cfb, child, child_path, depth + 1, msg_reader.HEADER_SUB,
                         named)
        elif name == msg_reader.ATTACHED_MESSAGE:
            if ledger.files is not None:  # members mode: carried (module docstring)
                try:
                    saved = msg_reader.embedded_message(cfb, child, MAX_PART_BYTES)
                except (*_CFB_ERRORS, OverflowError, RecursionError):
                    ledger.unread(child_path, "msg_storage", None, "carried_message_not_read")
                else:
                    _part(ledger, saved, child_path, _attached_name(cfb, by_name, True),
                          depth + 1, hint=_MSG_TYPE)
                    return
            _msg_storage(ledger, cfb, child, child_path, depth + 1,
                         msg_reader.HEADER_EMBEDDED, named)
        elif name == msg_reader.NAMEID and index == 0:
            _msg_storage(ledger, cfb, child, child_path, depth + 1, 0, named)
        else:
            ledger.unread(child_path, "msg_storage", None, "msg_storage_not_parsed")
        return
    try:
        data = cfb.stream(child, MAX_PART_BYTES)
    except OverflowError:
        ledger.skipped(child_path, "msg_property", None, cfb.entries[child][6],
                       "part_bytes", MAX_PART_BYTES)
        return
    except _CFB_ERRORS:
        ledger.unread(child_path, "msg_property", None, "unreadable")
        return
    _msg_stream(ledger, cfb, by_name, name, data, child_path, depth, header, named)


def _msg_stream(ledger: _Ledger, cfb: msg_reader.Cfb, by_name: dict[str, int], name: str,
                data: bytes, path: str, depth: int, header: int, named: dict[int, str]) -> None:
    """One ``.msg`` stream: decoded by its property tag, else unread (closed world) with
    the tag it was left unread by (``tag``: schema only, never a value)."""
    def undecoded(prop_type: str, prop_id: str) -> None:
        ledger.unread(path, "msg_property", data, "msg_property_not_decoded",
                      tag=_tag(prop_type, prop_id, named))

    if name == msg_reader.PROPERTIES:
        try:
            text = msg_reader.fixed_properties(data, header, named)
        except msg_reader.PropertyNotDecoded as exc:  # every such entry, by its tag
            tags = [_tag(f"{tag & 0xFFFF:04X}", f"{tag >> 16:04X}", named) for tag in exc.tags]
            if not ledger.room(2):  # no room to split: the whole table unread
                ledger.unread(path, "msg_property", data, "msg_property_not_decoded", tag=tags)
                return
            # The entries that decoded are read; the rest are one part of their own.
            ledger.read(path, "msg_property", data, exc.text, "msg-property-table")
            ledger.unread(f"{path}#undecoded", "msg_property", exc.rows,
                          "msg_property_not_decoded", tag=tags)
        except (ValueError, struct.error):
            undecoded("table", "size")
        else:
            ledger.read(path, "msg_property", data, text, "msg-property-table")
        return
    prop = msg_reader.PROPERTY_STREAM.fullmatch(name)
    if prop is None:
        undecoded("stream", "unknown")
        return
    prop_id, prop_type, element = prop[1], prop[2], prop[3]
    if not header and prop_type == "0102":  # the named-property map (no property table)
        _nameid_stream(ledger, data, path, prop_id, named)
    elif prop_type in ("001F", "001E", "101F", "101E") and (element or prop_type[0] == "0"):
        text = msg_reader.string_value(data, prop_type)
        if prop_id != "1000":  # the plain body is its own text
            text = f"{msg_reader.label(prop_id, named)}: {text}"
        ledger.read(path, "msg_property", data, text, "msg-property")
    elif prop_type in ("101F", "101E", "1102") and element is None:
        # The lengths of a multi-valued property's values (each value is its own stream).
        if len(data) % (8 if prop_type == "1102" else 4):
            undecoded(prop_type, prop_id)
        else:
            ledger.read(path, "msg_property", data, "", "msg-property")
    elif prop_type == "0048" or (prop_type[0] == "1" and prop_type not in ("101F", "101E",
                                                                         "1102")):
        try:
            values = msg_reader.fixed_values(data, int(prop_type, 16))
        except (ValueError, struct.error):
            undecoded(prop_type, prop_id)
        else:
            ledger.read(path, "msg_property", data,
                        f"{msg_reader.label(prop_id, named)}: {'; '.join(values)}",
                        "msg-property")
    elif prop_type not in ("0102", "1102"):
        undecoded(prop_type, prop_id)
    elif prop_id == "1013":  # HTML body
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError:
            source = data.decode("cp1252", "replace")
        ledger.read(path, "msg_property", data, _html(source), "html-text")
    elif prop_id == "1009":  # compressed RTF body
        try:
            text, is_html = msg_reader.rtf_text(msg_reader.rtf_decompress(data, MAX_PART_BYTES))
        except (ValueError, struct.error, RecursionError):
            ledger.unread(path, "msg_property", data, "unreadable")
        else:
            ledger.read(path, "msg_property", data, _html(text) if is_html else text.strip(),
                        "msg-rtf")
    elif prop_id == "3701":  # attachment bytes
        _part(ledger, data, path, _attached_name(cfb, by_name), depth + 1)
    elif prop_id in msg_reader.OPAQUE_BINARY or prop_id in msg_reader.ADDRESS_BINARY:
        name = msg_reader.OPAQUE_BINARY.get(prop_id) or msg_reader.ADDRESS_BINARY[prop_id]
        try:
            text = msg_reader.binary_text(prop_id, data)
        except (ValueError, struct.error):
            undecoded(prop_type, prop_id)
            return
        if text is None:
            ledger.read(path, "msg_property", data, "", "msg-identifier", property=name)
        elif not text:  # an address property that names no address
            undecoded(prop_type, prop_id)
        else:
            ledger.read(path, "msg_property", data,
                        f"{msg_reader.LABELS.get(prop_id) or name}: {text}", "msg-address",
                        property=name)
    elif prop_type == "0102" and _metadata(tag := _tag(prop_type, prop_id, named)):
        ledger.unread(path, "msg_property", data, "non_content_metadata", tag=tag)
    else:
        undecoded(prop_type, prop_id)


# Microsoft's named-property sets (MS-OXPROPS 1.3.2): a binary in one is protocol
# metadata (recurrence, time zone, meeting and sharing ids), never message content.
MAPI_METADATA_SETS = frozenset({
    "PS_MAPI", "00062002-0000-0000-c000-000000000046",  # PSETID_Appointment
    "00062003-0000-0000-c000-000000000046", "00062004-0000-0000-c000-000000000046",
    "00062008-0000-0000-c000-000000000046", "0006200a-0000-0000-c000-000000000046",
    "0006200e-0000-0000-c000-000000000046", "00062040-0000-0000-c000-000000000046",
    "00062041-0000-0000-c000-000000000046",  # Task, Address, Common, Log, Note, Sharing, PostRss
    "6ed8da90-450b-101b-98da-00aa003f1305",  # PSETID_Meeting
    "71035549-0739-4dcb-9163-00f0580dbbdf",  # PSETID_AirSync
    "41f28f13-83f4-4114-a584-eedb5a6b0bff",  # PSETID_Messaging
    "11000e07-b51b-40d6-af21-caa85edab1d0"})  # PSETID_CalendarAssistant


def _metadata(tag: dict[str, str]) -> bool:
    """A binary property's ``tag`` names protocol metadata: a MAPI-defined id (below the
    message-class-defined range from ``0x6800``) or a named property in a Microsoft
    set. A string-named one, or one of any other set, may be content."""
    prop_id = tag["id"]
    if re.fullmatch(r"[0-9A-F]{4}", prop_id):
        return int(prop_id, 16) < 0x6800
    return prop_id.split(":", 1)[0] in MAPI_METADATA_SETS


# The ``tag`` of an undecoded ``.msg`` part: a property type and id (4 hex digits), a
# table that is not a whole number of entries (``table``/``size``), a stream that is no
# property (``stream``/``unknown``), a named-property map stream (``nameid``), or a named
# property by its numeric name (property set GUID or name, ``:0x`` and LID; ``named``
# when it has a string name or none). Nothing else is a tag (counts-only output).
_GUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
TAG_TYPE = re.compile(r"[0-9A-F]{4}|table|stream|nameid")
TAG_ID = re.compile(rf"[0-9A-F]{{4}}|size|unknown|named|"
                    rf"(?:PS_MAPI|PS_PUBLIC_STRINGS|{_GUID}):0x[0-9A-F]{{4,8}}")
_NAMED_ID = re.compile(r"[89A-F][0-9A-F]{3}")
_NUMERIC_NAME = re.compile(rf"(PS_MAPI|PS_PUBLIC_STRINGS|\{{({_GUID})\}}):(0x[0-9A-F]{{4,8}})")


def _tag(prop_type: str, prop_id: str, named: dict[int, str]) -> dict[str, str]:
    if _NAMED_ID.fullmatch(prop_id):  # file-local: the tag is the property's own name
        numeric = _NUMERIC_NAME.fullmatch(named.get(int(prop_id, 16)) or "")
        prop_id = f"{numeric[2] or numeric[1]}:{numeric[3]}" if numeric else "named"
    return {"type": prop_type, "id": prop_id}


_MSG_TYPE = "application/vnd.ms-outlook"


def _attached_name(cfb: msg_reader.Cfb, by_name: dict[str, int], item: bool = False) -> str:
    """An attachment's file name (long, else short; an attached item's display name,
    its subject, when ``item``), else ""."""
    labels = ("3707", "3704", "3001") if item else ("3707", "3704")
    label = next((f"__substg1.0_{n}001F" for n in labels if f"__substg1.0_{n}001F" in by_name),
                 None)
    try:
        attached = (cfb.stream(by_name[label], MAX_PART_BYTES).decode("utf-16-le", "replace")
                    if label else "")
    except (*_CFB_ERRORS, OverflowError):
        attached = ""  # the name only types the bytes; they are accounted either way
    return attached.rstrip("\x00")


def _nameid_stream(ledger: _Ledger, data: bytes, path: str, prop_id: str,
                   named: dict[int, str]) -> None:
    """A stream of ``__nameid_version1.0``: GUIDs, entries, names, or a hash bucket."""
    if prop_id == "0002" and not len(data) % 16:
        text = "\n".join("{" + str(UUID(bytes_le=data[i:i + 16])) + "}"
                         for i in range(0, len(data), 16))
    elif prop_id == "0003" and named:  # the map itself (decoded before the walk)
        text = "\n".join(f"0x{tag:04X} = {name}" for tag, name in sorted(named.items()))
    elif prop_id == "0004" and named:  # the names, shown in the map
        text = ""
    elif prop_id[0] == "1" and not len(data) % 8:  # a lookup bucket of the entries
        text = ""
    else:
        ledger.unread(path, "msg_property", data, "msg_property_not_decoded",
                      tag={"type": "nameid", "id": prop_id})
        return
    ledger.read(path, "msg_property", data, text, "msg-named-properties")


# ------------------------------------------------------------------ PDF

def _parser_failure(exc: BaseException, reason: str) -> str:
    """The unread reason of a failed ``pypdf`` call (``reason`` for an ordinary
    error); an interrupt, exit or any other ``BaseException`` is re-raised."""
    from integrations.document_understanding import native_panic

    if native_panic(exc):
        return "parser_panicked"
    if isinstance(exc, Exception):
        return reason
    raise exc

# Catalog entries that hold document structure only (no text of their own).
_PDF_STRUCTURE = frozenset({"/Type", "/Pages", "/Version", "/PageLayout", "/PageMode",
                            "/ViewerPreferences", "/Lang", "/MarkInfo", "/PageLabels"})
_PDF_MARKUP = frozenset({"/Text", "/FreeText", "/Highlight", "/Underline", "/StrikeOut",
                         "/Squiggly", "/Square", "/Circle", "/Line", "/Polygon", "/PolyLine",
                         "/Stamp", "/Caret", "/Ink"})
# Why the one reader read nothing, as an unread reason.
_READING_FAILURES = {"encrypted": "encrypted",
                     "pdf_parser_unavailable": "pdf_parser_unavailable",
                     "parser_panicked": "parser_panicked",
                     "pdf_parse_error": "container_unreadable"}


def _pdf(ledger: _Ledger, data: bytes, path: str, depth: int) -> None:
    """Page text from the one reader (``integrations.document_understanding.read_pages``:
    pypdf when it reads every page, else MarkItDown when installed, else pypdf's partial
    reading or poppler's text layer); the rest of the
    document through the reader pypdf opened, else one unread part.

    The page bound is checked before any page is read: only the first pages are read,
    at most ``MAX_PAGES`` and no more than the parts room left (each page is a part);
    the rest are skipped page ranges (:func:`_page_plan`). The room is planned for
    the parts that will exist (document information when there is some, catalog
    entries that are not structure) and held for the planned pages and ranges while
    the catalog and each page's resources are read, so no part goes past
    ``MAX_PARTS``; a page whose resources are listed is planned two parts (its text and
    at least one part for its resources), so a tight plan keeps its text. With room for
    one part only a one-page PDF can be read: a longer one is skipped before a page is
    read. When MarkItDown's page breaks do not match, its text is kept unpaged: no
    second reader reads the whole PDF again."""
    from integrations import document_understanding

    room = ledger.left()
    bound = min(room, document_understanding.MAX_PAGES)
    if bound < 1:
        ledger.skipped(path, "pdf", data, None, "parts", MAX_PARTS)
        return
    caps = {"PDF page count exceeds bound": ("pages", document_understanding.MAX_PAGES)
            if room > 1 else ("parts", MAX_PARTS),
            "extracted PDF text exceeds bound": ("text_chars",
                                                 document_understanding.MAX_TEXT)}
    try:
        # One part of room holds no page and the skipped range after it: read the first
        # pages only with room for both.
        reading = document_understanding.read_pages(
            data, "pdf", max_pages=bound, max_text=None, page_fallback=False,
            first_pages=room > 1)
    except ValueError as exc:  # ValidationError: poppler's text bound, or no reader
        if str(exc) not in caps:  # reads the first pages of a PDF over the page bound
            raise
        ledger.skipped(path, "pdf", data, None, *caps[str(exc)])
        return
    if reading.failure is not None:
        ledger.unread(path, "pdf", data, _READING_FAILURES[reading.failure])
        return
    reader = reading.document
    if reader is not None:
        try:
            catalog = reader.trailer["/Root"].get_object()
            pages = list(reader.pages)
            info = {key: value for key, value in (reader.metadata or {}).items()
                    if isinstance(value, str)}
            keys = sorted(catalog.keys())
        except BaseException as exc:  # noqa: BLE001 - pypdf raises many classes on a bad file
            reason = _parser_failure(exc, "container_unreadable")
            if reason != "parser_panicked":
                ledger.unread(path, "pdf", data, reason)
                return
            reader = None  # its pages were read; the rest is one unread part
    if reader is None:
        _pdf_text_only(ledger, data, path, reading)
        return
    entries = [key for key in keys if key not in _PDF_STRUCTURE]
    # Paged text: a part per page, two for one whose resources are listed. Unpaged
    # text: one part, and every page still walked for its resources, a slot each for
    # the part that says they were not read.
    # Without room for one page and its resources, each page is planned one part (a
    # page whose resources do not fit is one skipped part).
    listed = [_page_resources(page) for page in pages[:len(reading.pages)]]
    fixed = bool(info) + len(entries) + (0 if reading.pages else 1 + len(pages))
    needs = [1 + bool(found[1] or found[2] or found[3]) for found in listed]
    plan = _page_plan(ledger, reading, fixed, needs)
    if plan is None:
        needs = [1] * len(listed)
        plan = _page_plan(ledger, reading, fixed, needs)
    if plan is None:
        ledger.skipped(path, "pdf", data, None, "parts", MAX_PARTS)
        return
    kept, ranges = plan
    walked = pages[:kept] if reading.pages else pages
    needs = needs[:kept] if reading.pages else [1] * len(walked)  # held per walked page
    if info:
        ledger.read(f"{path}#info", "pdf_info", None, "\n".join(
            f"{key.lstrip('/')}: {value}" for key, value in info.items()), "pdf-info")
    with ledger.reserve(sum(needs) + (not reading.pages) + len(ranges)):
        for index, key in enumerate(entries, 1):
            with ledger.reserve(len(entries) - index):  # the later catalog entries
                _pdf_catalog(ledger, reader, catalog, key, path, depth)
    if not reading.pages:  # MarkItDown's text, which no reader could split into pages
        ledger.read(f"{path}!/text", "pdf", None, reading.text, reading.method,
                    **_provenance(reading, reading.method))
    texts = {page.number: page for page in reading.pages[:kept]}
    for number, page in enumerate(walked, 1):
        with ledger.reserve(sum(needs[number:]) + len(ranges)):  # the later pages, ranges
            _pdf_page(ledger, listed[number - 1] if reading.pages else _page_resources(page),
                      texts.get(number), reading, f"{path}!/page/{number}", depth)
    _skip_pages(ledger, reading, path, ranges)


def _page_plan(ledger: _Ledger, reading: Any, fixed: int, needs: list[int] | None = None
               ) -> tuple[int, list[tuple[int, int, str, int]]] | None:
    """(pages kept, skipped page ranges) of ``reading``'s paged text, read pages first,
    so that they and ``fixed`` other parts of the PDF fit in the parts room left;
    None when not one page (or the unpaged text) fits. ``needs``: the parts of each
    read page (default one each). A range is (first, last, cap, limit): the pages past
    ``MAX_PAGES`` cap ``pages``, the rest of the pages within it that do not fit cap
    ``parts``. A read page that does not fit (the PDF's other parts are only known
    once pypdf opened it) is skipped with the unread ones."""
    from integrations import document_understanding

    bound = document_understanding.MAX_PAGES
    read = len(reading.pages)
    total = reading.skipped_pages[1] if reading.skipped_pages else read
    ranges = [(bound + 1, total, "pages", bound)] if total > bound else []
    room = ledger.left() - fixed - len(ranges)
    if not read:
        return (0, ranges) if room >= 0 and not ranges else None
    needs = needs or [1] * read

    def fitting(room: int) -> int:  # the first pages whose parts fit in ``room``
        kept = 0
        while kept < read and needs[kept] <= room:
            room -= needs[kept]
            kept += 1
        return kept

    kept = fitting(room)
    if kept < min(total, bound):  # pages within the page bound left: one more range
        kept = fitting(room - 1)
        ranges.insert(0, (kept + 1, min(total, bound), "parts", MAX_PARTS))
    return (kept, ranges) if kept >= 1 else None


def _skip_pages(ledger: _Ledger, reading: Any, path: str,
                ranges: list[tuple[int, int, str, int]]) -> None:
    for first, last, cap, limit in ranges:
        ledger.skipped(f"{path}!/page/{first}" + (f"-{last}" if last > first else ""),
                       "pdf_page", None, None, cap, limit, first_page=first, last_page=last,
                       source_sha256=reading.source_sha256)


def _provenance(reading: Any, method: str) -> dict[str, Any]:
    """What a part read by the one reader records: the PDF's hash; for MarkItDown text
    the converter record's identity."""
    fields: dict[str, Any] = {"source_sha256": reading.source_sha256}
    if method == "markitdown" and reading.conversion is not None:
        fields["conversion"] = {key: reading.conversion[key] for key in (
            "converter", "converter_version", "options_sha256", "output_sha256")}
    return fields


def _pdf_text_only(ledger: _Ledger, data: bytes, path: str, reading: Any) -> None:
    """pypdf did not open the PDF (missing, or it panicked): the reader's page text and
    the rest of the document one unread part."""
    plan = _page_plan(ledger, reading, 1 + (not reading.pages))
    if plan is None:
        ledger.skipped(path, "pdf", data, None, "parts", MAX_PARTS)
        return
    kept, ranges = plan
    if not reading.pages:
        ledger.read(f"{path}!/text", "pdf", None, reading.text, reading.method,
                    **_provenance(reading, reading.method))
    for page in reading.pages[:kept]:
        provenance = _provenance(reading, page.method)
        if page.text:
            ledger.read(f"{path}!/page/{page.number}", "pdf_page", None, page.text,
                        page.method, **provenance)
        else:  # no text layer, and this path neither OCRs nor lists the page's images
            ledger.unread(f"{path}!/page/{page.number}", "pdf_page", None,
                          page.unread or "image_only", **provenance)
    _skip_pages(ledger, reading, path, ranges)
    ledger.unread(f"{path}#structure", "pdf", data, "pdf_text_layer_only")


def _pdf_catalog(ledger: _Ledger, reader: Any, catalog: Any, key: str, path: str,
                 depth: int) -> None:
    entry = f"{path}#{key.lstrip('/')}"
    try:
        if key == "/Names":
            # Each embedded file and other name tree (named destinations are page
            # targets only), written only with room for all of them.
            items: list[tuple[str, str, bytes | None]] = []
            for tree in sorted(catalog[key].get_object().keys()):
                if tree == "/EmbeddedFiles":
                    items += [(f"{path}!/embedded/{name}"
                               + (f"~{index}" if len(contents) > 1 else ""), name, content)
                              for name, contents in sorted(reader.attachments.items())
                              for index, content in enumerate(contents, 1)]
                elif tree != "/Dests":
                    items.append((f"{entry}/{tree.lstrip('/')}", tree, None))
            if not ledger.room(len(items)):
                ledger.skipped(entry, "pdf_catalog_entry", None, None, "parts", MAX_PARTS)
                return
            for index, (child, name, content) in enumerate(items, 1):
                with ledger.reserve(len(items) - index):  # the ones after it
                    if content is None:
                        ledger.unread(child, "pdf_catalog_entry", None, "pdf_entry_not_parsed",
                                      entry=_pdf_entry("names", name))
                    else:
                        _part(ledger, content, child, name, depth + 1)
        elif key == "/Metadata":
            body = catalog[key].get_object().get_data()
            root = _xml(body)
            if root is None:
                ledger.unread(entry, "pdf_catalog_entry", body, "xml_unparseable")
            else:
                ledger.read(entry, "pdf_catalog_entry", body, _chardata(root, [], set()),
                            "xml-chardata")
        elif key == "/Outlines":
            ledger.read(entry, "pdf_catalog_entry", None, "\n".join(_outline(reader.outline)),
                        "pdf-outline")
        else:
            ledger.unread(entry, "pdf_catalog_entry", None, "pdf_entry_not_parsed",
                          entry=_pdf_entry("catalog", key))
    except BaseException as exc:  # noqa: BLE001 - a malformed entry is unread, never dropped
        ledger.unread(entry, "pdf_catalog_entry", None, _parser_failure(exc, "unreadable"))


def _outline(items: Iterable[Any]) -> list[str]:
    titles = []
    for item in items:
        if isinstance(item, list):
            titles += _outline(item)
        elif getattr(item, "title", None):
            titles.append(str(item.title))
    return titles


def _page_resources(page: Any) -> tuple[Any, list[str], list[Any], str | None]:
    """(images, image names, annotations, failure) of one pypdf page: its resources
    listed, not read; ``failure`` is why they could not be listed."""
    try:
        images = page.images
        names = ["/".join(i) if isinstance(i, list) else str(i) for i in images.keys()]
        return images, names, [a.get_object() for a in (page.get("/Annots") or [])], None
    except BaseException as exc:  # noqa: BLE001
        return None, [], [], _parser_failure(exc, "unreadable")


def _pdf_page(ledger: _Ledger, resources: tuple[Any, list[str], list[Any], str | None],
              text: Any, reading: Any, path: str, depth: int) -> None:
    """One page (``resources``: :func:`_page_resources`): its text part (``text``; None
    when the reading's text is one unpaged part), then its images and annotations,
    each read with room held for the ones after it; resources that do not fit (or
    cannot be listed) are one part saying so. With no room for both that part and the
    page's text, the page is one skipped part."""
    images, names, annotations, failure = resources
    own = text is not None
    fits = failure is None and ledger.room(own + len(names) + len(annotations))
    if own and not fits and not ledger.room(2):
        ledger.skipped(path, "pdf_page", None, None, "parts", MAX_PARTS,
                       source_sha256=reading.source_sha256)
        return
    if own:
        provenance = _provenance(reading, text.method)
        if text.unread:
            ledger.unread(path, "pdf_page", None, text.unread, **provenance)
        else:
            ledger.read(path, "pdf_page", None, text.text, text.method, **provenance)
    if failure is not None:
        ledger.unread(f"{path}#resources", "pdf_page", None, failure)
        return
    if not fits:
        ledger.skipped(f"{path}#resources", "pdf_page", None, None, "parts", MAX_PARTS)
        return
    later = len(names) + len(annotations)
    for index, name in enumerate(names):
        later -= 1
        try:
            data = images[index].data  # encoded by pypdf (PNG/JPEG/...), needs Pillow
        except BaseException as exc:  # noqa: BLE001 - a filter pypdf cannot decode: unread
            ledger.unread(f"{path}/image/{name}", "image", None,
                          _parser_failure(exc, "unreadable"))
            continue
        with ledger.reserve(later):
            _part(ledger, data, f"{path}/image/{name}", name, depth + 1, hint="image/")
    for index, annotation in enumerate(annotations, 1):
        later -= 1
        with ledger.reserve(later):
            _pdf_annotation(ledger, annotation, f"{path}/annotation/{index}", depth)


def _pdf_annotation(ledger: _Ledger, annotation: Any, path: str, depth: int) -> None:
    try:
        subtype = annotation.get("/Subtype")
        if subtype == "/Popup":
            return  # shows its parent's /Contents; the parent is the accounted part
        if subtype == "/FileAttachment":
            spec = annotation["/FS"].get_object()
            files = spec["/EF"].get_object()
            stream = (files.get("/UF") or files["/F"]).get_object()
            _part(ledger, stream.get_data(), path, str(spec.get("/UF") or spec.get("/F") or ""),
                  depth + 1)
        elif subtype == "/Link":
            action = annotation.get("/A")
            uri = action.get_object().get("/URI") if action is not None else None
            ledger.read(path, "pdf_annotation", None, f"[Link: {uri}]" if uri else "",
                        "pdf-annotation")
        elif subtype == "/Widget":
            ledger.unread(path, "pdf_form_field", None, "pdf_entry_not_parsed",
                          entry=_pdf_entry("annotation", subtype))
        elif subtype in _PDF_MARKUP:
            ledger.read(path, "pdf_annotation", None, str(annotation.get("/Contents") or ""),
                        "pdf-annotation")
        else:
            ledger.unread(path, "pdf_annotation", None, "pdf_entry_not_parsed",
                          entry=_pdf_entry("annotation", subtype))
    except BaseException as exc:  # noqa: BLE001
        ledger.unread(path, "pdf_annotation", None, _parser_failure(exc, "unreadable"))


def _pdf_entry(where: str, name: Any) -> str:
    """The ``entry`` of an unparsed PDF entry (``PDF_ENTRIES``): a name the standard
    defines at ``where``, else ``other`` (a file's own name is never stored as one)."""
    name = name if isinstance(name, str) else ""
    name = name if name.startswith("/") else "/" + name
    return f"{where}:{name if name in _PDF_UNPARSED[where] else 'other'}"


__all__ = ["ACCOUNTING_SCHEMA", "CAPS", "EXTRACTED", "FAMILIES", "FORMATS", "KINDS",
           "MAPI_METADATA_SETS", "METHOD_FAMILIES", "MAX_PART_BYTES", "PDF_ENTRIES", "REASONS",
           "SKIPPED", "STATES", "TAG_ID", "TAG_TYPE", "UNREAD", "VOCABULARY", "account",
           "capped", "is_container", "members", "part_format"]
