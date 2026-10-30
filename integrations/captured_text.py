"""The text of a captured file: one path from its pinned bytes to every reader (DOC-01).

A captured file (a SharePoint drive item version, a mail attachment) is read for text
here and nowhere else:

* At capture, :func:`capture_text` (``integrations.capture_sharepoint``,
  ``integrations.capture_mail``): the deterministic extractor
  (``integrations.document_understanding.extract_document``; no model) over the new
  bytes. A PDF page without a text layer (a scan) is OCR-read locally when this host
  has an engine (poppler + tesseract, or the macOS helper; never a network service):
  ``extracted_ocr``. Without one such a file is ``ocr_unavailable``, never silently
  textless; a later capture run on a host with OCR re-reads its stored bytes (no source
  read). The capture stores the status and the text, pinned by SHA-256 (a PDF's pages
  joined by ``ontology.mentions.PAGE_BREAK``). A PDF over the page bound is never
  refused outright: its pages 1-``MAX_PAGES`` are stored, and every reading of it
  names the rest skipped by the page cap (``CapturedText.skipped_by_cap``), which its
  readers carry: ``documentFields`` (gap ``pages``), the resolve rows
  (``row.skipped_by_cap``), a container's member (a gap); the mention documents, which
  have no gap channel, index its name only and record why
  (``CapturedText.text_withheld``).
  The stored text status stays ``extracted``/``extracted_ocr``. Bytes over the extractor's bound (a
  capture scope may declare a larger content cap) are never handed to it whole:
  ``exceeds_bound``, no text; a container among them is read member by member below.
  Text over the extractor's character bound is ``failed`` at capture; a sampler that
  matches identifiers only (``scripts.eval_oracle_dev``) asks for its first
  ``max_chars`` characters instead, read by the same readers without that bound and
  never stored.
* At projection and resolve, :func:`captured_text`: the mention documents
  (``ontology.project``, ``ontology.project_graph``), the typed fields
  (``ontology.document_fields``) and the cited text of ``MailCaptureStore.resolve`` and
  ``SharePointCaptureStore.resolve``. The pinned bytes (re-hashed) go through the same
  extractor without OCR, so the text is the same on every host; a file the capture
  OCR-read is read from its pinned OCR text instead (``text_sha256``): OCR output is not
  reproducible, so the captured reading is the one every reader cites. Bytes or OCR text
  missing or altered give None: nothing is guessed. One reading per (bytes, MIME type,
  name, OCR text) and process, shared by every reader of a build (a cache bounded by
  the text it holds, :class:`ReadingMemo`). A read that raised is not remembered: a
  transient failure (``child_process.Abandoned``, an ``OSError``, a timeout) is None to
  its caller and read again by the next, never unreadable for the life of the process.

OCR text is untrusted source text: only the deterministic identifier grammar turns it
into values, and readers label it unconfirmed (``ocr_pages``).

A container (a zip that is not an Office package, an ``.eml``, an Outlook ``.msg``) is
also read member by member: :func:`container_members` enumerates its member files
through the one enumeration of ``operating.use_case_parts.members`` (nested containers
recursively, a mail carried inside another included; ``.msg`` streams through
``operating.msg_reader``) and reads each member's bytes with :func:`read_text`, the
same extractor every captured file goes through (no OCR at projection: a scanned member
is ``no_text`` there; :func:`read_container` OCR-reads PDF members when asked).
A member is cited ``<container handle>&member=<path>&member_sha256=<hex>``
(:func:`member_handle`; its pages ``&page=<n>``), so a citation names the member's own
bytes inside the container's pinned bytes; readers are gated by the container
(:func:`split_member`). Every member file the extractor could not read and every part
of the container's structure that was not read (an encrypted or over-cap member, an
undecoded ``.msg`` property) is a gap with its reason, never silently dropped.
"""

from __future__ import annotations

from collections import OrderedDict
import contextlib
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import zlib
from typing import Any, Callable, Generic, Hashable, Iterator, Mapping, TypeVar
from urllib.parse import parse_qs, quote

from ontology.mentions import PAGE_BREAK, is_paged_file, text_pages
from operating.msg_reader import CFB_MAGIC

# text_status values (besides the extractor's no_text / unsupported / failed).
TEXT_EXTRACTED = "extracted"
TEXT_OCR = "extracted_ocr"           # some pages OCR-read: untrusted, unconfirmed text
OCR_UNAVAILABLE = "ocr_unavailable"  # scanned pages and no local OCR engine on this host
OCR_FAILED = "ocr_failed"            # stored typed: ocr_failed:<cause> (ocr_failure_cause)
OCR_NO_TEXT = "ocr_no_text"          # OCR ran and read nothing
# Scanned pages left for a later OCR pass on a host with an engine (the capture defers OCR
# so one slow scan never holds the capture: ``capture_text(defer_ocr=True)``); the text
# stored is the text layer's.
OCR_PENDING = "ocr_pending"
# Scanned pages whose OCR was put off with the bytes dropped (the mail capture's ``ocr``
# ``deferred``, ``integrations.capture_mail``): the text layer (if any) is the reading
# every reader gets, the reading carries ``ocr_deferred`` (a named gap, never an "empty"
# or "absent" document), and a later pass re-fetches the bytes and OCR-reads them.
OCR_DEFERRED = "ocr_deferred"
TEXT_EXCEEDS_BOUND = "exceeds_bound"  # over the extractor's bound: never read whole
TEXT_READ = (TEXT_EXTRACTED, TEXT_OCR)
# Statuses a later capture run re-reads from the stored bytes when this host has OCR.
TEXT_UNREAD = ("no_text", OCR_UNAVAILABLE, OCR_PENDING)
# An OCR failure stored before a tool's exit was typed by tool (``tool_exit``, or the bare
# ``ocr_failed``): its cause is unknown, so a host with OCR reads the stored bytes once
# more; the new reading is typed by tool and is not among these, so never a loop.
UNTYPED_OCR_FAILURES = (OCR_FAILED, f"{OCR_FAILED}:tool_exit")

MAX_CACHED_CHARS = 32_000_000   # the readings kept per process (each <= MAX_TEXT chars)
_URL = "https://capture.invalid/document"   # the extractor wants an https URL; never fetched
_SHA256 = re.compile(r"[0-9a-f]{64}")


