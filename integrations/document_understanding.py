"""Source-cited extraction of text business documents into transaction evidence.

This module reads bytes supplied by an authorized caller. It performs no remote
fetches and stores no source bytes. Literal text spans are observations, while
line grouping is marked as a heuristic interpretation. :func:`read_pages` is the
one place that chooses how a PDF, DOCX or PPTX is read, for this module and the
use-case part accounting alike: a PDF that ``pypdf`` reads page by page has that
reading; else Portal's MarkItDown path (``doc_convert``) when the optional
``markitdown`` package is installed, labelled machine-converted with its
provenance, then poppler's text layer. A DOCX/PPTX the stdlib converter opens is
read by MarkItDown when installed, else by that converter in MarkItDown's shape
(``doc_convert.convert_stdlib``), labelled the same way. XLSX
keeps the Workbench cell transcription, with a span per sheet. An HTML file is its
visible text (:func:`html_text`, the one HTML reading; bounded by ``MAX_HTML_BYTES``)
and JSON is plain text as written. Outlook ``.msg``
messages are read like RFC822 ones: a message's text is its own headers and body
only. Its attachments, nested messages included (attached as a file or carried: a
``message/rfc822`` entity, an embedded Outlook item), are not read here: each is a
container member read on its own bytes and cited by its member handle, enumerated by
the one container walker (``operating.use_case_parts.members``), so no attachment is
read twice. :func:`mail_body` is the one rule for which MIME entities are the
message's own text and :func:`body_text` the one reading of such an entity. An
explicitly enabled local OCR path (macOS PDFKit/Vision, else poppler + tesseract
where installed) can read text layers and OCR image-only pages locally. OCR
identifiers remain review leads rather than automatically asserted joins. Numeric
confidence scores would imply calibration we do not have, so confidence is a
categorical grade.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from email import policy
import functools
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from hashlib import sha256
import io
from itertools import islice
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import statistics
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

from ontology import identifiers as _ids
from ontology.core import ValidationError
from ontology.units import ISO_4217, SIGN_CURRENCIES, currency_reading, currency_token_pattern
from . import child_process, doc_convert
from .xlsx_extract import extract_xlsx_text


EXTRACTOR_VERSION = "plx-document-understanding/v3"
MAX_BYTES = 20_000_000
MAX_TEXT = 100_000
MAX_PAGES = 200
MAX_CLAIMS = 300
MAX_LINE_ITEMS = 100
MAX_WARNINGS = 20


@dataclass(frozen=True)
class ExtractedDocument:
    mime_type: str
    source_sha256: str
    status: str  # extracted, no_text, unsupported
    method: str
    text: str
    claims: tuple[dict[str, Any], ...]
    line_items: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    pages: tuple[dict[str, int], ...] = ()
    # PDF pages whose text came from OCR rather than a text layer (1-based).
    ocr_pages: tuple[int, ...] = ()
    # Provenance of machine-converted text (``doc_convert.Conversion.record``):
    # set only when a converter (MarkItDown or the stdlib one) produced ``text``.
    conversion: Mapping[str, Any] | None = None
    # Workbook sheets in ``text``: {"sheet", "number", "start", "end"}.
    sheets: tuple[dict[str, Any], ...] = ()
    # Why a PDF/DOCX/PPTX was not read (``Reading.failure``: ``encrypted``,
    # ``pdf_parser_unavailable``, ``parser_panicked``, ``pdf_parse_error``,
    # ``conversion_failed``); its ``method`` is then ``none``.
    failure: str | None = None
    # The (first, last) pages of a PDF over ``MAX_PAGES`` left unread past the page
    # bound (pages 1-``MAX_PAGES`` are read), else None.
    skipped_pages: tuple[int, int] | None = None

    def evidence_attributes(self, original_url: str, *, title: str = "",
                            parent_id: str | None = None) -> dict[str, Any]:
        """Project extraction onto the existing SourceDocument evidence contract."""
        _https(original_url)
        identifiers = []
        quantities = []
        costs = []
        for claim in self.claims:
            span = claim["span"]
            provenance = _provenance(original_url, span["start"], span["end"])
            if claim["kind"] == "identifier":
                identifier = {"type": claim["field"], "value": claim["value"],
                              "provenance": provenance}
                if claim.get("confidence") == "review":
                    identifier.update({"confidence": "review", "interpretation": True})
                identifiers.append(identifier)
            elif (claim["kind"] == "quantity" and claim["unit"] not in NO_UNIT
                  and claim.get("confidence") != "review" and claim["value"] is not None):
                quantities.append({"value": claim["value"], "unit": claim["unit"],
                                   "context": claim["field"], "provenance": provenance})
            elif (claim["kind"] == "price" and claim["currency"] != "unknown"
                  and claim.get("confidence") != "review" and claim["value"] is not None):
                costs.append({"amount": claim["value"], "currency": claim["currency"],
                              "context": claim["field"], "provenance": provenance})
        # A filename is an identity lead, never a source-asserted AO number.
        # Keep it review-grade even when the text layer has no extractable text.
        title_ao = _ids.profile_pattern("document_title_ao").search(title)
        if title_ao and not any(item["type"] == "assembly_order" and
                                item["value"].casefold() == title_ao.group().casefold()
                                for item in identifiers):
            identifiers.append({"type": "assembly_order", "value": title_ao.group(),
                "provenance": f"{original_url}{'&' if '#' in original_url else '#'}documentTitle={title_ao.start()}-{title_ao.end()}",
                "confidence": "review", "interpretation": True})
        attrs: dict[str, Any] = {"title": title or "Source document", "text": self.text,
            "mime_type": self.mime_type, "original_url": original_url,
            "identifiers": identifiers, "quantities": quantities, "costs": costs,
            "extraction": {"version": EXTRACTOR_VERSION, "status": self.status,
                "method": self.method, "source_sha256": self.source_sha256,
                "warnings": list(self.warnings), "claims": list(self.claims),
                "line_items": list(self.line_items), "pages": list(self.pages)}}
        if self.conversion is not None:
            attrs["extraction"]["conversion"] = json.loads(json.dumps(self.conversion))
        if self.sheets:
            attrs["extraction"]["sheets"] = [dict(sheet) for sheet in self.sheets]
        if self.skipped_pages is not None:
            attrs["extraction"]["skipped_by_cap"] = {
                "first_page": self.skipped_pages[0], "last_page": self.skipped_pages[1],
                "cap": "pages", "limit": MAX_PAGES}
        if parent_id:
            attrs["parent_id"] = parent_id
        return attrs


def _https(url: str) -> None:
    if not isinstance(url, str):
        raise ValidationError("original source URL must be HTTPS")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValidationError("original source URL must be HTTPS")


def _provenance(url: str, start: int, end: int) -> str:
    return f"{url}{'&' if '#' in url else '#'}documentText={start}-{end}"


class _HTMLText(HTMLParser):
    """Visible text of an HTML document: script/style content is dropped and a
    block element's end is a line break and a table cell's end (td/th) is a tab, so
    adjacent blocks and cells never run together.

    Two sibling elements with no text between them (``</b><span>``, ``</span><span>``:
    a page that styles inline elements as blocks) are separated by one space, unless
    the first holds a word fragment (no space in its text, or a lone last character)
    that the second continues: a letter followed by a lowercase letter, or a digit by
    a digit
    (``<b>Re</b><i>lease</i>`` stays ``Release``). Text straight after an end tag
    (``<b>Re</b>lease``) is never split, and nothing is added before punctuation."""

    _BLOCKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
               "table", "ul", "ol", "blockquote", "pre", "section", "article",
               "dl", "dt", "dd", "nav", "header", "footer", "main", "aside", "figure",
               "figcaption", "caption", "hr", "details", "summary", "address",
               "fieldset", "legend", "form", "dialog", "hgroup", "thead", "tbody",
               "tfoot", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._after_end = False   # an end tag, with no text since
        self._sibling = False     # a start tag right after an end tag, with no text since

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._after_end:
            self._sibling = True
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
            return
        if not self._skip:
            self._after_end = True
        if tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        if self._sibling and self.parts:
            piece = self.parts[-1]
            before, after = piece[-1:], data[0]
            # A word continued: the first element's text is one fragment (no space in
            # it) or ends in a lone character after a space (``( M</em><em>ail``), and
            # the second goes on in the same class (lowercase letter, or digit).
            tail = piece.split()[-1:] or [""]
            continued = (len(tail[0]) == 1 or not any(c.isspace() for c in piece)) and (
                (before.isalpha() and after.isalpha() and after.islower())
                or (before.isdigit() and after.isdigit()))
            if before and not before.isspace() and after.isalnum() and not continued:
                self.parts.append(" ")
        self._after_end = self._sibling = False
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_text(source: str) -> str:
    """The visible text of an HTML document (:class:`_HTMLText`), runs of blank lines
    folded to one: the one HTML reading of a standalone file, a container member, a mail
    body part and a case original alike."""
    parser = _HTMLText()
    parser.feed(source)
    parser.close()
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n\n", parser.text()).strip()


# ------------------------------------------------------- the one PDF/Office reader

# What a reading records when MarkItDown converted a PDF but gave no page
# boundaries, so its pages came from the next reader in the order.
PAGE_FALLBACK = "markitdown_page_boundaries_unavailable"
# poppler's bounds, raised rather than read as missing text.
_BOUND_ERRORS = ("PDF page count exceeds bound", "extracted PDF text exceeds bound")


@dataclass(frozen=True)
class Page:
    number: int
    text: str
    method: str
    # Why this page's text was not read (``unreadable``, ``parser_panicked``), else None.
    unread: str | None = None


@dataclass(frozen=True)
class Reading:
    """One reading of a PDF, DOCX or PPTX (:func:`read_pages`).

    ``pages`` holds each PDF page's text where the reader gives page boundaries
    (``spans`` locate them in ``text``); ``method`` names the reader of the text and
    every page its own. ``failure`` says why nothing was read (``encrypted``,
    ``pdf_parser_unavailable``, ``parser_panicked``, ``pdf_parse_error``,
    ``conversion_failed``). ``skipped_pages`` is the (first, last) page range a
    ``first_pages`` reading (OCR included) left unread past its page bound, else None.
    ``document``
    is the open ``pypdf`` reader when pypdf opened the PDF, for a caller's
    structural walk; it never chooses the text.
    """
    source_sha256: str
    status: str  # extracted, no_text, unsupported
    method: str
    text: str = ""
    pages: tuple[Page, ...] = ()
    spans: tuple[dict[str, int], ...] = ()
    warnings: tuple[str, ...] = ()
    conversion: Mapping[str, Any] | None = None
    failure: str | None = None
    fallback: str | None = None  # PAGE_FALLBACK when the pages came from the next reader
    ocr_pages: frozenset[int] = frozenset()
    skipped_pages: tuple[int, int] | None = None
    document: Any = field(default=None, compare=False, repr=False)


def native_panic(exc: BaseException) -> bool:
    """A native extension's panic: pyo3 gives every extension its own
    ``pyo3_runtime.PanicException`` class, so it is known by name, never by class."""
    return any(cls.__name__ == "PanicException" and cls.__module__ == "pyo3_runtime"
               for cls in type(exc).__mro__)


def _failure(exc: BaseException, reason: str) -> str:
    """``parser_panicked`` for a native panic, ``reason`` for an ordinary error; an
    interrupt, exit or any other ``BaseException`` is re-raised."""
    if native_panic(exc):
        return "parser_panicked"
    if isinstance(exc, Exception):
        return reason
    raise exc


def _paged(source: str, method: str, pages: list[Page], *, warnings: tuple[str, ...] = (),
           max_text: int | None = None, document: Any = None,
           skipped_pages: tuple[int, int] | None = None) -> Reading:
    """One reading from per-page text: pages joined by one newline, a span per page."""
    parts: list[str] = []
    spans = []
    position = 0
    for page in pages:
        if parts:
            parts.append("\n")
            position += 1
        if max_text is not None and position + len(page.text) > max_text:
            raise ValidationError("extracted PDF text exceeds bound")
        spans.append({"page": page.number, "start": position, "end": position + len(page.text)})
        parts.append(page.text)
        position += len(page.text)
    text = "".join(parts)
    status = "extracted" if text.strip() else "no_text"
    if status == "no_text" and not any(page.unread for page in pages):
        warnings += ("image_only_or_empty_pdf_requires_ocr",)
    return Reading(source, status, method, text, tuple(pages), tuple(spans), warnings,
                   skipped_pages=skipped_pages, document=document)


class _PypdfRecords(logging.Handler):
    """pypdf's logging records while it reads (:func:`_pypdf_logged`): pypdf logs a
    malformed file's repairs as warnings and has no handler, so they would reach stderr
    through the last-resort handler. Each is named in the reading thread's warnings
    (``pypdf_log:<LEVEL>``) and passed to every :func:`pypdf_record_listener`, never
    printed; the message (a document's text may be in it) is not kept."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)   # the last-resort handler's level
        self.lock = threading.Lock()
        self.depth = 0
        self.propagate = True
        self.local = threading.local()
        self.listeners: list[Callable[[logging.LogRecord], None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        sink = getattr(self.local, "sink", None)
        if sink is not None:
            sink.add(f"{PYPDF_LOG}{record.levelname}")
        for listener in list(self.listeners):
            listener(record)


_PYPDF_RECORDS = _PypdfRecords()
PYPDF_LOG = "pypdf_log:"  # a reading's warning: pypdf logged a record at this level


@contextlib.contextmanager
def _pypdf_logged() -> Iterator[set[str]]:
    """While pypdf reads: its records go to ``_PYPDF_RECORDS`` only (the ``pypdf`` logger
    does not propagate; one handler for every reading thread), and the set yielded
    collects this thread's ``pypdf_log:<LEVEL>`` names."""
    handler, logger = _PYPDF_RECORDS, logging.getLogger("pypdf")
    with handler.lock:
        if handler.depth == 0:
            handler.propagate = logger.propagate
            logger.addHandler(handler)
            logger.propagate = False
        handler.depth += 1
    outer, sink = getattr(handler.local, "sink", None), set()
    handler.local.sink = sink
    try:
        yield sink
    finally:
        handler.local.sink = outer
        if outer is not None:
            outer.update(sink)
        with handler.lock:
            handler.depth -= 1
            if handler.depth == 0:
                logger.removeHandler(handler)
                logger.propagate = handler.propagate


@contextlib.contextmanager
def pypdf_record_listener(listener: Callable[[logging.LogRecord], None]) -> Iterator[None]:
    """``listener`` gets every pypdf logging record a reading keeps off stderr while the
    block runs (a CLI counts them by cause)."""
    _PYPDF_RECORDS.listeners.append(listener)
    try:
        yield
    finally:
        _PYPDF_RECORDS.listeners.remove(listener)


def _pypdf_open(content: bytes, max_pages: int | None
                ) -> tuple[Any, str | None, tuple[str, ...]]:
    """(reader, failure, warnings): pypdf's reader of ``content``, or why there is none.

    A PDF that opens with the empty user password is read (any viewer opens it); one
    that needs a password is ``encrypted``. Over ``max_pages`` raises before any read.
    """
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return None, "pdf_parser_unavailable", ("pdf_parser_unavailable_install_pypdf",)
    except BaseException as exc:
        # A broken native dependency (e.g. a pyo3 panic from `cryptography`) is an
        # environment defect, not a property of this document.
        return (None, _failure(exc, "pdf_parser_unavailable"),
                ("pdf_parser_broken_reinstall_pypdf",))
    with _pypdf_logged() as logged:
        try:
            reader = PdfReader(io.BytesIO(content), strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                return None, "encrypted", ("encrypted_pdf", *sorted(logged))
            count = len(reader.pages)
        except BaseException as exc:
            # Invalid PDFs must never silently appear as empty, parsed documents.
            return (None, _failure(exc, "pdf_parse_error"),
                    (f"pdf_parse_error:{type(exc).__name__}", *sorted(logged)))
    if max_pages is not None and count > max_pages:
        raise ValidationError("PDF page count exceeds bound")
    return reader, None, tuple(sorted(logged))


def _pypdf_pages(source: str, reader: Any, max_text: int | None,
                 first: int | None = None) -> Reading:
    """Each page's text by pypdf; with ``first`` only the first ``first`` pages, the
    rest the reading's ``skipped_pages``."""
    count = len(reader.pages)
    pages, warnings = [], ()
    with _pypdf_logged() as logged:
        for number, page in enumerate(islice(reader.pages, first), start=1):
            try:
                pages.append(Page(number, page.extract_text() or "", "pypdf-text"))
            except BaseException as exc:
                pages.append(Page(number, "", "pypdf-text", _failure(exc, "unreadable")))
                if f"pdf_parse_error:{type(exc).__name__}" not in warnings:
                    warnings += (f"pdf_parse_error:{type(exc).__name__}",)
    warnings += tuple(sorted(logged))
    return _paged(source, "pypdf-text", pages, warnings=warnings, max_text=max_text,
                  document=reader,
                  skipped_pages=(len(pages) + 1, count) if count > len(pages) else None)


def _pdftotext_pages(content: bytes, source: str, max_pages: int | None,
                     first_pages: bool) -> Reading | None:
    """Each page's text layer through local poppler, or None when poppler cannot run."""
    try:
        layers = local_pdf_text_layer(content, max_pages=max_pages, first_pages=first_pages)
    except (ValueError, OSError) as exc:  # ValidationError: missing or failed tools
        if str(exc) in _BOUND_ERRORS:
            raise
        return None
    if layers is None:
        return Reading(source, "unsupported", "none", warnings=("encrypted_pdf",),
                       failure="encrypted")
    read = [text for text in layers if text is not None]
    return _paged(source, "pdftotext-text",
                  [Page(number, text, "pdftotext-text") for number, text in enumerate(read, 1)],
                  skipped_pages=(len(read) + 1, len(layers)) if len(read) < len(layers) else None)


def _ocr_reading(content: bytes, source: str, max_pages: int | None,
                 first_pages: bool) -> Reading:
    """Local OCR's reading; with ``first_pages`` a PDF over the page bound is read on
    its first pages and the rest are the reading's ``skipped_pages``, as without OCR."""
    reader = _portable_pdf_text if _local_ocr_backend() == "portable" else _macos_pdf_text
    try:
        text, spans, status, warnings, ocr_pages, count = reader(
            content, max_pages=max_pages, first_pages=first_pages)
    except OcrFailed as error:
        if error.cause != "pdf_unreadable":
            raise
        # poppler refuses the file: pypdf, which is more forgiving of a damaged
        # structure, reads its text layer when it can; else the cause is typed by bytes.
        fallback = _pypdf_fallback(content, source, max_pages, first_pages)
        if fallback is None:
            raise OcrFailed(_unreadable_cause(content)) from error
        return fallback
    method = "poppler-tesseract" if reader is _portable_pdf_text else "macos-pdfkit-vision"
    failure = None
    if status == "unsupported":
        failure = "encrypted" if "encrypted_pdf" in warnings else "pdf_parse_error"
        method = "none"
    pages = tuple(Page(span["page"], text[span["start"]:span["end"]], method)
                  for span in spans)
    return Reading(source, status, method, text, pages, tuple(spans), tuple(warnings),
                   failure=failure, ocr_pages=frozenset(ocr_pages),
                   skipped_pages=((len(pages) + 1, count)
                                  if failure is None and count > len(pages) else None))



def _unreadable_cause(content: bytes) -> str:
    """The typed cause of a PDF poppler refused, by the bytes' shape alone."""
    if b"%PDF-" not in content[:1024]:
        return "not_a_pdf"
    if b"%%EOF" not in content[-1024:]:
        return "pdf_truncated"
    if b"stream" not in content:
        return "pdf_stub"   # whole, but no stream anywhere: no page can hold a mark
    return "pdf_unreadable"


def _pypdf_fallback(content: bytes, source: str, max_pages: int | None,
                    first_pages: bool) -> Reading | None:
    """pypdf's text-layer reading of a PDF poppler would not open (its pages by
    ``pypdf-text``, no OCR), or None when pypdf cannot read text from it either."""
    reader, _failure, opened = _pypdf_open(content, None if first_pages else max_pages)
    if reader is None:
        return None
    first = max_pages if (first_pages and max_pages is not None
                          and len(reader.pages) > max_pages) else None
    paged = _pypdf_pages(source, reader, None, first)
    if paged.failure is not None or not paged.text.strip() or any(
            page.unread for page in paged.pages):
        return None
    return replace(paged, warnings=tuple(dict.fromkeys(
        ("pdf_read_by_pypdf_after_poppler_refused", *opened, *paged.warnings))))


def _pdf_reading(content: bytes, *, ocr: bool, max_pages: int | None,
                 max_text: int | None, page_fallback: bool, first_pages: bool) -> Reading:
    source = sha256(content).hexdigest()
    if ocr:
        return _ocr_reading(content, source, max_pages, first_pages)
    reader, failure, opened = _pypdf_open(content, None if first_pages else max_pages)
    if failure == "encrypted":
        return Reading(source, "unsupported", "none", warnings=opened, failure=failure)
    # MarkItDown converts whole files only, so it runs only on a PDF whose page count
    # (pypdf's, else poppler's) is known and within the bound. One over it is read on
    # its first pages, page by page (pypdf, else poppler's text layer).
    backend = doc_convert.installed_backend()
    count = None
    if backend is not None or first_pages:
        count = (len(reader.pages) if reader is not None else
                 _poppler_page_count(content) if backend is not None else None)
    over = max_pages is not None and count is not None and count > max_pages
    if over and not first_pages:
        raise ValidationError("PDF page count exceeds bound")
    first = max_pages if over else None
    # A PDF pypdf reads, every page and some text, has the built-in reading whether or
    # not MarkItDown is installed: pdfminer puts a blank line between lines spaced 1.5x
    # their font or more, which ends a table, so its text would lose the line items.
    paged = _pypdf_pages(source, reader, max_text, first) if reader is not None else None
    if (paged is not None and paged.failure is None and paged.text.strip() and
            not any(page.unread for page in paged.pages)):
        return replace(paged, warnings=tuple(dict.fromkeys(opened + paged.warnings)))
    # pypdf's records stay named whichever reader gives the text.
    logged = tuple(sorted({w for w in opened + (paged.warnings if paged else ())
                           if w.startswith(PYPDF_LOG)}))
    converted, notes = None, ()
    bounded_out = False  # MarkItDown would have run but for the page bound
    if backend is not None and over:
        bounded_out, backend = True, None
    elif backend is not None and count is None and max_pages is not None:
        notes, backend = ("pdf_page_count_unknown_not_converted",), None
    if backend is not None:
        try:
            converted = doc_convert.convert(content, "pdf", backend=backend,
                                            page_count=count)
        except doc_convert.ConversionFailed as error:
            notes = (str(error)[:200],)
    if converted is not None and converted.spans:
        text = converted.text
        spans = tuple({"page": span["number"], "start": span["start"], "end": span["end"]}
                      for span in converted.spans)
        pages = tuple(Page(span["page"], text[span["start"]:span["end"]], "markitdown")
                      for span in spans)
        status = "extracted" if text.strip() else "no_text"
        warnings = () if text.strip() else ("image_only_or_empty_pdf_requires_ocr",)
        return Reading(source, status, "markitdown", text, pages, spans, warnings + logged,
                       converted.record(), document=reader)
    # The next reader, page by page: pypdf's partial or empty reading, else poppler's
    # text layer when pypdf is missing or panicked (a PDF pypdf finds malformed is not
    # retried). After a conversion only with ``page_fallback``: else MarkItDown is the
    # one reader.
    if converted is not None and not page_fallback:
        paged = None
    elif reader is None and failure in ("pdf_parser_unavailable", "parser_panicked"):
        paged = _pdftotext_pages(content, source, max_pages, first_pages)
    if converted is None:
        if paged is None and bounded_out:  # no page-by-page reader read its first pages
            raise ValidationError("PDF page count exceeds bound")
        if paged is None:
            return Reading(source, "unsupported", "none", warnings=notes + opened,
                           failure=failure)
        # poppler read it: why pypdf did not stays visible.
        return replace(paged, warnings=notes + opened + paged.warnings)
    if (paged is not None and paged.failure is None and paged.text.strip() and
            not any(page.unread for page in paged.pages)):
        return replace(paged, warnings=tuple(dict.fromkeys(
            (PAGE_FALLBACK, *paged.warnings, *logged))), fallback=PAGE_FALLBACK)
    # No reader gives this PDF's pages: MarkItDown's text, unpaged, never guessed spans.
    # Without a page count (only an unbounded reading converts then) no spans exist.
    text = converted.text
    warnings = (("pdf_page_count_unknown_page_spans_omitted",) if count is None
                else ("converted_page_spans_unavailable",))
    if not text.strip():
        warnings += ("image_only_or_empty_pdf_requires_ocr",)
    return Reading(source, "extracted" if text.strip() else "no_text", "markitdown", text,
                   warnings=warnings + logged, conversion=converted.record(),
                   document=reader)


def _office_reading(content: bytes, kind: str) -> Reading:
    """The stdlib converter opens the file first: one it refuses (not the format,
    encrypted, malformed, over a bound) is unread with its reason whether or not
    MarkItDown is installed, as MarkItDown's fallback converters read any bytes as
    text. Then Portal's MarkItDown path when installed, else (or when it fails) the
    stdlib converter's text, keeping MarkItDown's error as a warning."""
    source = sha256(content).hexdigest()
    try:
        builtin = doc_convert.convert_stdlib(content, kind)
    except doc_convert.ConversionFailed as error:
        return Reading(source, "unsupported", "none", warnings=(str(error)[:200],),
                       failure="conversion_failed")
    backend = doc_convert.installed_backend()
    failed: tuple[str, ...] = ()
    result = None
    if backend is not None:
        try:
            result = doc_convert.convert(content, kind, backend=backend,
                                        builtin=builtin)
        except doc_convert.ConversionFailed as error:
            failed = (str(error)[:200],)
    if result is None:
        result = builtin
    warnings = failed + result.warnings
    if not result.text.strip():
        warnings += ("converted_document_has_no_text",)
    return Reading(source, "extracted" if result.text.strip() else "no_text",
                   result.converter, result.text, warnings=warnings,
                   conversion=result.record())


def read_pages(content: bytes, kind: str, *, ocr: bool = False,
               max_pages: int | None = MAX_PAGES,
               max_text: int | None = MAX_TEXT, page_fallback: bool = True,
               first_pages: bool = False) -> Reading:
    """The one reading of a PDF (``kind`` ``pdf``), DOCX or PPTX: the only place that
    chooses the reader.

    A PDF is read in one order: explicitly enabled local OCR (``ocr``, Workbench
    work); else ``pypdf`` page by page when it reads every page and some text (the
    same reading with or without MarkItDown: pdfminer, MarkItDown's PDF reader, puts
    a blank line between lines spaced 1.5x their font or more, which ends a table);
    else Portal's MarkItDown (``doc_convert``) when it is installed, its pages taken
    from its form feeds when they match the PDF's page count; else pypdf's partial or
    empty reading, or, when pypdf is missing or panics, poppler's text layer
    (``local_pdf_text_layer``). When MarkItDown converts but gives no page
    boundaries, the pages come from the next reader when it read text, and the
    reading records ``PAGE_FALLBACK``; only when no reader gives pages, or
    ``page_fallback`` is false (MarkItDown read the whole PDF; no second reader reads
    it again), is MarkItDown's text kept unpaged. A DOCX/PPTX the stdlib converter opens is read by MarkItDown,
    else by that converter; one it refuses is unread with its reason either way.
    Every reading carries the original's SHA-256 and its method; a converted one the
    converter's record. ``max_pages``/``max_text`` (None: unbounded) raise
    ``ValidationError`` before a page is read; poppler keeps its own bounds as well.
    With ``first_pages`` (a positive ``max_pages``) a PDF over ``max_pages`` is read
    on its first ``max_pages`` pages instead, page by page (OCR; else pypdf, else
    poppler's text layer), and the reading's ``skipped_pages`` names the rest; when
    no page-by-page reader can read it, it is over the bound as without
    ``first_pages``. MarkItDown converts whole files only: it runs only when the
    PDF's page count (pypdf's, else poppler's ``pdfinfo``) is known and within
    ``max_pages``; with an unknown count it does not run
    (``pdf_page_count_unknown_not_converted``) unless ``max_pages`` is None.
    """
    if first_pages and (max_pages is None or max_pages < 1):
        raise ValueError("first_pages_requires_a_page_bound")
    if kind == "pdf":
        return _pdf_reading(content, ocr=ocr, max_pages=max_pages, max_text=max_text,
                            page_fallback=page_fallback, first_pages=first_pages)
    if kind in ("docx", "pptx"):
        return _office_reading(content, kind)
    raise ValueError("unsupported_reading_kind")


def _macos_pdf_text(content: bytes, *, max_pages: int | None = None,
                    first_pages: bool = False
                    ) -> tuple[str, tuple[dict[str, int], ...],
                               str, tuple[str, ...], frozenset[int], int]:
    """Read one bounded PDF through local PDFKit/Vision with private temp bytes.

    This path is opt-in. A failed OCR run raises rather than committing a Graph
    cursor with silently missing image text. The Swift process never receives
    credentials or a source URL. The helper reads at most the page bound
    (``MAX_PAGES``, or a lower ``max_pages``) and reports the PDF's page count,
    returned last as in ``_portable_pdf_text``: a PDF over the bound raises
    ``PDF page count exceeds bound`` unless ``first_pages``, which keeps the pages read.
    """
    bound = MAX_PAGES if max_pages is None else min(max_pages, MAX_PAGES)
    if platform.system() != "Darwin" or not Path("/usr/bin/swift").is_file():
        raise ValidationError("local_pdf_ocr_requires_macos_swift")
    cache = Path(tempfile.gettempdir()) / f"plx-pdf-ocr-cache-{os.geteuid()}"
    cache.mkdir(mode=0o700, exist_ok=True)
    info = cache.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValidationError("local_pdf_ocr_cache_untrusted")
    cache.chmod(0o700)
    helper = Path(__file__).with_name("macos_pdf_ocr.swift")
    with child_process.private_directory("plx-pdf-ocr-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(content)
        path.chmod(0o600)
        env = dict(os.environ)
        env["SWIFT_MODULECACHE_PATH"] = str(cache / "swift")
        env["CLANG_MODULE_CACHE_PATH"] = str(cache / "clang")
        try:
            result = child_process.run(["/usr/bin/swift", str(helper), str(path), str(bound)],
                                       timeout=120, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValidationError("local_pdf_ocr_unavailable") from error
    if result.returncode != 0:
        code = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        if code[0] == "pdf_text_limit_exceeded":
            raise ValidationError("extracted PDF text exceeds bound")
        raise OcrFailed("tool_signal" if result.returncode < 0
                        else _MACOS_HELPER_CAUSE.get(code[0], "tool_exit"))
    if len(result.stdout) > 1_000_000:
        raise OcrFailed("output_bound")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError("local_pdf_ocr_result_invalid") from error
    rows = payload.get("pages") if isinstance(payload, dict) else None
    count = payload.get("page_count") if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or type(count) is not int or
            not 1 <= len(rows) <= bound or len(rows) != min(count, bound)):
        raise ValidationError("local_pdf_ocr_pages_invalid")
    if count > bound and not first_pages:
        raise ValidationError("PDF page count exceeds bound")
    parts: list[str] = []
    pages: list[dict[str, int]] = []
    ocr_pages: set[int] = set()
    position = 0
    for index, row in enumerate(rows, start=1):
        if (not isinstance(row, dict) or set(row) != {"page", "text", "method"} or
                type(row["page"]) is not int or row["page"] != index or
                not isinstance(row["text"], str) or
                row["method"] not in {"pdfkit-text", "vision-ocr"}):
            raise ValidationError("local_pdf_ocr_page_invalid")
        page_text = row["text"]
        if position + len(page_text) > MAX_TEXT:
            raise ValidationError("extracted PDF text exceeds bound")
        parts.append(page_text)
        pages.append({"page": index, "start": position,
                      "end": position + len(page_text)})
        if row["method"] == "vision-ocr":
            ocr_pages.add(index)
        position += len(page_text)
        if index < len(rows):
            parts.append("\n")
            position += 1
    text = "".join(parts)
    status = "extracted" if text.strip() else "no_text"
    warnings = (("ocr_text_requires_identity_review",) if ocr_pages else ())
    return text, tuple(pages), status, warnings, frozenset(ocr_pages), count


_PORTABLE_OCR_TOOLS = ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")
MAX_OCR_PAGES = 40  # the same image-page bound as the macOS helper
_MAX_PAGE_POINTS = 20_000  # the macOS helper's page-size bound
_PAGE_RETRY_CAUSES = ("render_exit", "recognize_exit", "tool_signal")
_MAX_RENDER_PIXELS = 3_300  # longest rendered side: letter at 300 dpi
_OCR_DEADLINE_SECONDS = 300.0  # the whole document, all tool calls together


def _local_ocr_backend() -> str:
    """``macos`` on a host with the Swift helper, else ``portable`` when
    poppler and tesseract are installed, else ``macos`` (which then refuses
    with ``local_pdf_ocr_requires_macos_swift`` as before)."""
    if platform.system() == "Darwin" and Path("/usr/bin/swift").is_file():
        return "macos"
    if all(shutil.which(tool) for tool in _PORTABLE_OCR_TOOLS):
        return "portable"
    return "macos"


def local_ocr_available() -> bool:
    """Whether this host can OCR image-only PDF pages locally: the macOS helper, or
    poppler + tesseract on the PATH. Never a network service."""
    if platform.system() == "Darwin" and Path("/usr/bin/swift").is_file():
        return True
    return all(shutil.which(tool) for tool in _PORTABLE_OCR_TOOLS)


def local_image_ocr_available() -> bool:
    """Whether ``local_image_text`` can run: tesseract on the PATH."""
    return shutil.which("tesseract") is not None


def local_image_text(content: bytes, *, timeout: float = 60.0) -> str:
    """OCR text of one raster image (PNG, JPEG, GIF, TIFF, BMP, WebP) by local tesseract,
    under the same envelope as the PDF OCR path: a private temporary file, a minimal
    environment (never credentials), a time bound and 1 MB of output. Machine-read text;
    any failure raises ``local_image_ocr_failed`` (or ``_unavailable``). Interword spaces
    are kept as on the PDF path, so a form's column gaps stay two or more spaces."""
    tool = shutil.which("tesseract")
    if tool is None:
        raise ValidationError("local_image_ocr_unavailable")
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX", "HOME", "TMPDIR"}}
    env["OMP_THREAD_LIMIT"] = "1"
    with child_process.private_directory("plx-image-ocr-") as directory:
        path = Path(directory) / "image"
        path.write_bytes(content)
        path.chmod(0o600)
        try:
            result = child_process.run([tool, str(path), "stdout", "--psm", "3",
                                        "-c", "preserve_interword_spaces=1"],
                                       timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValidationError("local_image_ocr_failed") from error
    if result.returncode != 0 or len(result.stdout) > 1_000_000:
        raise ValidationError("local_image_ocr_failed")
    return result.stdout.decode("utf-8", "replace").strip()


def _only_line(output: str, pattern: str) -> re.Match[str]:
    """The single line matching ``pattern``; metadata cannot forge a second."""
    found = list(re.finditer(pattern, output, re.M))
    if len(found) != 1:
        raise OcrFailed("page_count")
    return found[0]


def _png_size(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError as error:
        raise OcrFailed("render_mismatch") from error
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise OcrFailed("render_mismatch")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


# Why a local OCR run failed (:class:`OcrFailed`): a tool call past the document's
# deadline; a PDF poppler refused and pypdf cannot read, typed by its bytes
# (:func:`_unreadable_cause`: ``not_a_pdf`` no ``%PDF-`` header in the first KB, empty
# included; ``pdf_truncated`` a header but no ``%%EOF`` in the last KB; ``pdf_stub``
# both but no ``stream`` anywhere, so no page content, image or font: a placeholder);
# a tool that exited non-zero, said by tool (``pdf_unreadable``: pdfinfo could
# not open the file or found no page in it, damaged or truncated or a stub or not a PDF;
# :func:`_unreadable_cause` types it by the bytes: ``not_a_pdf`` (no ``%PDF-`` header
# in the first KB: an HTML page, an image), ``pdf_truncated`` (a header but no ``%%EOF``
# in the last KB), ``pdf_stub`` (whole but no ``stream``), else ``pdf_unreadable``;
# ``pdfinfo_exit``;
# ``text_layer_exit``: pdftotext; ``render_exit``: pdftoppm; ``recognize_exit``:
# tesseract) or ``tool_signal`` (a tool killed by a signal, out of memory); ``tool_exit``
# any other helper's non-zero exit (the macOS helper's unrecognised code); a tool's
# output over its 1 MB, a render of another size than its page (or none), more
# image-only pages than ``MAX_OCR_PAGES``, a page outside the page-size bound, a page
# count or page listing poppler did not give consistently, word boxes that are not
# tesseract's TSV. The capture keeps it in its text status
# (``integrations.captured_text``): one file's failure is its own typed result, never
# the run's.
OCR_FAILURE_CAUSES = ("deadline", "tool_exit", "pdf_unreadable", "pdfinfo_exit",
                      "not_a_pdf", "pdf_truncated", "pdf_stub",
                      "text_layer_exit", "render_exit", "recognize_exit", "tool_signal",
                      "output_bound", "render_mismatch", "image_page_bound", "page_bound",
                      "page_count", "ocr_output_invalid")
_TOOL_EXIT_CAUSE = {"pdftotext": "text_layer_exit", "pdftoppm": "render_exit",
                    "tesseract": "recognize_exit"}


def _tool_exit_cause(tool: str, code: int, *, opening: bool = False) -> str:
    """The typed cause of a poppler/tesseract call that exited ``code``: a negative code
    is a signal (``tool_signal``); pdfinfo's exit 1 (cannot open) or 99 (opened, but no
    page: "Invalid page count 0", a stub of a few bytes) on opening the file is a file
    poppler cannot read (``pdf_unreadable``); every other exit names its tool."""
    if code < 0:
        return "tool_signal"
    if tool == "pdfinfo":
        return "pdf_unreadable" if opening and code in (1, 99) else "pdfinfo_exit"
    return _TOOL_EXIT_CAUSE.get(tool, "tool_exit")


# The Swift helper's stderr code (``macos_pdf_ocr.swift``, exit 2) and the cause it is.
_MACOS_HELPER_CAUSE = {"ocr_page_limit_exceeded": "image_page_bound",
                       "pdf_page_dimensions_invalid": "page_bound",
                       "pdf_page_render_failed": "render_exit",
                       "vision_ocr_failed": "recognize_exit",
                       "pdf_unavailable_or_encrypted": "pdf_unreadable",
                       "pdf_page_count_out_of_bounds": "page_count",
                       "pdf_page_unavailable": "render_exit"}


class OcrFailed(ValidationError):
    """``local_pdf_ocr_failed`` with its typed ``cause`` (``OCR_FAILURE_CAUSES``); any
    other cause is refused, so no OCR fault leaves untyped."""

    def __init__(self, cause: str) -> None:
        if cause not in OCR_FAILURE_CAUSES:
            raise ValueError("ocr_failure_cause_unknown")
        super().__init__("local_pdf_ocr_failed")
        self.cause = cause


class _UserPassword(Exception):
    """poppler cannot open the PDF without its user password."""


def local_pdf_text_layer(content: bytes, *, max_pages: int | None = None,
                         first_pages: bool = False) -> tuple[str | None, ...] | None:
    """The text layer of each page of one bounded PDF through local poppler
    (``pdftotext``), with no OCR and no ``pypdf``: the text-layer pass of
    ``_portable_pdf_text`` under the same bounds and envelope. A page without a
    text layer is ``""``; a PDF poppler cannot open without its user password is
    None. Needs only ``pdfinfo`` and ``pdftotext``; a PDF over a bound (``MAX_PAGES``
    or a lower ``max_pages``; ``MAX_TEXT``) raises that bound's error (``PDF page count
    exceeds bound`` before any text is read, ``extracted PDF text exceeds bound``), any
    other failure ``local_pdf_ocr_failed`` or ``_unavailable``. With ``first_pages`` a
    PDF over the page bound gives its first pages' text and None for each page past
    the bound (not read)."""
    text, pages, status, _, _, count = _portable_pdf_text(
        content, ocr=False, max_pages=max_pages, first_pages=first_pages)
    if status == "unsupported":
        return None
    layers = tuple(text[page["start"]:page["end"]] for page in pages)
    return layers + (None,) * (count - len(layers))


def pdf_page_count(content: bytes) -> int | None:
    """A PDF's page count, no page read: pypdf's, else poppler's ``pdfinfo``; None
    when neither can count it."""
    reader, _failure, _warnings = _pypdf_open(content, None)
    return len(reader.pages) if reader is not None else _poppler_page_count(content)


def _tool_env() -> dict[str, str]:
    """The minimal environment of a local poppler/tesseract call: no credentials."""
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX", "HOME", "TMPDIR"}}
    env["OMP_THREAD_LIMIT"] = "1"
    return env


def _poppler_page_count(content: bytes) -> int | None:
    """The PDF's page count by local poppler's ``pdfinfo`` (no page is read), or None
    when poppler is missing, fails or cannot open the PDF without a password."""
    tool = shutil.which("pdfinfo")
    if tool is None:
        return None
    with child_process.private_directory("plx-pdf-count-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(content)
        path.chmod(0o600)
        try:
            result = child_process.run([tool, str(path)], timeout=60, env=_tool_env())
        except (OSError, subprocess.TimeoutExpired):
            return None
    if result.returncode != 0 or len(result.stdout) > 1_000_000:
        return None
    try:
        return int(_only_line(result.stdout.decode("utf-8", "replace"),
                              r"^Pages:\s+(\d+)\s*$").group(1))
    except ValidationError:
        return None


_TSV_HEADER = ("level", "page_num", "block_num", "par_num", "line_num", "word_num", "left",
               "top", "width", "height", "conf", "text")
_TSV_WORD = 5   # tesseract's level of a word box
# The last line of a page whose text :func:`ocr_layout_text` rebuilt from word boxes, so
# the pinned OCR text itself says its character columns are the scan's (a reader may
# then place a cell by its columns: ``ontology.document_fields``). OCR text without it
# (pinned before word boxes, or another engine's) has no such columns; a text layer
# never carries it (removed from every page not rebuilt here).
LAYOUT_MARK = "[ocr word-box layout]"
# The word confidence (tesseract's TSV ``conf``, 0-100) below which a word box may be
# handwriting OCR could not read: on the AO shop-floor scans 95% of printed form words
# read at or above 91, while most of the text in a handwritten box reads below it (a
# stray glyph in handwriting may still read high: ``ontology.document_fields`` decides a
# box by its share of such text; decision
# devloop/DECISIONS/2026-10-19-ao-handwritten-reader-limit.md). The mark line names the
# spans of such words on its page: '[ocr word-box layout] below 85: 120-125 300-307'.
HANDWRITING_CONFIDENCE = 85
_LAYOUT_LINE = re.compile(r"\n" + re.escape(LAYOUT_MARK) +
                          r"(?: below (\d+): (\d+-\d+(?: \d+-\d+)*))?[ \t\r\n]*\Z")


def layout_page(text: str) -> bool:
    """Whether a page's text is word-box layout text (its last line is the mark line)."""
    return _LAYOUT_LINE.search(text) is not None


def layout_confidence(text: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """A layout page's text with its mark line bare (``LAYOUT_MARK``; offsets before it
    unchanged) and the spans of its words OCR read below ``HANDWRITING_CONFIDENCE``. A
    mark line naming another bound, or a span that is not an ordered span of the text
    above it, names none: never a word taken for handwriting on doubt."""
    m = _LAYOUT_LINE.search(text)
    if m is None:
        return text, ()
    bare = text[:m.start()] + "\n" + LAYOUT_MARK
    if m[1] is None or int(m[1]) != HANDWRITING_CONFIDENCE:
        return bare, ()
    spans = tuple(tuple(int(n) for n in pair.split("-")) for pair in m[2].split())
    at = 0
    for start, end in spans:
        if not at <= start < end <= m.start():
            return bare, ()
        at = end
    return bare, spans


def _tsv_words(tsv: str) -> list[tuple[int, int, int, int, str, float]]:
    """The word boxes of tesseract's TSV (top, bottom, left, right, text, confidence); a
    malformed TSV (another header, a short row, a non-integer box or a non-numeric or
    out-of-range word confidence) raises ``local_pdf_ocr_failed``, never a partial page."""
    rows = tsv.splitlines()
    if not rows or tuple(rows[0].split("\t")) != _TSV_HEADER:
        raise OcrFailed("ocr_output_invalid")
    words = []
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) != len(_TSV_HEADER):
            raise OcrFailed("ocr_output_invalid")
        try:
            level, left, top, width, height = (int(cells[i]) for i in (0, 6, 7, 8, 9))
            conf = float(cells[10])
        except ValueError as error:
            raise OcrFailed("ocr_output_invalid") from error
        text = cells[11].strip()
        if level == _TSV_WORD and text and width > 0 and height > 0:
            if not 0 <= conf <= 100:
                raise OcrFailed("ocr_output_invalid")
            words.append((top, top + height, left, left + width, text, conf))
    return words


def ocr_layout_text(tsv: str) -> str:
    """One page's OCR text rebuilt from tesseract's TSV word boxes (the PDF path's one OCR
    call, ``tsv``): words whose boxes overlap vertically by half the smaller height are one
    line, so a handwritten cell written off its row's baseline, which tesseract puts in a
    block of its own, rejoins its row; lines run top to bottom. A word starts at the
    character column of its left edge (one column per median character width, from the
    page's leftmost word), so a cell sits under its column's header and a table cell is
    read by its x-overlap with the header cell (``ontology.document_fields``). Words
    closer than two character widths keep one space, a wider gap keeps two or more (a
    column gap); a vertical gap of more than ``2.5`` median word heights is a blank line.
    The text ends in its mark line (``LAYOUT_MARK``, naming the spans of the words read
    below ``HANDWRITING_CONFIDENCE``, if any); a page with no word is ''. Machine-read
    layout text, pinned as the page's OCR text, so citations resolve against it. A
    malformed TSV raises ``local_pdf_ocr_failed`` (:func:`_tsv_words`), never a partial
    page."""
    words = _tsv_words(tsv)
    if not words:
        return ""
    char = max(1.0, statistics.median((w[3] - w[2]) / len(w[4]) for w in words))
    tall = statistics.median(w[1] - w[0] for w in words)
    lines: list[list[tuple[int, int, int, int, str, float]]] = []
    for word in sorted(words, key=lambda w: ((w[0] + w[1]) / 2, w[2])):
        if lines:
            line = lines[-1]
            top = statistics.median(w[0] for w in line)
            bottom = statistics.median(w[1] for w in line)
            if min(word[1], bottom) - max(word[0], top) >= \
                    0.5 * min(word[1] - word[0], bottom - top):
                line.append(word)
                continue
        lines.append([word])
    origin = min(w[2] for w in words)
    out: list[str] = []
    low: list[tuple[int, int]] = []   # spans of the words read below the bound
    at = 0   # the page offset of the line being built
    below = None
    for line in lines:
        top = min(w[0] for w in line)
        if below is not None and top - below > 2.5 * tall:
            out.append("")
            at += 1
        below = max(w[1] for w in line)
        text, right, run = "", None, False   # run: the line's last word was read below
        for top, bottom, left, end, word, conf in sorted(line, key=lambda w: w[2]):
            column = round((left - origin) / char)
            if right is not None and left - right < 2 * char:
                text += " "
            elif text:
                text += " " * max(2, column - len(text))
            else:
                text = " " * column
            if conf < HANDWRITING_CONFIDENCE:   # a run of such words on a line is one span
                start = low.pop()[0] if run else at + len(text)
                low.append((start, at + len(text) + len(word)))
            run = conf < HANDWRITING_CONFIDENCE
            text += word
            right = end
        out.append(text.rstrip())
        at += len(out[-1]) + 1
    page = "\n".join(out).rstrip()
    if not page.strip():
        return ""
    mark = LAYOUT_MARK
    if low:
        mark += f" below {HANDWRITING_CONFIDENCE}: " + " ".join(f"{a}-{b}" for a, b in low)
    return page + "\n" + mark


def _turned_pgm(path: Path, degrees: int, size: tuple[int, int]) -> Path:
    """``path``, an 8-bit greyscale PGM render of ``size`` (width, height), turned
    ``degrees`` (90, 180 or 270) clockwise into a PGM beside it; a render of another
    size or format raises ``local_pdf_ocr_failed`` (``render_mismatch``)."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise OcrFailed("render_mismatch") from error
    width, height = size
    header = b"P5\n%d %d\n255\n" % size   # pdftoppm's header for this render
    pixels = data[len(header):]
    if not data.startswith(header) or len(pixels) != width * height:
        raise OcrFailed("render_mismatch")
    if degrees == 180:
        shape, turned = (width, height), pixels[::-1]
    elif degrees == 90:   # each row of the turned page is a column read bottom to top
        shape, turned = (height, width), b"".join(pixels[c::width][::-1]
                                                  for c in range(width))
    else:                 # 270: a column read top to bottom, the last column first
        shape, turned = (height, width), b"".join(pixels[c::width]
                                                  for c in reversed(range(width)))
    out = path.with_name(path.stem + "-turned.pgm")
    out.write_bytes(b"P5\n%d %d\n255\n" % shape + turned)
    return out


def _portable_pdf_text(content: bytes, *, ocr: bool = True, max_pages: int | None = None,
                       first_pages: bool = False
                       ) -> tuple[str, tuple[dict[str, int], ...],
                                  str, tuple[str, ...], frozenset[int], int]:
    """Read one bounded PDF through local poppler + tesseract, page by page.

    Same contract as ``_macos_pdf_text``, plus the PDF's page count (0 when it is
    not opened): a page with a text layer keeps it;
    an image-only page is rendered (at most 300 dpi and 3300 px on its
    longest side, greyscale) and OCRed, and its range is reported so
    identifiers from it stay review leads; its text is rebuilt from tesseract's word
    boxes (:func:`ocr_layout_text`), so a table's column gaps survive as runs of spaces
    and a cell keeps its column. Renders use the crop box, the
    size pdfinfo reports. Bounds: 200 pages, pages up to
    20000 pt, 40 OCR pages, 1 MB of output per tool call, 300 s for the whole
    document. Each scanned page first gets tesseract's orientation pass (OSD): a page
    it says is turned (a scan fed in upside down or on its side) is read again turned
    upright, and that reading is kept unless the words OCR read as rendered are the
    more confident (summed word confidence), so a wrong turn never garbles a page that
    read as rendered; a page OSD gives no answer
    for (too little text, no ``osd`` model on the host) is read as rendered. The tools
    get a private temporary PDF and a minimal
    environment, never credentials or a source URL. Every failure (a tool
    error, a forged or inconsistent page count, a missing or wrong-sized
    render, a bound exceeded) raises ``local_pdf_ocr_failed`` (:class:`OcrFailed`, its
    typed cause) or
    ``local_pdf_ocr_unavailable`` rather than returning missing page text; the
    two bounds the pypdf path shares raise its errors (more than ``MAX_PAGES``
    pages; more than ``MAX_TEXT`` characters, or a ``pdftotext`` output over its
    1 MB; ``max_pages`` lowers the page bound; with ``first_pages`` a PDF over the page
    bound is read on its first pages instead). A PDF poppler cannot open without its
    user password is unsupported, as on the pypdf path; an owner password alone
    (restricting editing or printing, not reading) never stops a read. With ``ocr`` false
    only the text layer is read (``pdfinfo`` and ``pdftotext``): an image-only
    page keeps its empty text and nothing is rendered.
    """
    tools = {tool: shutil.which(tool)
             for tool in (_PORTABLE_OCR_TOOLS if ocr else ("pdfinfo", "pdftotext"))}
    if not all(tools.values()):
        raise ValidationError("local_pdf_ocr_unavailable")
    env = _tool_env()
    deadline = time.monotonic() + _OCR_DEADLINE_SECONDS

    def run(command: list[str], *, opening: bool = False) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OcrFailed("deadline")
        try:
            result = child_process.run(command, timeout=remaining, env=env)
        except subprocess.TimeoutExpired as error:
            raise OcrFailed("deadline") from error
        except OSError as error:
            raise ValidationError("local_pdf_ocr_unavailable") from error
        # poppler's "error opening the PDF" (exit 1) for a user password it was not
        # given; keyed on the word, not on one release's wording of the message.
        if opening and result.returncode == 1 and b"password" in result.stderr.lower():
            raise _UserPassword
        if result.returncode != 0:
            raise OcrFailed(_tool_exit_cause(Path(command[0]).name, result.returncode,
                                             opening=opening))
        if len(result.stdout) > 1_000_000:
            if command[0] == tools["pdftotext"]:
                raise ValidationError("extracted PDF text exceeds bound")
            raise OcrFailed("output_bound")
        return result.stdout.decode("utf-8", "replace")

    def turn(image: Path) -> int:
        """The degrees OSD says to turn ``image`` clockwise; 0 when it gives no answer."""
        try:
            said = re.findall(r"^Rotate:\s+(\d+)\s*$", run(
                [tools["tesseract"], str(image), "stdout", "--psm", "0"]), re.M)
        except OcrFailed as error:
            if error.cause != "recognize_exit":   # a signal or deadline is not "no answer"
                raise
            return 0
        return int(said[0]) if len(said) == 1 and said[0] in ("90", "180", "270") else 0

    def words(image: Path) -> str:
        return run([tools["tesseract"], str(image), "stdout", "--psm", "3", "tsv"])

    with child_process.private_directory("plx-pdf-ocr-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(content)
        path.chmod(0o600)

        def ocr_page(index: int, dpi: int, size_pts: tuple[float, float]) -> str:
            """One page rendered at ``dpi`` and read: its layout text (``ocr_layout_text``)."""
            width, height = size_pts
            number = str(index)
            image = Path(directory) / f"page-{index}"
            run([tools["pdftoppm"], "-q", "-cropbox", "-r", str(dpi), "-gray", "-png",
                 "-singlefile", "-f", number, "-l", number, str(path), str(image)])
            rendered = image.with_suffix(".png")
            # pdftoppm can exit 0 after a failed allocation; check the render.
            size = _png_size(rendered)
            expected = (width * dpi / 72, height * dpi / 72)
            if not any(all(abs(got - want) <= 2 for got, want in zip(size, shape))
                       for shape in (expected, expected[::-1])):  # /Rotate swaps
                raise OcrFailed("render_mismatch")
            # Word boxes, rebuilt into lines and columns (ocr_layout_text): a form's
            # column gaps stay two or more spaces and a cell under its header.
            # A layout page keeps its first line's indent (it is that line's column)
            # and ends in the mark line that says so.
            tsv, degrees = words(rendered), turn(rendered)
            if degrees:   # the same render as raw greyscale, turned upright
                run([tools["pdftoppm"], "-q", "-cropbox", "-r", str(dpi), "-gray",
                     "-singlefile", "-f", number, "-l", number, str(path), str(image)])
                turned = _turned_pgm(image.with_suffix(".pgm"), degrees, size)
                upright = words(turned)
                # A page on its side reads as confidently as rendered, but its word
                # boxes run down the page: a tie is the turned reading's.
                if sum(w[5] for w in _tsv_words(upright)) >= \
                        sum(w[5] for w in _tsv_words(tsv)):
                    tsv = upright
                for left in (image.with_suffix(".pgm"), turned):
                    left.unlink(missing_ok=True)
            page = ocr_layout_text(tsv)
            rendered.unlink(missing_ok=True)
            return page

        try:
            info = run([tools["pdfinfo"], str(path)], opening=True)
        except _UserPassword:
            return "", (), "unsupported", ("encrypted_pdf",), frozenset(), 0
        # "Encrypted: yes" past this point is an owner password (it opened without one).
        pages_in = int(_only_line(info, r"^Pages:\s+(\d+)\s*$").group(1))
        bound = MAX_PAGES if max_pages is None else min(max_pages, MAX_PAGES)
        if pages_in > bound and not first_pages:
            raise ValidationError("PDF page count exceeds bound")
        total = min(pages_in, bound)  # the pages read
        if total < 1:
            raise OcrFailed("page_count")
        sizes = {}
        listing = run([tools["pdfinfo"], "-f", "1", "-l", str(total), str(path)])
        for match in re.finditer(r"^Page\s+(\d+)\s+size:\s+(\d+(?:\.\d+)?) x (\d+(?:\.\d+)?) pts",
                                 listing, re.M):
            number = int(match.group(1))
            if number in sizes:
                raise OcrFailed("page_count")
            sizes[number] = (float(match.group(2)), float(match.group(3)))
        if set(sizes) != set(range(1, total + 1)):
            raise OcrFailed("page_count")
        # One text-layer pass; pdftotext ends every page with a form feed. A
        # form feed inside the text breaks that split, so read page by page.
        # Like pypdf and PDFKit, the text layer is not clipped to the visible box.
        # A text layer poppler refuses (pdftotext exits non-zero: damaged streams, a
        # copy restriction) is no text layer when pages are OCRed: the pages are rendered
        # and read like a scan, so their text is review-grade; without OCR it is the
        # typed ``text_layer_exit``. A signal or deadline is never read around.
        def layer_pass(*arguments: str) -> str | None:
            try:
                return run([tools["pdftotext"], "-q", "-enc", "UTF-8", *arguments,
                            str(path), "-"])
            except OcrFailed as error:
                if ocr and error.cause == "text_layer_exit":
                    return None
                raise

        last = ["-l", str(total)] if total < pages_in else []
        whole = layer_pass(*last)
        if whole is None:
            layers = [""] * total
        else:
            layers = whole.split("\f")
        if whole is not None and (len(layers) != total + 1 or layers[-1].strip()):
            layers = [(layer_pass("-f", str(index), "-l", str(index)) or "").replace("\f", "")
                      for index in range(1, total + 1)]
        if ocr and sum(not layer.strip() for layer in layers[:total]) > MAX_OCR_PAGES:
            raise OcrFailed("image_page_bound")
        parts: list[str] = []
        pages: list[dict[str, int]] = []
        ocr_pages: set[int] = set()
        position = 0
        for index in range(1, total + 1):
            number = str(index)
            page_text = layers[index - 1]
            if ocr and not page_text.strip():
                width, height = sizes[index]
                if not 0 < max(width, height) <= _MAX_PAGE_POINTS or min(width, height) <= 0:
                    raise OcrFailed("page_bound")
                dpi = max(1, min(300, int(_MAX_RENDER_PIXELS * 72 / max(width, height))))
                try:
                    page_text = ocr_page(index, dpi, sizes[index])
                except OcrFailed as error:
                    # A render or recognition that died (out of memory on a large
                    # raster, a tool crash): once more at half the resolution, then the
                    # typed failure. A deadline or a bad answer is not retried.
                    if error.cause not in _PAGE_RETRY_CAUSES or dpi <= 100:
                        raise
                    page_text = ocr_page(index, dpi // 2, sizes[index])
                ocr_pages.add(index)
            else:
                page_text = page_text.replace(LAYOUT_MARK, "").strip()
            if position + len(page_text) > MAX_TEXT:
                raise ValidationError("extracted PDF text exceeds bound")
            parts.append(page_text)
            pages.append({"page": index, "start": position,
                          "end": position + len(page_text)})
            position += len(page_text)
            if index < total:
                parts.append("\n")
                position += 1
    text = "".join(parts)
    status = "extracted" if text.strip() else "no_text"
    warnings = (("ocr_text_requires_identity_review",) if ocr_pages else ())
    return text, tuple(pages), status, warnings, frozenset(ocr_pages), pages_in


def mail_body(message: Any) -> tuple[Any, ...]:
    """The MIME entities whose text is ``message``'s own text: its body (plain text
    preferred, else HTML; a ``multipart`` message whose parts cannot be split, no
    boundary or a truncated one, is all body), then the other renderings of it in the
    same ``multipart/alternative``; empty when it has no text body. Every other leaf
    entity is an attachment, a container member of its own. The one rule for both
    readings of a mail: :func:`extract_document` reads the first, and
    ``operating.use_case_parts.members`` makes every other leaf a member."""
    body = message.get_body(preferencelist=("plain", "html")) \
        if message.is_multipart() else message
    if body is None or (body.get_content_type() not in _BODY_TYPES and
                        body.get_content_maintype() != "multipart"):
        return ()
    # Parents of multipart children only: get_body never looks inside a carried message.
    # A malformed multipart (no boundary, truncated) has a str payload and no children.
    parents, stack = {}, [message]
    while stack:
        entity = stack.pop()
        if entity.get_content_maintype() == "multipart" and \
                isinstance(payload := entity.get_payload(), list):
            for child in payload:
                parents[id(child)] = entity
                stack.append(child)
    node = parents.get(id(body))
    while node is not None and node.get_content_type() != "multipart/alternative":
        node = parents.get(id(node))
    if node is None:
        return (body,)
    renderings, stack = [], [node]
    while stack:
        entity = stack.pop()
        if entity.get_content_maintype() == "multipart":
            if isinstance(payload := entity.get_payload(), list):
                stack.extend(reversed(payload))
        elif entity is not body and entity.get_content_type() in _BODY_TYPES \
                and not entity.is_attachment():
            renderings.append(entity)
    return (body, *renderings)


_BODY_TYPES = ("text/plain", "text/html")


def body_text(entity: Any) -> str | None:
    """The text of one :func:`mail_body` entity (HTML as its visible text; an unsplit
    ``multipart`` body as UTF-8 text); None when it does not decode."""
    try:
        if entity.get_content_maintype() == "multipart":
            value = (entity.get_payload(decode=True) or b"").decode(
                entity.get_content_charset("utf-8"))
        else:
            value = entity.get_content()
    except (LookupError, UnicodeError, ValueError):
        return None
    if not isinstance(value, str):
        return None
    if entity.get_content_type() == "text/html":
        parser = _HTMLText()
        parser.feed(value)
        value = parser.text()
    return value


def _eml_text(content: bytes) -> tuple[str, tuple[str, ...]]:
    """An RFC822 message's own text (headers and :func:`mail_body`), never its
    attachments; a body that does not decode is the warning
    ``text_encoding_unsupported``."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
    except Exception as exc:
        return "", (f"email_parse_error:{type(exc).__name__}",)
    if not message.get("From") and not message.get("To") and not message.get("Subject"):
        return "", ("email_headers_missing",)
    headers = []
    for key in ("Subject", "From", "To", "Date"):
        if message.get(key):
            headers.append(f"{key}: {message.get(key)}")
    own, warnings = "", ()
    for body in mail_body(message)[:1]:
        value = body_text(body)
        if value is None:
            warnings = ("text_encoding_unsupported",)
        else:
            own = value
    return "\n".join(headers + [own]), warnings


MSG_TYPES = frozenset({"application/vnd.ms-outlook", "application/x-msg"})


def _is_message(mime: str, filename: str) -> bool:
    name = filename.lower()
    return mime in MSG_TYPES or mime == "message/rfc822" or name.endswith((".eml", ".msg"))


def _msg_text(content: bytes) -> tuple[str, tuple[str, ...]]:
    """An Outlook ``.msg`` in ``_eml_text``'s shape: header lines and the body, never
    its attachments."""
    from .office_text import OfficeFormatError, read_msg
    try:
        message = read_msg(content)
    except OfficeFormatError as error:
        return "", (str(error),)
    body = message["body"]
    if message["html"]:
        parser = _HTMLText()
        parser.feed(body)
        body = parser.text()
    if not message["headers"] and not body.strip():
        return "", ("msg_headers_and_body_missing", *message["warnings"])
    text = "\n".join([f"{key}: {value}" for key, value in message["headers"]] + [body])
    return text, tuple(message["warnings"])


# Identifier grammars come from the one identifier schema
# (ontology/schema/identifiers.json): ``document_labelled`` (a ':' or '#' after
# the label; a value ends at whitespace, a separator or the text's end, so a
# value cut short by a character outside its class ("SKU_88*A") is no claim
# rather than a wrong literal prefix ("SKU"); OCR of multi-column shop-floor
# forms often yields "PRODUCT CODE:\nLINE NUMBER", so a product code never
# crosses a line boundary) and ``ao_form``: Ricardo's scanned shop-floor AO has
# adjacent "ERP CODE: <product> OE: <order-entry>" fields; their printed values
# are literal, but mapping these short form labels to Product and SalesOrder is
# an interpretation, so they stay review-grade even when a future copy has a
# machine-readable text layer (horizontal whitespace only: one empty form cell
# must not consume the next line's unrelated identifier).
_IDENTIFIERS = _ids.profile_patterns("document_labelled")
_AO_FORM_IDENTIFIERS = _ids.profile_patterns("ao_form")
# A quantity's unit is the word right after its number (at most one space): past a
# column gap (a tab, two spaces, a line end) a word belongs to the next cell. A word
# there that is no label is a unit the gap may have cut off, so the unit is
# ``unknown`` (``_quantity_unit``), never assumed absent; both are in ``NO_UNIT``.
_UNIT_WORDS = r"bottles?|cartons?|cases?|pallets?|units?|pieces?|pcs|each|ea|kg|lb|lbs"
_QUANTITY = re.compile(r"\b(?P<label>planned quantity|actual production quantity|manufactured quantity|sample quantity|inspected quantity|accepted quantity|rejected quantity|ordered quantity|gross weight|net weight|units per carton|cartons per pallet|quantity|qty)\s*[:#]\s*(?P<number>\d(?:[\d.,]*\d)?)(?: ?(?P<unit>" + _UNIT_WORDS + r")\b)?\b", re.I)
NO_UNIT = ("unspecified", "unknown")
# Currencies are the one table in ontology/units.py: a written token is an ISO 4217 code
# (any case), a sign form (US$, C$) or a code with a sign attached; a bare sign is the
# separate ``symbol``. Only ISO 4217 cores are matched, so a non-ISO word ("RMB") never
# makes a price claim, and ``currency_reading`` leaves a contradicting sign unknown.
_ISO_CODES = "|".join(sorted(ISO_4217))
_SIGN_CLASS = "[" + "".join(SIGN_CURRENCIES) + "]"
_CURRENCY_TOKEN = currency_token_pattern(_ISO_CODES, bare_sign=False)
_PRICE = re.compile(r"\b(?P<label>unit price|total price|total amount|freight quote|shipping cost|price)\s*[:#]\s*(?:(?P<currency>" + _CURRENCY_TOKEN + r")\s*)?(?:(?P<symbol>" + _SIGN_CLASS + r")\s*)?(?P<number>\d(?:[\d.,]*\d)?)\b", re.I)
# A document-level currency declaration ("Currency: CAD", "All prices in
# USD") whose code ends the line, so "CAD; USD" or "CAD (from USD)" is not
# one. It applies to its own document only: a mail and each attachment (a
# container member) are read apart. The separator alternatives cannot
# overlap, keeping the scan linear on long whitespace runs.
_CURRENCY_DECLARATION = re.compile(r"(?im)^[ \t]*(?:currency|(?:all[ \t]+)?(?:prices|amounts)[ \t]+(?:are[ \t]+)?in)(?:[ \t]*[:#][ \t]*|[ \t]+)(" + _ISO_CODES + r")[ \t]*\r?$")
_DATE = re.compile(r"\b(?P<label>requested delivery|delivery date|expected receipt date|expected arrival date|ship date|shipping date|due date|order date)\s*[:#]\s*(?P<value>\d{4}-\d{2}-\d{2}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})\b", re.I)
_PARTY = re.compile(r"(?m)^[ \t]*(?P<label>supplier|vendor|customer|ship to|bill to|origin|destination)[ \t]*[:#][ \t]*(?P<value>[^ \t\r\n][^\r\n]{1,199}?)(?=[ \t]+(?:PO|REQUIRED DATE|SHIP TO|BILL TO|CUSTOMER|SUPPLIER|VENDOR)[ \t]*[:#]|[ \t]*$)", re.I)
_MAIL_PARTY = re.compile(r"(?m)^(?P<label>From|To):\s*(?P<value>[^\r\n]{2,200})", re.I)
_MAIL_DATE = re.compile(r"(?m)^Date:\s*(?P<value>[^\r\n]{4,100})", re.I)
_SPEC = re.compile(r"(?m)^[^\S\n]*(?P<label>material|color|colour|capacity|dimensions|packaging|shipping mode|incoterm|carrier)\s*[:#]\s*(?P<value>[^\r\n]{1,200})", re.I)
_ITEM = _ids.profile_pattern("document_item_line")


# A written number: digits with "." / "," as decimal mark or thousands separator.
NUMBER_TOKEN = re.compile(r"\d(?:[\d.,]*\d)?")


def _grouped(text: str, separator: str) -> bool:
    """Whether ``text`` is digits in thousands groups ("1,234,567") joined by ``separator``."""
    return re.fullmatch(r"[1-9]\d{0,2}(?:" + re.escape(separator) + r"\d{3})+", text) is not None


def _decimal_mark(raw: str) -> str | None:
    """The decimal mark one written number proves by itself, else None: both marks
    ("1.234,56": the last one), a repeated separator ("1,234,567": the other one is
    the decimal mark) or one separator that cannot group thousands ("0.04125",
    "0,85", "12.5")."""
    seps = [c for c in raw if c in ".,"]
    if not seps:
        return None
    if len(set(seps)) == 2:
        last = seps[-1]
        head = raw[:raw.rfind(last)]
        return last if seps.count(last) == 1 and _grouped(head, "," if last == "." else ".") \
            else None
    separator = seps[0]
    if len(seps) > 1:
        return ("," if separator == "." else ".") if _grouped(raw, separator) else None
    head, tail = raw.split(separator)
    return separator if len(tail) != 3 or len(head) > 3 or head.startswith("0") else None


@functools.lru_cache(maxsize=8)
def number_convention(text: str) -> str | None:
    """The decimal mark ("." or ",") a document's numbers prove, when they agree on
    one; None when no number proves one or two numbers prove different ones."""
    marks = {mark for token in NUMBER_TOKEN.finditer(text)
             if (mark := _decimal_mark(token.group())) is not None}
    return next(iter(marks)) if len(marks) == 1 else None


def parse_number(raw: str, convention: str | None) -> tuple[Decimal | None, str | None]:
    """(value, None) or (None, problem) for one written number (``NUMBER_TOKEN``).

    A separator is read as a decimal mark only when that is unambiguous: the number
    proves it (``_decimal_mark``), or it is a single separator before exactly three
    digits ("1,234": thousands or 1.234) and the document's ``convention`` decides.
    Without one that number is ``decimal_separator_ambiguous``, never a guess; an
    impossible grouping ("1,2,3") is ``number_unreadable``."""
    if NUMBER_TOKEN.fullmatch(raw) is None:
        return None, "number_unreadable"
    seps = [c for c in raw if c in ".,"]
    if not seps:
        return Decimal(raw), None
    mark = _decimal_mark(raw)
    if mark is None and len(seps) == 1:
        if convention is None:
            return None, "decimal_separator_ambiguous"
        mark = convention
    if mark is None or mark not in raw:   # every separator groups thousands
        if len(set(seps)) == 1 and _grouped(raw, seps[0]):
            return Decimal(raw.replace(seps[0], "")), None
        return None, "number_unreadable"
    head, _, tail = raw.rpartition(mark)
    group = "," if mark == "." else "."
    if mark in head or (group in head and not _grouped(head, group)):
        return None, "number_unreadable"
    return Decimal(head.replace(group, "") + "." + tail), None


def _number(value: str, convention: str | None) -> int | float | None:
    """A claim's JSON number (``parse_number``); None when it is not unambiguous."""
    parsed, _ = parse_number(value, convention)
    if parsed is None:
        return None
    return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)


def _claim(kind: str, field_name: str, value: Any, match: re.Match[str], group: int | str,
           *, extras: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = {"kind": kind, "field": field_name, "value": value,
              "span": {"start": match.start(group), "end": match.end(group)},
              "confidence": "literal", "interpretation": False}
    if extras:
        result.update(extras)
    return result


@functools.lru_cache(maxsize=8)
def _declared_currencies(text: str) -> frozenset[str]:
    """The currency codes the document declares.

    Cached because line_item_text_witness reparses many blocks of one text.
    """
    return frozenset(match.group(1).upper() for match in _CURRENCY_DECLARATION.finditer(text))


def _price_currency(inline: str | None, symbol: str | None,
                    declared: frozenset[str]) -> tuple[str, bool]:
    """Resolve a price's currency; return (code or "unknown", literal).

    Both are read from the one table in ``ontology/units.py`` (``currency_reading``).
    An explicit code or sign form on the amount itself is literal (a code with a sign
    that is not its own attached, "CAD€", is unknown; a code wins over a separate sign
    after it, which is not read), and so is a one-currency sign ("€", "£")
    when no declaration in its document contradicts it. A sign several currencies
    write ("$", "¥") never names one on its own: it resolves only through exactly
    one consistent declaration in its document, and that binding is an
    interpretation because the amount's own span does not state the currency. No
    symbol, several declarations or a contradiction ("Currency: CAD" with "€")
    leave the currency unknown.
    """
    if inline:
        code, _candidates = currency_reading(inline)
        return (code, True) if code else ("unknown", False)
    code, candidates = currency_reading(symbol) if symbol else (None, frozenset())
    if not candidates:
        return "unknown", False
    if not declared and code is not None:
        return code, True
    if len(declared) == 1 and declared <= candidates:
        return next(iter(declared)), len(candidates) == 1
    return "unknown", False


# A column gap: a run of spaces and tabs holding a tab or two or more spaces (OCR keeps
# a scan's wide interword spaces; one space stays inside a cell). The one rule for a
# value a gap may have cut, here and in ``ontology.document_fields`` (its patterns end a
# value at ``COLUMN_GAP``, a lookahead fragment).
COLUMN_GAP = r"\t|  "


def column_gap_end(text: str, at: int) -> int | None:
    """Where the next cell of ``at``'s line starts when a column gap starts at ``at``,
    else None (no whitespace, one space, or a line end); bounded by the run, never a
    copy of the rest of the text."""
    end = at
    while end < len(text) and text[end] in " \t":
        end += 1
    run = text[at:end]
    return end if "\t" in run or len(run) >= 2 else None


def _quantity_unit(match: re.Match[str], text: str) -> str:
    """A quantity's unit: its written unit, else ``unknown`` when the next cell past a
    column gap (:func:`column_gap_end`) starts with a word and is no label (holds no ':'
    or '#'), as it may be the cut-off unit, else ``unspecified``."""
    if match.group("unit"):
        return match.group("unit").lower()
    cell = column_gap_end(text, match.end())
    if cell is None or cell >= len(text) or not text[cell].isalpha():
        return "unspecified"
    while cell < len(text) and text[cell] not in ":#\t\n" and \
            not text.startswith("  ", cell):
        cell += 1
    return "unspecified" if cell < len(text) and text[cell] in ":#" else "unknown"


def _extract_claims(text: str, *, context: str | None = None
                    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Extract claims from ``text``.

    ``context`` reparses a block of a larger document (``text`` is a slice of
    it) while resolving currency declarations against the whole document.
    """
    declared = _declared_currencies(text if context is None else context)
    convention = number_convention(text if context is None else context)
    claims: list[dict[str, Any]] = []

    def number_claim(kind: str, match: re.Match[str], extras: dict[str, Any]) -> None:
        # A number that is not unambiguous is no value ("1,234" with no decimal
        # convention in the document): kept as a review claim saying why.
        value, problem = parse_number(match.group("number"), convention)
        if problem is not None:
            extras.update({"unreadable": problem, "confidence": "review",
                           "interpretation": True})
        claims.append(_claim(kind, match.group("label").lower(),
                             None if value is None else
                             _number(match.group("number"), convention),
                             match, "number", extras=extras))

    for kind, pattern in _IDENTIFIERS:
        for match in pattern.finditer(text):
            value = match.group(1).rstrip(".,;:")
            # A digit-led cell past a column gap: OCR may have split one identifier
            # ("PO: 4500  123"), so the read prefix is uncertain. Past one space the
            # digits are the same cell's next word ("Invoice No: 88123 12 cartons").
            after = column_gap_end(text, match.end(1))
            continued = after is not None and after < len(text) and text[after].isdigit()
            claims.append(_claim("identifier", kind, value, match, 1, extras={
                "confidence": "review", "interpretation": True,
                "uncertain": "split_at_gap"} if continued else None))
    for kind, pattern in _AO_FORM_IDENTIFIERS:
        for match in pattern.finditer(text):
            claims.append(_claim("identifier", kind, match.group(1), match, 1,
                                 extras={"confidence": "review", "interpretation": True}))
    for match in _QUANTITY.finditer(text):
        number_claim("quantity", match, {"unit": _quantity_unit(match, text)})
    for match in _PRICE.finditer(text):
        currency, literal = _price_currency(match.group("currency"), match.group("symbol"),
                                            declared)
        # Only a literal currency makes a priced operational fact. An unknown
        # or declaration-resolved currency stays a claim (the latter
        # review-grade) and is never projected into costs.
        extras: dict[str, Any] = {"currency": currency}
        if currency != "unknown" and not literal:
            extras.update({"confidence": "review", "interpretation": True})
        number_claim("price", match, extras)
    for match in _DATE.finditer(text):
        raw = match.group("value")
        parsed = None
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                parsed = datetime.strptime(raw, fmt).date().isoformat()
                break
            except ValueError:
                continue
        if parsed:
            claims.append(_claim("date", match.group("label").lower(), parsed, match, "value"))
    for match in _PARTY.finditer(text):
        value = match.group("value").strip()
        claims.append(_claim("party", match.group("label").lower(), value, match, "value"))
    for match in _MAIL_PARTY.finditer(text):
        claims.append(_claim("party", match.group("label").lower(),
                             match.group("value").strip(), match, "value"))
    for match in _MAIL_DATE.finditer(text):
        try:
            parsed = parsedate_to_datetime(match.group("value"))
            if parsed.tzinfo is not None:
                claims.append(_claim("date", "message date", parsed.isoformat(), match, "value",
                                     extras={"confidence": "review", "interpretation": True}))
        except (TypeError, ValueError, IndexError):
            pass
    for match in _SPEC.finditer(text):
        label = match.group("label").lower()
        claims.append(_claim("shipping" if label in {"shipping mode", "incoterm", "carrier"}
                             else "specification", label, match.group("value").strip(), match, "value"))

    # Group claims by physical line only when an explicit item label, quantity,
    # and price coexist. The grouping is useful but is an interpretation, so it
    # never becomes an authoritative SourceDocument identifier.
    lines: list[dict[str, Any]] = []
    physical = text.splitlines(keepends=True)
    offsets = []
    offset = 0
    for line in physical:
        offsets.append(offset)
        offset += len(line)
    for index, line in enumerate(physical):
        item = _ITEM.search(line)
        if not item:
            continue
        # A labeled item can share one line with quantity/price or head a
        # compact key/value block. Stop at the next item or a blank separator.
        block = line
        end_index = index
        for later in range(index + 1, min(index + 5, len(physical))):
            if not physical[later].strip() or _ITEM.search(physical[later]):
                break
            block += physical[later]
            end_index = later
        qty = _QUANTITY.search(block)
        price = _PRICE.search(block)
        quantity = qty and _number(qty.group("number"), convention)
        unit_price = price and _number(price.group("number"), convention)
        if qty and price and price.group("label").lower() == "unit price" and \
                quantity is not None and unit_price is not None:
            lines.append({"item_code": item.group("code"),
                "quantity": quantity,
                "unit": _quantity_unit(qty, block),
                "unit_price": unit_price,
                "currency": _price_currency(price.group("currency"), price.group("symbol"),
                                            declared)[0],
                "span": {"start": offsets[index] + item.start(),
                         "end": offsets[end_index] + len(physical[end_index].rstrip("\r\n"))},
                "confidence": "review", "interpretation": True})
    claims.sort(key=lambda c: (c["span"]["start"], c["kind"], c["field"]))
    if len(claims) > MAX_CLAIMS or len(lines) > MAX_LINE_ITEMS:
        raise ValidationError("extracted claim count exceeds bound")
    return tuple(claims), tuple(lines)


def line_item_text_witness(text: str, item: Mapping[str, Any]) -> bool:
    """Reparse one cited block before using a stored interpreted line item.

    A source observation can outlive an extraction implementation or contain
    an inconsistent line_items payload. This verifies that the current text
    still yields the exact item, quantity, unit, price and currency. It does
    not certify that OCR accurately represented the original PDF.
    """
    if not isinstance(text, str) or not isinstance(item, Mapping):
        return False
    span = item.get("span")
    if (not isinstance(span, Mapping) or type(span.get("start")) is not int or
            type(span.get("end")) is not int or
            not 0 <= span["start"] < span["end"] <= len(text)):
        return False
    line_start = text.rfind("\n", 0, span["start"]) + 1
    block = text[line_start:span["end"]]
    if len(block) > 4000:
        return False
    try:
        _, parsed = _extract_claims(block, context=text)
    except (ValidationError, ValueError, OverflowError):
        return False
    expected = {key: item.get(key) for key in
                ("item_code", "quantity", "unit", "unit_price", "currency")}
    return any(all(row.get(key) == value for key, value in expected.items())
               for row in parsed)


_MARKDOWN_ESCAPED = "_*"  # what MarkItDown 0.1.x's DOCX output escapes (markdownify)


def _conversion_ranges(conversion: Mapping[str, Any], length: int
                       ) -> list[tuple[int, int, str, tuple[tuple[int, int, int], ...]]]:
    """(start, end, format, slide spans) of machine-converted text: the whole stored text."""
    return [(0, length, conversion["format"],
             tuple((span["number"], span["start"], span["end"])
                   for span in conversion["spans"] if span["unit"] == "slide"))]


def _converted_claims(text: str, ranges: list[tuple[int, int, str,
                      tuple[tuple[int, int, int], ...]]]
                      ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Claims from machine-converted text, cited in the stored (converted) text.

    The stored text stays byte-exact converter output. DOCX output escapes
    ``_`` and ``*`` with a backslash, so claims are read from a view with
    those escapes removed and every span maps back to the stored text. Each
    claim inside converted text is marked ``converted``. It is review-grade
    where the view is not an exact reading of the source: a bare ``_``/``*``
    in DOCX output is Markdown markup (emphasis), and a ``\\_``/``\\*`` in PPTX
    output (which escapes nothing) may be the source's own backslash. PPTX
    claims cite their slide.
    """
    view: list[str] = []
    origin: list[int] = []  # stored index where each view character begins
    markup: set[int] = set()
    position = 0
    for start, end, fmt, _slides in sorted(ranges):
        view.extend(text[position:start])
        origin.extend(range(position, start))
        index = start
        while index < end:
            char = text[index]
            if fmt == "docx" and char == "\\" and index + 1 < end and \
                    text[index + 1] in _MARKDOWN_ESCAPED:
                view.append(text[index + 1])
                origin.append(index)
                index += 2
                continue
            if fmt == "docx" and char in _MARKDOWN_ESCAPED:
                markup.add(len(view))
            view.append(char)
            origin.append(index)
            index += 1
        position = end
    view.extend(text[position:])
    origin.extend(range(position, len(text)))
    origin.append(len(text))
    claims, line_items = _extract_claims("".join(view))

    def place(view_span: Mapping[str, int]) -> tuple[dict[str, int], dict[str, Any]]:
        """Stored span, plus the converted marker/grade/slide for a view span."""
        span = {"start": origin[view_span["start"]], "end": origin[view_span["end"]]}
        inside = next((item for item in ranges
                       if item[0] <= span["start"] and span["end"] <= item[1]), None)
        if inside is None:
            return span, {}
        _start, _end, fmt, slides = inside
        extra: dict[str, Any] = {"converted": True}
        stored_text = text[span["start"]:span["end"]]
        if ((fmt == "docx" and any(index in markup for index in
                                   range(view_span["start"], view_span["end"]))) or
                (fmt == "pptx" and re.search(r"\\[_*]", stored_text))):
            extra.update(confidence="review", interpretation=True)
        slide = next((number for number, first, last in slides
                      if first <= span["start"] and span["end"] <= last), None)
        if slide is not None:
            extra["slide"] = slide
        return span, extra

    mapped_claims = []
    for claim in claims:
        span, extra = place(claim["span"])
        mapped_claims.append({**claim, "span": span, **extra})
    # Line items are review-grade interpretations already; only their spans move.
    mapped_items = [{**item, "span": place(item["span"])[0]} for item in line_items]
    mapped_claims.sort(key=lambda c: (c["span"]["start"], c["kind"], c["field"]))
    return tuple(mapped_claims), tuple(mapped_items)


# JSON is read as written (never re-serialized: a parse would round floats and reorder
# nothing worth the loss), its raw bytes hashed as every file's are.
PLAIN_TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "text/csv",
                    "application/json", "text/json")
PLAIN_TEXT_SUFFIXES = (".csv", ".md", ".markdown", ".txt", ".json")
HTML_TYPES = ("text/html", "application/xhtml+xml")
HTML_SUFFIXES = (".html", ".htm", ".xhtml")
# An HTML file is parsed (stdlib HTMLParser) only up to this many bytes (about 1.5 s with
# its claims at the bound): a map or a report page is a few hundred KB; past the bound it
# is ``unsupported`` with the ``html_exceeds_bound`` warning, never parsed on a read.
MAX_HTML_BYTES = 2_000_000


def is_plain_text(kind: str, filename: str = "") -> bool:
    """Whether a file of MIME ``kind`` (lower case, no parameters) named ``filename`` is
    read as UTF-8 text as written: plain text, Markdown, CSV or JSON by type or by name."""
    return (any(kind.startswith(t) for t in PLAIN_TEXT_TYPES)
            or filename.lower().endswith(PLAIN_TEXT_SUFFIXES))


def is_html(kind: str, filename: str = "") -> bool:
    """Whether a file of MIME ``kind`` (lower case, no parameters) named ``filename`` is
    read as HTML (its visible text, :func:`html_text`) by type or by name."""
    return kind in HTML_TYPES or filename.lower().endswith(HTML_SUFFIXES)


def extract_document(content: bytes, mime_type: str, *, original_url: str,
                     filename: str = "", local_pdf_ocr: bool = False) -> ExtractedDocument:
    """Extract a bounded authorized source document without fetching or writing.

    A message's text is its own headers and body (module docstring): its
    attachments are container members, read on their own. A PDF over ``MAX_PAGES``
    (with or without ``local_pdf_ocr``) is read on pages 1-``MAX_PAGES`` and the rest
    is ``skipped_pages`` (warning ``pdf_pages_skipped_by_cap:<first>-<last>``), as the
    use-case intake reads it, never refused outright.
    """
    _https(original_url)
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise ValidationError("document bytes required within 20 MB bound")
    if not isinstance(mime_type, str) or not mime_type.strip():
        raise ValidationError("MIME type required")
    kind = mime_type.split(";", 1)[0].strip().lower()
    pages: tuple[dict[str, int], ...] = ()
    ocr_pages: frozenset[int] = frozenset()
    review_ranges: list[tuple[int, int]] = []
    warnings: tuple[str, ...] = ()
    conversion: dict[str, Any] | None = None
    sheets: list[dict[str, Any]] = []
    failure: str | None = None
    skipped: tuple[int, int] | None = None
    if (readable := doc_convert.convertible_kind(kind, filename)) is not None:
        ocr = local_pdf_ocr and readable == "pdf"
        reading = read_pages(content, readable, ocr=ocr, first_pages=readable == "pdf")
        text, pages, status = reading.text, reading.spans, reading.status
        warnings, method, ocr_pages = reading.warnings, reading.method, reading.ocr_pages
        conversion = dict(reading.conversion) if reading.conversion is not None else None
        review_ranges = [(page["start"], page["end"]) for page in pages
                         if page["page"] in ocr_pages]
        failure = reading.failure
        if any(page.unread for page in reading.pages):
            # A page pypdf could not read: never a partial document.
            text, pages, status, conversion, method = "", (), "unsupported", None, "none"
            failure = ("parser_panicked" if any(page.unread == "parser_panicked"
                                                for page in reading.pages)
                       else "pdf_parse_error")
        elif (skipped := reading.skipped_pages) is not None:
            warnings += (f"pdf_pages_skipped_by_cap:{skipped[0]}-{skipped[1]}",)
    elif kind == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or (
            kind == "application/octet-stream" and filename.lower().endswith(".xlsx")):
        text, status, warnings, ranges, found_sheets = extract_xlsx_text(content, max_text=MAX_TEXT)
        method = "ooxml-cell-transcription"
        review_ranges = list(ranges)
        sheets = list(found_sheets)
    elif _is_message(kind, filename):
        msg = kind in MSG_TYPES or filename.lower().endswith(".msg")
        text, warnings = (_msg_text if msg else _eml_text)(content)
        status = "extracted" if text.strip() else "unsupported"
        method = "outlook-msg" if msg else "rfc822-mime"
    elif is_html(kind, filename):
        method, text = "html-text", ""
        if len(content) > MAX_HTML_BYTES:
            status, warnings = "unsupported", (f"html_exceeds_bound:{MAX_HTML_BYTES}",)
        else:
            try:
                text = html_text(content.decode("utf-8-sig"))
                status = "extracted" if text else "no_text"
            except UnicodeDecodeError:
                status, warnings = "unsupported", ("text_encoding_unsupported",)
    elif is_plain_text(kind, filename):
        # A .csv is text whatever the host declares (vnd.ms-excel), as a member's is; a
        # Markdown or CSV file is plain text read as written (no rendering).
        try:
            text = content.decode("utf-8-sig")
            status, method = "extracted", "utf8-text"
        except UnicodeDecodeError:
            text, status, method = "", "unsupported", "utf8-text"
            warnings = ("text_encoding_unsupported",)
    else:
        text, status, method = "", "unsupported", "none"
        warnings = (f"unsupported_mime_type:{kind}",)
    # An HTML file's cost is bounded by its bytes (MAX_HTML_BYTES); its visible text is
    # never longer than they are, so it is read whole (a map page holds >MAX_TEXT chars).
    if len(text) > (MAX_HTML_BYTES if method == "html-text" else MAX_TEXT):
        raise ValidationError("extracted document text exceeds bound")
    if len(warnings) > MAX_WARNINGS:
        raise ValidationError("document warning count exceeds bound")
    if status != "extracted":
        claims, line_items = (), ()
    elif conversion is not None:
        claims, line_items = _converted_claims(text, _conversion_ranges(conversion, len(text)))
    else:
        claims, line_items = _extract_claims(text)
    if review_ranges:
        reviewed = []
        for claim in claims:
            span = claim["span"]
            if any(not (span["end"] <= start or span["start"] >= end)
                   for start, end in review_ranges):
                claim = {**claim, "confidence": "review", "interpretation": True}
            reviewed.append(claim)
        claims = tuple(reviewed)
    if pages:
        def page_for(span: Mapping[str, int]) -> int | None:
            return next((page["page"] for page in pages
                         if page["start"] <= span["start"] and
                         span["end"] <= page["end"]), None)
        claims = tuple(({**claim, "page": page} if
                        (page := page_for(claim["span"])) is not None else claim)
                       for claim in claims)
        line_items = tuple(({**item, "page": page} if
                            (page := page_for(item["span"])) is not None else item)
                           for item in line_items)
    if sheets:
        def sheet_for(span: Mapping[str, int]) -> str | None:
            return next((sheet["sheet"] for sheet in sheets
                         if sheet["start"] <= span["start"] and span["end"] <= sheet["end"]), None)
        claims = tuple(({**claim, "sheet": sheet} if
                        (sheet := sheet_for(claim["span"])) is not None else claim)
                       for claim in claims)
    return ExtractedDocument(kind, sha256(content).hexdigest(), status, method,
                             text, claims, line_items, warnings, pages,
                             tuple(sorted(ocr_pages)), conversion, tuple(sheets), failure,
                             skipped)