def text_status_kind(status: str) -> str:
    """A text status without its typed OCR cause (``ocr_failed:deadline`` is
    ``ocr_failed``): what every reader that labels or counts statuses compares."""
    return OCR_FAILED if ocr_failure_cause(status) is not None else status


def ocr_failure_cause(status: Any) -> str | None:
    """Why the capture's OCR run of a file failed, from its text status: a cause of
    ``document_understanding.OCR_FAILURE_CAUSES``, ``untyped`` for a bare
    ``ocr_failed`` (stored before causes were typed), ``unknown`` for any other suffix
    (counted, never dropped); None for any other status."""
    from .document_understanding import OCR_FAILURE_CAUSES

    if not isinstance(status, str) or status.split(":", 1)[0] != OCR_FAILED:
        return None
    cause = status.partition(":")[2]
    return "untyped" if status == OCR_FAILED else \
        cause if cause in OCR_FAILURE_CAUSES else "unknown"


def _mime(row: Mapping[str, Any]) -> str:
    mime = row.get("content_type") or row.get("mime_type")
    return mime if isinstance(mime, str) and mime.strip() else "application/octet-stream"


def _name(row: Mapping[str, Any]) -> str:
    return row["name"] if isinstance(row.get("name"), str) else ""


def file_type(row: Mapping[str, Any]) -> tuple[str, str]:
    """(MIME type, name) a captured file is read as (:func:`captured_text`)."""
    return _mime(row), _name(row)


def _joined(text: str, pages: Any) -> str:
    """A PDF's extracted text, page by page (``PAGE_BREAK`` between pages, none inside)."""
    if not pages:
        return text.replace(PAGE_BREAK, " ")
    return PAGE_BREAK.join(text[p["start"]:p["end"]].replace(PAGE_BREAK, " ") for p in pages)


# The extractor's errors for text over its character bound (MAX_TEXT).
_TEXT_BOUND_ERRORS = frozenset({"extracted document text exceeds bound",
                                "extracted PDF text exceeds bound",
                                "extracted workbook text exceeds bound"})
_WORKBOOK_TEXT = 1_000_000   # a workbook's text read for its first characters


def capture_text(content: bytes, record: Mapping[str, Any], *,
                 max_chars: int | None = None, defer_ocr: bool = False
                 ) -> tuple[str, str | None]:
    """(text status, text to store) of newly captured bytes (``record``: the file's
    ``name``, ``mime_type`` or ``content_type`` and optional ``original_url``). A
    failure is a status, never an exception. ``max_chars``: text over the extractor's
    character bound gives its first ``max_chars`` characters (:func:`_first_chars`)
    instead of ``failed``; never for a capture, whose pinned text is the whole file's.
    An OCR run that failed is ``ocr_failed:<cause>``, its typed cause
    (``document_understanding.OcrFailed``). ``defer_ocr``: no OCR run now; a PDF with
    pages lacking a text layer is ``ocr_pending`` (text layer kept) on a host with an
    engine, so a later pass OCR-reads the stored bytes (``ocr_unavailable`` without one)."""
    from .document_understanding import (MAX_BYTES, OcrFailed, extract_document,
                                         local_ocr_available)

    if len(content) > MAX_BYTES:
        return TEXT_EXCEEDS_BOUND, None
    url = record.get("original_url")
    mime, name = _mime(record), _name(record)
    paged = is_paged_file({"mime_type": mime, "name": name})
    engine = paged and local_ocr_available()
    ocr = engine and not defer_ocr
    try:
        found = extract_document(content, mime,
                                 original_url=url if isinstance(url, str) else _URL,
                                 filename=name, local_pdf_ocr=ocr)
    except Exception as exc:  # noqa: BLE001 - an unreadable file or parser defect is a status
        # Only an OCR run that failed is ocr_failed: a PDF over the text bound is failed
        # and one that needs its user password unsupported (status below), as without OCR.
        if ocr and str(exc) == "local_pdf_ocr_unavailable":
            return OCR_UNAVAILABLE, None
        if max_chars is not None and str(exc) in _TEXT_BOUND_ERRORS:
            return _first_chars(content, mime, name, max_chars)
        if ocr and isinstance(exc, OcrFailed):
            return f"{OCR_FAILED}:{exc.cause}", None
        return (OCR_FAILED if ocr and str(exc) == "local_pdf_ocr_failed" else "failed"), None
    # A PDF over the page bound: the text of its pages 1-MAX_PAGES (module docstring).
    read = found.status == "extracted" and bool(found.text)
    text = (_joined(found.text, found.pages) if paged else found.text) if read else None
    if paged and not ocr and (found.status == "no_text" or any(
            not found.text[p["start"]:p["end"]].strip() for p in found.pages)):
        # Pages without a text layer were not read: later when only deferred.
        return (OCR_PENDING if engine else OCR_UNAVAILABLE), text
    if read:
        return (TEXT_OCR if found.ocr_pages else TEXT_EXTRACTED), text
    if ocr and found.status == "no_text":
        return OCR_NO_TEXT, None
    return (found.status if found.status in ("no_text", "unsupported") else "failed"), None


def _first_chars(content: bytes, mime: str, name: str, max_chars: int
                 ) -> tuple[str, str | None]:
    """(text status, first ``max_chars`` characters) of a file whose text is over the
    extractor's bound, read by the extractor's own readers without that bound and without
    OCR: a PDF page without a text layer leaves it ``ocr_unavailable``."""
    from .doc_convert import convertible_kind
    from .document_understanding import is_plain_text, read_pages
    from .xlsx_extract import extract_xlsx_text

    kind = mime.split(";", 1)[0].strip().lower()
    readable = convertible_kind(kind, name)
    blank = False
    try:
        if readable is not None:
            found = read_pages(content, readable, max_text=None)
            if found.status != "extracted" or any(page.unread for page in found.pages):
                return "failed", None
            text = _joined(found.text, found.spans) if readable == "pdf" else found.text
            blank = readable == "pdf" and any(
                not found.text[p["start"]:p["end"]].strip() for p in found.spans)
        elif kind == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or (
                kind == "application/octet-stream" and name.lower().endswith(".xlsx")):
            text, status, *_ = extract_xlsx_text(content, max_text=_WORKBOOK_TEXT)
            if status != "extracted":
                return "failed", None
        elif is_plain_text(kind, name):
            text = content.decode("utf-8-sig")
        else:
            return "failed", None
    except Exception:  # noqa: BLE001 - an unreadable file or parser defect is a status
        return "failed", None
    if not text.strip():
        return "failed", None
    return (OCR_UNAVAILABLE if blank else TEXT_EXTRACTED), text[:max_chars]


@dataclass(frozen=True)
class CapturedText:
    """One reading of a captured file: the extractor's status, method and MIME type, its
    pages (``(page, text)``; one ``(None, text)`` when unpaged), the pages read by OCR,
    ``text_sha256`` when it is the capture's pinned OCR text, and the (first, last)
    pages of a PDF over the page bound that were not read."""

    source_sha256: str
    mime_type: str
    status: str
    method: str
    pages: tuple[tuple[int | None, str], ...]
    ocr_pages: frozenset[int]
    paged: bool
    text_sha256: str | None = None
    skipped_pages: tuple[int, int] | None = None
    # Its scanned pages were not OCR-read yet (``OCR_DEFERRED``): what it does not state
    # is unread, not absent.
    ocr_deferred: bool = False

    @property
    def skipped_by_cap(self) -> dict[str, Any] | None:
        """The pages past the page bound, as the extractor's evidence records them
        (``first_page``, ``last_page``, ``cap`` ``pages``, ``limit``), else None."""
        from .document_understanding import MAX_PAGES

        if self.skipped_pages is None:
            return None
        return {"first_page": self.skipped_pages[0], "last_page": self.skipped_pages[1],
                "cap": "pages", "limit": MAX_PAGES}

    @property
    def text_withheld(self) -> str | None:
        """Why a reader without a gap channel (a mention document) indexes none of this
        text: ``pdf_pages_skipped_by_cap:<first>-<last>`` (the extractor's warning) when
        pages past the page bound were not read, else None."""
        if self.skipped_pages is None:
            return None
        return f"pdf_pages_skipped_by_cap:{self.skipped_pages[0]}-{self.skipped_pages[1]}"

    @property
    def text(self) -> str | None:
        """The text mention extraction and resolve cite: a paged file's pages joined by
        ``PAGE_BREAK`` (cited ``&page=<n>``), else the whole text; None without text."""
        if self.status != "extracted" or not any(t.strip() for _, t in self.pages):
            return None
        if self.paged and self.pages[0][0] is not None:
            return PAGE_BREAK.join(t.replace(PAGE_BREAK, " ") for _, t in self.pages)
        return "".join(t for _, t in self.pages).replace(PAGE_BREAK, " ")

    def page(self, number: int) -> str | None:
        """The text of page ``number`` (1-based) of a paged file, as cited; else None."""
        text = self.text
        pages = text_pages(text) if self.paged and text is not None else []
        return pages[number - 1] if 1 <= number <= len(pages) else None


def read_text(content: bytes, mime_type: str, *, filename: str = "",
              text: str | None = None, local_pdf_ocr: bool = False) -> CapturedText:
    """One reading of ``content``: ``text`` (the capture's OCR text of these bytes, pages
    split by ``PAGE_BREAK``, every page OCR-read) when given, else the bytes through the
    extractor. Of a PDF over the page bound, pages 1-``MAX_PAGES`` and the range past
    them (a pinned text of ``MAX_PAGES`` pages: from the PDF's page count, which must be
    known). Raises what the extractor raises (bytes over its bounds)."""
    from ontology.core import ValidationError
    from .document_understanding import MAX_PAGES, extract_document, pdf_page_count

    digest = sha256(content).hexdigest()
    paged = is_paged_file({"mime_type": mime_type, "name": filename})
    if text is not None:
        pages = tuple(enumerate(text_pages(text), 1))
        skipped = None
        if paged and len(pages) >= MAX_PAGES:
            count = pdf_page_count(content)
            if count is None:  # whether pages were left unread cannot be told
                raise ValidationError("PDF page count unknown")
            skipped = (len(pages) + 1, count) if count > len(pages) else None
        return CapturedText(digest, mime_type, "extracted", "captured_ocr_text", pages,
                            frozenset(page for page, _ in pages), paged,
                            sha256(text.encode("utf-8")).hexdigest(), skipped)
    found = extract_document(content, mime_type, original_url=_URL, filename=filename,
                             local_pdf_ocr=local_pdf_ocr)
    pages = (tuple((p["page"], found.text[p["start"]:p["end"]]) for p in found.pages)
             if found.pages else ((None, found.text),))
    return CapturedText(digest, found.mime_type, found.status, found.method, pages,
                        frozenset(found.ocr_pages), paged, skipped_pages=found.skipped_pages)


# --------------------------------------------------------------------------- stored forms
#
# A blob of a capture store's blob directory is kept in one of three forms, the reader
# never told which: ``<sha256>`` (the bytes), ``<sha256>.z`` (the bytes zlib-compressed: a
# mail body or text, ``integrations.capture_mail``) or, for an attachment original the mail
# capture dropped once its text was read, ``<sha256>.dropped``: the readings
# (:class:`CapturedText`) every reader got from those bytes, kept so that a citation of its
# text still resolves to the same text, pages and status, plus the bytes' size and
# quickXorHash. The bytes themselves are re-read from the source on demand
# (``MailCaptureStore.refetch_original``), never guessed.

COMPRESSED_SUFFIX = ".z"
DROPPED_SUFFIX = ".dropped"
DROPPED_FORMAT = "plx-dropped-original/v1"
MAX_INFLATED_BYTES = 64 * 1024 * 1024   # a stored body or text inflates to at most this


def blob_path(blobs: str | Path, digest: str, suffix: str = "") -> Path:
    """The file of one form of the blob ``digest`` (a SHA-256 hex digest)."""
    return Path(blobs) / digest[:2] / (digest + suffix)


def inflate(packed: bytes) -> bytes | None:
    """The bytes of a ``.z`` form, or None when it is not one whole zlib stream within
    :data:`MAX_INFLATED_BYTES`."""
    try:
        stream = zlib.decompressobj()
        content = stream.decompress(packed, MAX_INFLATED_BYTES)
    except zlib.error:
        return None
    if stream.unconsumed_tail or not stream.eof or stream.unused_data:
        return None
    return content


def read_blob(blobs: str | Path, digest: Any) -> bytes | None:
    """Content-addressed bytes of a capture store's blob directory, re-hashed; None when
    missing (or dropped), altered or not a SHA-256. The bytes form is read first; only
    when it does not exist, the compressed form."""
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return None
    try:
        content: bytes | None = blob_path(blobs, digest).read_bytes()
    except FileNotFoundError:
        try:
            content = inflate(blob_path(blobs, digest, COMPRESSED_SUFFIX).read_bytes())
        except OSError:
            return None
    except OSError:
        return None
    return content if content is not None and sha256(content).hexdigest() == digest else None


def reading_key(mime: str, name: str, text_digest: str | None) -> str:
    """The key of one reading of a dropped original: what :func:`captured_text` reads it
    by (MIME type, name, the pinned OCR text's digest or None)."""
    return json.dumps([mime, name, text_digest])


def encode_reading(found: CapturedText | None) -> dict[str, Any] | None:
    if found is None:
        return None
    return {"source_sha256": found.source_sha256, "mime_type": found.mime_type,
            "status": found.status, "method": found.method,
            "pages": [[page, text] for page, text in found.pages],
            "ocr_pages": sorted(found.ocr_pages), "paged": found.paged,
            "text_sha256": found.text_sha256,
            "skipped_pages": None if found.skipped_pages is None else list(found.skipped_pages)}


def decode_reading(value: Any) -> CapturedText | None:
    if value is None:
        return None
    skipped = value["skipped_pages"]
    return CapturedText(value["source_sha256"], value["mime_type"], value["status"],
                        value["method"], tuple((page, text) for page, text in value["pages"]),
                        frozenset(value["ocr_pages"]), bool(value["paged"]),
                        value["text_sha256"], None if skipped is None else tuple(skipped))


def _readings_sha256(readings: Mapping[str, Any]) -> str:
    return sha256(json.dumps(readings, sort_keys=True, ensure_ascii=False).encode("utf-8")
                  ).hexdigest()


def write_dropped(blobs: str | Path, digest: str, size: int, readings: Mapping[str, Any],
                  quick_xor: str | None) -> int:
    """Write the ``.dropped`` form of ``digest`` (``readings``: :func:`reading_key` ->
    :func:`encode_reading`), atomically, private; returns its size on disk."""
    document = {"format": DROPPED_FORMAT, "sha256": digest, "bytes": size,
                "quick_xor": quick_xor, "readings": dict(readings),
                "readings_sha256": _readings_sha256(readings)}
    packed = zlib.compress(json.dumps(document, sort_keys=True, ensure_ascii=False
                                      ).encode("utf-8"), 6)
    target = blob_path(blobs, digest, DROPPED_SUFFIX)
    target.parent.mkdir(exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(packed)
    os.replace(temporary, target)
    return len(packed)


def read_dropped(blobs: str | Path, digest: Any) -> dict[str, Any] | None:
    """The ``.dropped`` form of ``digest``: ``bytes``, ``quick_xor`` and ``readings``;
    None when there is none or it is not whole (its format, digest or readings checksum
    does not hold)."""
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return None
    try:
        raw = inflate(blob_path(blobs, digest, DROPPED_SUFFIX).read_bytes())
        document = None if raw is None else json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("format") != DROPPED_FORMAT \
            or document.get("sha256") != digest \
            or not isinstance(document.get("readings"), dict) \
            or document.get("readings_sha256") != _readings_sha256(document["readings"]):
        return None
    return document


def readings_of(content: bytes, keys: Any, texts: Callable[[str], str | None]
                ) -> dict[str, Any] | None:
    """The reading of ``content`` under each ``(mime, name, text_digest)`` of ``keys`` as
    :func:`captured_text` makes it from the bytes (``texts``: a pinned OCR text by its
    digest), encoded by :func:`reading_key`; a key over the extractor's bounds reads None
    (as remembered by the memo). None when a reading cannot be made now (a pinned text
    missing, a transient or unexpected failure): the bytes must then be kept."""
    out: dict[str, Any] = {}
    for mime, name, text_digest in keys:
        text = None
        if text_digest is not None:
            text = texts(text_digest)
            if text is None:
                return None
        try:
            found: CapturedText | None = read_text(content, mime, filename=name, text=text)
        except Exception as exc:  # noqa: BLE001 - classified exactly as the memo does
            if _failed(exc, f"original {sha256(content).hexdigest()[:12]}") != "limit":
                return None
            found = None
        out[reading_key(mime, name, text_digest)] = encode_reading(found)
    return out


def ocr_digest(row: Mapping[str, Any]) -> str | None:
    """The pinned OCR text of a captured version the capture OCR-read, else None."""
    digest = row.get("text_sha256")
    return digest if row.get("text_status") == TEXT_OCR and isinstance(digest, str) else None


_T = TypeVar("_T")


_LOG = logging.getLogger(__name__)

# The extractor's bounds (``document_understanding``, ``xlsx_extract``): the same bytes
# exceed them on every read, so a read that hit one is unreadable for good and remembered
# as such. A PDF whose page count could not be told is not among them: ``pdfinfo`` timing
# out gives that too.
LIMIT_ERRORS = _TEXT_BOUND_ERRORS | frozenset({
    "PDF page count exceeds bound", "document bytes required within 20 MB bound",
    "document warning count exceeds bound", "extracted claim count exceeds bound",
    "workbook XML part exceeds bound", "workbook cell exceeds bound",
    "workbook expansion exceeds bound", "workbook shared strings exceed bound",
    "workbook sheet count exceeds bound", "workbook populated cell count exceeds bound"})


def read_failure(exc: Exception) -> str:
    """What a read that raised ``exc`` was: ``limit`` (over one of the extractor's
    bounds, :data:`LIMIT_ERRORS`: the same on every read), ``transient`` (a later read
    may succeed: ``child_process.Abandoned``, an ``OSError`` or timeout, the extractor's
    other errors, such as an OCR run that failed or timed out) or ``unexpected`` (any
    other exception: a defect, logged by type, never by its text)."""
    from ontology.core import ValidationError
    from .child_process import Abandoned

    if isinstance(exc, ValidationError):
        return "limit" if str(exc) in LIMIT_ERRORS else "transient"
    if isinstance(exc, (Abandoned, OSError, subprocess.TimeoutExpired, MemoryError)):
        return "transient"
    return "unexpected"


def _failed(exc: Exception, what: str) -> str:
    """:func:`read_failure` of ``exc``; an unexpected one is logged (its type and
    ``what``, a digest: never the document's text or name)."""
    kind = read_failure(exc)
    if kind == "unexpected":
        _LOG.warning("unexpected %s reading %s: not remembered", type(exc).__name__, what)
    return kind


class NotRemembered(Exception):
    """A read whose result is not remembered (:class:`ReadingMemo`): its bytes missing
    or altered (a later capture may hold them; ``value`` None), or a container whose
    member read failed transiently (``value`` the container's reading)."""

    def __init__(self, what: str, value: Any = None) -> None:
        super().__init__(what)
        self.value = value


class ReadingMemo(Generic[_T]):
    """The one rule of the process-wide reading memos (:func:`captured_text`,
    :func:`container_members`, ``ontology.document_fields``' fields): what a read
    returns is remembered by its key, a real reading without text or a None result
    included, least recently used out first once the entries' ``size`` passes ``bound``
    (the newest always stays). A read that raised (:func:`read_failure`) gives None: over
    one of the extractor's bounds it is remembered (the same bytes fail every read);
    transient (``child_process.Abandoned``, an ``OSError``, a timeout) or unexpected (a
    defect, logged and counted in ``unexpected``) it is not, and the next caller reads
    again, so a transient failure is never unreadable for the life of the process. A read
    that raises :class:`NotRemembered` gives its ``value``, not remembered. Thread-safe; a
    caller binds the memo once per read, so a read still running on a thread when the
    module's memo is replaced (a check run's fresh one, :meth:`empty`) remembers into the
    memo it began with (a read that starts after the swap uses the new one)."""

    def __init__(self, bound: int, size: Callable[[_T], int]) -> None:
        self.bound, self._size = bound, size
        self._entries: OrderedDict[Hashable, _T] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()
        self.unexpected = 0
        # A second level kept on disk between processes (:class:`KeptReadings`), while a
        # projection build sets one (:func:`kept_readings`); None: this process only.
        self.backing: Any = None

    def empty(self) -> "ReadingMemo[_T]":
        """A new, empty memo with this one's bound and sizes."""
        return ReadingMemo(self.bound, self._size)

    def __contains__(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def read(self, key: Hashable, read: Callable[[], _T],
             present: Callable[[], bool] | None = None) -> _T | None:
        """The remembered result of ``key``, else ``read()``'s, remembered; when ``read``
        raised, None (remembered only over a bound) or :class:`NotRemembered`'s value."""
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]
        backing = self.backing
        # A kept reading is served only while its bytes (or their dropped readings) are
        # on disk: a file gone reads as gone, as the read itself would.
        kept = (backing.get(key) if backing is not None
                and (present is None or present()) else None)
        if kept is not None:
            found: Any = kept[0]
        else:
            try:
                found = read()
            except NotRemembered as exc:
                return exc.value
            except Exception as exc:  # noqa: BLE001 - a failed read is no result
                kind = _failed(exc, f"memo key {_digest_of(key)}")
                if kind != "limit":
                    if kind == "unexpected":
                        with self._lock:
                            self.unexpected += 1
                    return None
                found = None
            if backing is not None:
                backing.put(key, found)
        with self._lock:
            if key in self._entries:   # another thread read it meanwhile: one entry
                self._entries.move_to_end(key)
                return self._entries[key]
            self._entries[key] = found
            self._total += self._size(found)
            while self._total > self.bound and len(self._entries) > 1:
                self._total -= self._size(self._entries.popitem(last=False)[1])
        return found


def _digest_of(key: Hashable) -> str:
    """The first characters of a memo key's bytes digest (its first part), for a log."""
    head = key[0] if isinstance(key, tuple) and key else key
    return head[:12] if isinstance(head, str) else "?"


def _size(found: CapturedText | None) -> int:
    return 64 + (0 if found is None else sum(len(t) for _, t in found.pages))


_CACHE: ReadingMemo[CapturedText] = ReadingMemo(MAX_CACHED_CHARS, _size)


def _blob_kept(blobs: str | Path, digest: str) -> bool:
    """Some form of the blob ``digest`` (its bytes, compressed or dropped) is on disk."""
    return any(blob_path(blobs, digest, suffix).exists()
               for suffix in ("", COMPRESSED_SUFFIX, DROPPED_SUFFIX))


class KeptReadings:
    """The readings of :func:`captured_text` kept on disk between processes (a SQLite file
    in a projection build's state directory, beside the generations it reads them for):
    what :class:`ReadingMemo` remembers (a reading, or None over one of the extractor's
    bounds) is kept under its key (bytes digest, MIME type, name, pinned OCR text digest)
    and the digest of the code that reads (``code``: ``ontology.project.derivation_code``),
    and read back only under the same code, so a kept reading is the one a read now would
    make. A reading that does not survive its encoding unchanged is not kept. Readings no
    build used in its last ``KEEP_BUILDS`` are dropped when a build ends. Any failure of
    the file is a miss, never a failed read."""

    KEEP_BUILDS = 3
    _BATCH = 500   # writes per commit

    def __init__(self, path: str | Path, code: str) -> None:
        import sqlite3
        self.code = code
        self._lock = threading.Lock()
        self._pending = 0
        self.hits = self.misses = self.kept = 0
        # Private: the text of captured files (credentialed state, never shared).
        os.close(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(path, 0o600)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = NORMAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS readings (key TEXT PRIMARY KEY, "
                             "code TEXT NOT NULL, value BLOB NOT NULL, used INTEGER NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, "
                             "value TEXT NOT NULL)")
            row = self._db.execute("SELECT value FROM meta WHERE key = 'builds'").fetchone()
            self.build = (int(row[0]) if row else 0) + 1
            self._db.execute("INSERT INTO meta VALUES ('builds', ?) ON CONFLICT(key) DO "
                             "UPDATE SET value = excluded.value", (str(self.build),))
            self._db.execute("BEGIN")
        except BaseException:
            self._db.close()
            raise

    @staticmethod
    def _key(key: Hashable) -> str | None:
        try:
            return json.dumps(list(key), ensure_ascii=False)   # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _wrote(self) -> None:
        self._pending += 1
        if self._pending >= self._BATCH:
            self._db.execute("COMMIT")
            self._db.execute("BEGIN")
            self._pending = 0

    def get(self, key: Hashable) -> tuple[CapturedText | None] | None:
        """``(reading,)`` kept for ``key`` under this code, else None (a miss)."""
        name = self._key(key)
        if name is None:
            return None
        try:
            with self._lock:
                row = self._db.execute("SELECT code, value FROM readings WHERE key = ?",
                                       (name,)).fetchone()
                if row is None or row[0] != self.code:
                    self.misses += 1
                    return None
                self._db.execute("UPDATE readings SET used = ? WHERE key = ?",
                                 (self.build, name))
                self._wrote()
                self.hits += 1
            return (decode_reading(json.loads(zlib.decompress(row[1]).decode("utf-8"))),)
        except Exception:  # noqa: BLE001 - an unreadable entry is a miss
            return None

    def put(self, key: Hashable, found: CapturedText | None) -> None:
        name = self._key(key)
        if name is None:
            return
        try:
            encoded = encode_reading(found)
            if decode_reading(json.loads(json.dumps(encoded))) != found:
                return
            value = zlib.compress(json.dumps(encoded, ensure_ascii=False).encode("utf-8"), 6)
            with self._lock:
                self._db.execute("INSERT INTO readings VALUES (?, ?, ?, ?) ON CONFLICT(key) "
                                 "DO UPDATE SET code = excluded.code, value = excluded.value, "
                                 "used = excluded.used", (name, self.code, value, self.build))
                self._wrote()
                self.kept += 1
        except Exception:  # noqa: BLE001 - a reading not kept is read again next time
            return

    def close(self, *, prune: bool) -> None:
        """Commit and close; ``prune`` (a build that ended): drop the readings no build of
        the last ``KEEP_BUILDS`` used."""
        try:
            with self._lock:
                if prune:
                    self._db.execute("DELETE FROM readings WHERE used <= ?",
                                     (self.build - self.KEEP_BUILDS,))
                self._db.execute("COMMIT")
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._db.close()


@contextlib.contextmanager
def kept_readings(path: str | Path) -> Iterator[KeptReadings]:
    """While open, :func:`captured_text` reads through the readings kept at ``path``
    (:class:`KeptReadings`) and keeps the new ones there. The readings are pruned when the
    block ends without an exception."""
    from ontology.project import derivation_code

    memo = _CACHE
    kept = KeptReadings(path, derivation_code())
    memo.backing = kept
    ended = False
    try:
        yield kept
        ended = True
    finally:
        memo.backing = None
        kept.close(prune=ended)


def captured_text(blobs: str | Path, row: Mapping[str, Any]) -> CapturedText | None:
    """The reading of one captured file (module docstring). ``row``: its
    ``content_sha256``, ``content_type`` or ``mime_type``, ``name`` and, when the
    capture stored text, ``text_status`` and ``text_sha256``; ``blobs``: the capture
    store's blob directory. None when the bytes (or the pinned OCR text) are missing or
    altered or the read raised (neither remembered: a later capture may hold them, a
    later read may succeed; :class:`ReadingMemo`)."""
    digest = row.get("content_sha256")
    if not isinstance(digest, str):
        return None
    mime, name, text_digest = _mime(row), _name(row), ocr_digest(row)

    def read() -> CapturedText | None:
        content = read_blob(blobs, digest)
        if content is None:   # a dropped original reads as the readings it left
            kept = read_dropped(blobs, digest)
            key = reading_key(mime, name, text_digest)
            if kept is None or key not in kept["readings"]:
                raise NotRemembered(digest)
            return decode_reading(kept["readings"][key])
        text = None if text_digest is None else read_blob(blobs, text_digest)
        if text_digest is not None and text is None:
            raise NotRemembered(digest)
        return read_text(content, mime, filename=name,
                         text=None if text is None else text.decode("utf-8"))
    found = _CACHE.read((digest, mime, name, text_digest), read,
                        lambda: _blob_kept(blobs, digest)
                        and (text_digest is None or _blob_kept(blobs, text_digest)))
    if found is not None and row.get("text_status") == OCR_DEFERRED:
        return replace(found, ocr_deferred=True)
    return found


# The statuses whose stored text is the text layer only: scanned pages were left for a
# later OCR pass (``capture_text(defer_ocr=True)``, the mail capture's deferral).
SCANS_PENDING = (OCR_PENDING, OCR_DEFERRED)


@dataclass(frozen=True)
class StoredText:
    """The text a capture stored for one captured file (:func:`stored_text`): ``text``
    as :attr:`CapturedText.text` cites it (None: none stored, missing, altered or
    blank), whether it is paged, and the pages past the page bound it does not hold
    (``skipped_by_cap``'s shape; ``{"cap": "pages", "limit": ...}`` alone when the
    range cannot be told without reading the file)."""

    text: str | None
    paged: bool
    skipped_by_cap: dict[str, Any] | None = None


def stored_text(blobs: str | Path, row: Mapping[str, Any]) -> StoredText:
    """The text the capture stored for a captured file (``row``: its record with
    ``text_status``, ``text_sha256`` and ``content_sha256``), read from its pinned text
    blob (re-hashed) and never from its bytes: no extraction and no OCR run. An original
    the capture dropped reads as the reading it kept (``.dropped``: the reading
    :func:`captured_text` serves for it). Otherwise the text is what the capture's own
    extractor run stored: a PDF's pages joined by ``PAGE_BREAK``, any other file's whole
    text (``capture_text``); an OCR-read file's reading is this text itself. A typed PDF
    the capture read on a host with OCR went through the OCR-enabled page reader, whose
    text layer can differ from the reading :func:`captured_text` makes without OCR.
    Of a PDF holding ``MAX_PAGES``
    pages the range past them is told from the PDF's page count (no page read) or the
    reading a dropped original kept; when neither tells it, every page past the bound is
    taken as unread (fail closed)."""
    from .document_understanding import MAX_PAGES, pdf_page_count

    mime, name = _mime(row), _name(row)
    paged = is_paged_file({"mime_type": mime, "name": name})
    digest = row.get("content_sha256")
    if isinstance(digest, str) and _SHA256.fullmatch(digest) is not None \
            and not blob_path(blobs, digest).exists() \
            and not blob_path(blobs, digest, COMPRESSED_SUFFIX).exists():
        # A dropped original: the reading it kept is the one every reader cites.
        kept = read_dropped(blobs, digest)
        key = reading_key(mime, name, ocr_digest(row))
        if kept is not None and key in kept["readings"]:
            found = decode_reading(kept["readings"][key])
            return StoredText(None if found is None else found.text, paged,
                              None if found is None else found.skipped_by_cap)
    raw = read_blob(blobs, row.get("text_sha256"))
    try:
        text = None if raw is None else raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        return StoredText(None, paged)
    pages = text_pages(text) if paged else [text]
    if not any(page.strip() for page in pages):
        return StoredText(None, paged)
    text = PAGE_BREAK.join(pages) if paged else text.replace(PAGE_BREAK, " ")
    skipped = None
    if paged and len(pages) >= MAX_PAGES:
        count = None
        content = read_blob(blobs, row.get("content_sha256"))
        if content is not None:
            try:
                count = pdf_page_count(content)
            except Exception:  # noqa: BLE001 - an uncounted PDF is read as capped
                count = None
        else:
            kept = read_dropped(blobs, row.get("content_sha256"))
            readings = [] if kept is None else [decode_reading(v) for v in
                                               kept["readings"].values() if v is not None]
            ends = {r.skipped_pages for r in readings}
            if len(ends) == 1:
                (end,) = ends
                count = len(pages) if end is None else end[1]
        if count is None:
            skipped = {"cap": "pages", "limit": MAX_PAGES}
        elif count > len(pages):
            skipped = {"first_page": len(pages) + 1, "last_page": count, "cap": "pages",
                       "limit": MAX_PAGES}
    return StoredText(text, paged, skipped)


# --------------------------------------------------------------------------- containers

MEMBER_KEYS = ("member", "member_sha256")


@dataclass(frozen=True)
class Member:
    """One member file of a container: its path in the container (``inner.zip!/a.pdf``),
    name, bytes' SHA-256 and size, the MIME type it was read as, whether it is a
    container itself (its members follow), and its reading (None: over the extractor's
    bounds or unreadable)."""

    path: str
    name: str
    sha256: str
    size: int
    mime_type: str
    container: bool
    reading: CapturedText | None

    @property
    def text_status(self) -> str:
        """``extracted`` when the member has text, else why not (the extractor's
        status: ``unsupported``, ``no_text``, ...; ``failed`` when it raised)."""
        if self.reading is None:
            return "failed"
        if self.reading.text is None and self.reading.status == "extracted":
            return "no_text"
        return self.reading.status


@dataclass(frozen=True)
class ContainerReading:
    """The member files of one container's bytes and the structural parts not read
    (``path``, ``kind``, ``state`` ``unread``/``skipped_by_cap``, ``reason`` or
    ``cap``/``limit``, ``sha256`` when the part had bytes); ``retry`` when a member's
    read failed transiently (:func:`read_failure`; such a reading is not remembered)."""

    source_sha256: str
    members: tuple[Member, ...]
    gaps: tuple[dict[str, Any], ...]
    retry: bool = field(default=False, compare=False)   # a member read failed transiently

    def member(self, path: str, digest: str | None) -> Member | None:
        """The member at ``path`` whose bytes are ``digest``, else None."""
        return next((m for m in self.members if m.path == path and m.sha256 == digest), None)


# A member without a mail content type is typed by its name: the extractor types the
# rest (Office files, .msg/.eml) by name itself; a fixed table, never the host's.
_MEMBER_SUFFIX_TYPES = {".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/plain",
                        ".md": "text/plain", ".json": "application/json",
                        ".html": "text/html", ".htm": "text/html"}


def _member_mime(name: str, hint: str | None) -> str:
    """A member's MIME type: its mail entity's content type, else by its name's suffix."""
    hint = (hint or "").split(";", 1)[0].strip().lower()
    if hint and hint != "application/octet-stream":
        return hint
    return _MEMBER_SUFFIX_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def read_container(content: bytes, name: str, mime_type: str | None = None, *,
                   local_pdf_ocr: bool = False) -> ContainerReading | None:
    """The members of ``content`` (named ``name``, of ``mime_type``), each read by
    :func:`read_text` (a PDF member's scanned pages OCR-read locally when
    ``local_pdf_ocr``; an OCR failure makes its reading None); None when it is not a
    container (module docstring)."""
    from operating.use_case_parts import members

    found = members(content, name, mime_type)
    if found is None:
        return None
    out, retry = [], False
    for item in found["files"]:
        mime = _member_mime(item["name"], item["hint"])
        try:
            reading: CapturedText | None = read_text(item["data"], mime, filename=item["name"],
                                                     local_pdf_ocr=local_pdf_ocr)
        except Exception as exc:  # noqa: BLE001 - an unreadable member is a gap, never a guess
            reading = None
            retry = retry or _failed(exc, f"member {item['sha256'][:12]}") != "limit"
        out.append(Member(item["path"], item["name"], item["sha256"], len(item["data"]),
                          mime, item["container"], reading))
    gaps = [{key: part[key] for key in ("path", "kind", "state", "reason", "cap",
                                        "limit", "sha256", "size") if key in part}
            for part in found["gaps"]]
    for member in out:  # a member PDF's pages past the page bound, as the intake names them
        skipped = member.reading.skipped_by_cap if member.reading is not None else None
        if skipped is not None:
            first, last = skipped["first_page"], skipped["last_page"]
            gaps.append({"path": f"{member.path}!/page/{first}"
                                 + (f"-{last}" if last > first else ""),
                         "kind": "pdf_page", "state": "skipped_by_cap",
                         "cap": skipped["cap"], "limit": skipped["limit"]})
    return ContainerReading(sha256(content).hexdigest(), tuple(out), tuple(gaps), retry)


def container_members(blobs: str | Path, row: Mapping[str, Any]) -> ContainerReading | None:
    """:func:`read_container` of one captured file's pinned bytes (``row``: its
    ``content_sha256`` and ``name``), once per process and shared by every reader of a
    build (bounded like :func:`captured_text`); None when it is not a container, the
    bytes are missing or altered, or the read raised (:class:`ReadingMemo`); a reading
    whose member read failed transiently is returned, not remembered."""
    digest = row.get("content_sha256")
    if not isinstance(digest, str):
        return None
    name, mime = _name(row), _mime(row)
    memo = _MEMBER_READINGS
    if (digest, name, mime) not in memo and not _may_contain(blobs, digest, row):
        return None

    def read() -> ContainerReading | None:
        content = read_blob(blobs, digest)
        if content is None:
            raise NotRemembered(digest)
        found = read_container(content, name, mime)
        if found is not None and found.retry:   # a member read again by the next caller
            raise NotRemembered(digest, found)
        return found
    return memo.read((digest, name, mime), read)


def _members_size(found: ContainerReading | None) -> int:
    return 64 + (0 if found is None else sum(_size(m.reading) for m in found.members))


_MEMBER_READINGS: ReadingMemo[ContainerReading | None] = ReadingMemo(MAX_CACHED_CHARS,
                                                                     _members_size)


def member_view(blobs: str | Path, row: Mapping[str, Any], path: str, digest: str | None,
                page: int | None = None) -> dict[str, Any] | None:
    """The cited row of one member of a captured container (``row``: the container's
    captured metadata with ``content_sha256``) as resolve returns it: its path, name,
    SHA-256, MIME type, state and ``content_text`` (the member's text, or that page's
    of a paged member), or None when the container holds no such member (path and
    bytes) or the page does not exist. A part that was not read resolves without
    text; a member PDF read on its first pages only carries ``skipped_by_cap``."""
    found = container_members(blobs, row)
    if found is None:
        return None
    member = found.member(path, digest)
    if member is None:
        gap = next((g for g in found.gaps if g["path"] == path and g.get("sha256") == digest),
                   None)
        if gap is None or page is not None:
            return None
        return {"path": path, "name": None, "content_sha256": digest, "mime_type": None,
                "state": gap["state"], "content_text": None}
    reading = member.reading
    text = None if reading is None else reading.text if page is None else reading.page(page)
    if page is not None and text is None:
        return None
    view = {"path": path, "name": member.name, "content_sha256": member.sha256,
            "mime_type": member.mime_type, "text_status": member.text_status,
            "content_text": text}
    if reading is not None and reading.skipped_by_cap is not None:
        view["skipped_by_cap"] = reading.skipped_by_cap
    return view


def byte_pinned(handle: str, digest: str) -> str:
    """A capture handle that names the version's pinned bytes (``#sha256=``)."""
    if re.search(r"[#&]sha256=" + digest + r"$", handle):
        return handle
    return f"{handle}{'&' if '#' in handle else '#'}sha256={digest}"


_CONTAINER_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", CFB_MAGIC)


def _may_contain(blobs: str | Path, digest: str, row: Mapping[str, Any]) -> bool:
    """Whether a captured file can be a container, from its first bytes (a zip or a
    compound file) or its type (a mail), so a build reads no other file twice."""
    if _mime(row).startswith("message/") or _name(row).lower().endswith(".eml"):
        return True
    if _SHA256.fullmatch(digest) is None:
        return False
    try:
        with open(Path(blobs) / digest[:2] / digest, "rb") as handle:
            return handle.read(8).startswith(_CONTAINER_MAGIC)
    except OSError:
        return False


def member_handle(container: str, path: str, digest: str | None) -> str:
    """The citation of one member (``digest``: its bytes' SHA-256; None for a part that
    was not read and has no bytes) of the container cited by the byte-pinned
    ``container`` handle."""
    handle = f"{container}{'&' if '#' in container else '#'}member={quote(path, safe='')}"
    return handle if digest is None else f"{handle}&member_sha256={digest}"


def split_member(handle: Any) -> tuple[str, str, str | None] | None:
    """(container handle, member path, member SHA-256 or None) of a member citation
    (:func:`member_handle`, without a page), else None. A handle with the member keys
    anywhere but last, twice or malformed is not a member citation (None) and resolves
    nowhere."""
    if not isinstance(handle, str) or "member=" not in handle:
        return None
    head, sep, tail = handle.partition("member=")
    if not sep or head[-1:] not in ("#", "&"):
        return None
    try:
        keys = parse_qs("member=" + tail, keep_blank_values=True, strict_parsing=True,
                        max_num_fields=2)
    except ValueError:
        return None
    if set(keys) - set(MEMBER_KEYS) or any(len(v) != 1 for v in keys.values()) or \
            "member" not in keys or "member=" in tail:
        return None
    digest = keys.get("member_sha256", [None])[0]
    if digest is not None and _SHA256.fullmatch(digest) is None:
        return None
    rebuilt = member_handle(head[:-1], keys["member"][0], digest)
    return (head[:-1], keys["member"][0], digest) if rebuilt == handle else None


__all__ = ["COMPRESSED_SUFFIX", "DROPPED_SUFFIX", "KeptReadings", "blob_path", "decode_reading",
           "encode_reading", "file_type", "inflate", "read_dropped", "reading_key", "readings_of",
           "write_dropped",
           "CapturedText", "ContainerReading", "LIMIT_ERRORS", "MAX_CACHED_CHARS",
           "MEMBER_KEYS", "Member", "NotRemembered", "OCR_DEFERRED", "OCR_FAILED",
           "OCR_NO_TEXT",
           "OCR_PENDING", "OCR_UNAVAILABLE", "ReadingMemo", "SCANS_PENDING", "StoredText", "TEXT_EXCEEDS_BOUND", "TEXT_EXTRACTED",
           "TEXT_OCR", "TEXT_READ", "TEXT_UNREAD", "UNTYPED_OCR_FAILURES", "byte_pinned", "capture_text",
           "captured_text", "container_members", "kept_readings", "member_handle", "member_view",
           "ocr_digest", "ocr_failure_cause", "read_blob", "read_container", "read_failure",
           "read_text", "split_member", "stored_text", "text_status_kind"]
