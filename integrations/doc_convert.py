"""Office/PDF to Markdown through Portal's MarkItDown path, locally and optionally.

Portal's converter (``plx-customer-portal`` ``scripts/markitdown/convert.py``
at staging ``5724575``) is a Python CLI, not an API, and nothing upstream
converts arbitrary bytes over a GET. So the Workbench mirrors it here:

* the same package (``markitdown``, Portal pins ``markitdown[pdf,docx,pptx,xlsx]
  >=0.1.6`` in ``scripts/markitdown/requirements.txt``) as an OPTIONAL
  dependency, like ``pypdf``;
* exactly Portal's options: ``MarkItDown(enable_plugins=False)`` and
  ``convert_local(<local path>)``, never a URI, never a stream from the
  network, no Azure Document Intelligence endpoint, no LLM client;
* exactly Portal's post-processing: ``result.markdown`` or else
  ``result.text_content``; no result is an error. The text is not rewritten.

The bytes go to a private temporary file (mode 0600) that is deleted after the
call; nothing is sent to any service. MarkItDown's file-type guesser (magika)
runs onnxruntime, whose OneDS telemetry posts to Microsoft unless
``ORT_DISABLE_TELEMETRY=1``; ``_import_backend``, the one place this code base
imports that stack, sets it first. When the package is not importable,
``installed_backend()`` returns ``None`` and callers fall back to the existing
extractors (``integrations/document_understanding.py``). Scanned-page OCR is
not done here: it stays Workbench work (``local_pdf_ocr``).

Without the package, DOCX and PPTX still convert: ``convert_stdlib`` reads them
with the stdlib (``integrations/office_text.py``) into the shape MarkItDown
writes, recorded as converter ``plx-office-stdlib`` with its own options. Portal
exposes no converted text over its read API (``convert.py`` is a local CLI), so
the Workbench converts the captured bytes itself rather than consuming Portal's.

Every conversion carries provenance (``Conversion.record()``): the original's
SHA-256, converter name and version, the options and their digest, the
output's SHA-256, page or slide spans where the output marks them, and the
label ``machine-converted``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import functools
import importlib.util
from hashlib import sha256
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from . import child_process

CONVERTER = "markitdown"
LABEL = "machine-converted"
UPSTREAM = "plx-customer-portal:scripts/markitdown/convert.py@5724575"
# The call Portal makes, as data. Changing any of it changes OPTIONS_SHA256.
OPTIONS: dict[str, Any] = {
    "constructor": {"enable_plugins": False},
    "call": "convert_local",
    "input": "private-local-file",
    "result": ["markdown", "text_content"],
    "postprocess": "none",
}
OPTIONS_SHA256 = sha256(json.dumps(OPTIONS, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()

# The stdlib converter writes MarkItDown's shape for DOCX/PPTX; its options say
# where it knowingly differs. Bump STDLIB_VERSION on any change to its output.
STDLIB_CONVERTER = "plx-office-stdlib"
STDLIB_VERSION = "1"
STDLIB_OPTIONS: dict[str, Any] = {
    "shape": "markitdown-0.1.x",
    "input": "in-memory-bytes",
    "formats": ["docx", "pptx"],
    "docx": {"emphasis": "dropped", "hyperlinks": "text-only",
             "text_boxes_and_notes": "warned-not-converted"},
    "pptx": {"charts_and_diagrams": "warned-not-converted"},
}
STDLIB_OPTIONS_SHA256 = sha256(json.dumps(STDLIB_OPTIONS, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
_PROFILES = {CONVERTER: (OPTIONS, OPTIONS_SHA256),
             STDLIB_CONVERTER: (STDLIB_OPTIONS, STDLIB_OPTIONS_SHA256)}

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
# Formats this module converts. XLSX is deliberately absent: the Workbench's
# cell transcription keeps formulas apart from cached values and cites cell
# coordinates, which MarkItDown's table rendering does not (see
# DOCUMENT_UNDERSTANDING.md).
_KINDS = {PDF: "pdf", DOCX: "docx", PPTX: "pptx"}
_SUFFIXES = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx"}
# MarkItDown's PPTX converter writes one marker per slide.
_SLIDE = re.compile(r"<!-- Slide number: (\d+) -->")


class ConversionFailed(Exception):
    """The converter ran and raised, or returned no text, for these bytes."""


@dataclass(frozen=True)
class Backend:
    """The MarkItDown class (or a test double with its API) and its version."""
    factory: Callable[..., Any]
    version: str
    # True only for the imported package: its per-format extras are then checked
    # (``missing_extras``). A test double with the same API is taken as ready.
    real: bool = False


@functools.lru_cache(maxsize=1)
def _import_backend() -> Backend | None:
    # Before the first import: onnxruntime (markitdown -> magika) reads it to decide on
    # telemetry. Overridden, not defaulted: no caller may turn that egress back on.
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    try:
        from markitdown import MarkItDown  # type: ignore[import-not-found]
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        # Not installed, or a broken native dependency: either way the
        # existing extractors run instead.
        return None
    try:
        from importlib.metadata import version
        installed = version("markitdown")
    except Exception:
        installed = "unknown"
    return Backend(MarkItDown, installed, real=True)


def installed_backend() -> Backend | None:
    """The importable MarkItDown backend, or ``None`` (fallback to existing extractors)."""
    return _import_backend()


# The extra modules each format's MarkItDown converter imports (markitdown[docx]
# needs mammoth and lxml, and so on); the list in verify/cloud_setup.sh's
# markitdown_pinned. Without them MarkItDown raises "pip install markitdown[docx]".
_REQUIRES: dict[str, tuple[str, ...]] = {"docx": ("mammoth", "lxml"), "pptx": ("pptx",),
                                         "pdf": ("pdfminer",)}


def missing_extras(kind: str) -> tuple[str, ...]:
    """Modules the MarkItDown converter for ``kind`` needs that are not importable here."""
    missing = []
    for name in _REQUIRES.get(kind, ()):
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return tuple(missing)


def available(kind: str | None = None) -> bool:
    """MarkItDown imports; with ``kind``, also that format's converter is ready."""
    if installed_backend() is None:
        return False
    return kind is None or not missing_extras(kind)


def convertible_kind(mime_type: str, filename: str = "") -> str | None:
    """``pdf``/``docx``/``pptx`` for a format this module converts, else ``None``."""
    kind = mime_type.split(";", 1)[0].strip().lower() if isinstance(mime_type, str) else ""
    if kind in _KINDS:
        return _KINDS[kind]
    if kind == "application/octet-stream" and isinstance(filename, str):
        return _SUFFIXES.get(Path(filename.lower()).suffix)
    return None


@dataclass(frozen=True)
class Conversion:
    kind: str
    text: str
    converter_version: str
    source_sha256: str
    spans: tuple[dict[str, Any], ...] = ()
    converter: str = CONVERTER
    # What the stdlib converter saw but did not convert; not part of the record.
    warnings: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        options, options_sha256 = _PROFILES[self.converter]
        return {"label": LABEL, "converter": self.converter,
                "converter_version": self.converter_version,
                "upstream": UPSTREAM, "options": json.loads(json.dumps(options)),
                "options_sha256": options_sha256, "format": self.kind,
                "source_sha256": self.source_sha256,
                "output_sha256": sha256(self.text.encode("utf-8")).hexdigest(),
                "spans": [dict(span) for span in self.spans]}


def _pdf_spans(text: str, page_count: int | None) -> tuple[dict[str, Any], ...]:
    """Page spans from the converter's form feeds, only when they match the page count.

    pdfminer (MarkItDown's PDF reader) ends each page with a form feed. A
    count that disagrees with the PDF's own page count yields no spans rather
    than guessed ones.
    """
    if not page_count:
        return ()
    segments = text.split("\f")
    if len(segments) == page_count + 1 and not segments[-1].strip():
        segments = segments[:-1]
    if len(segments) != page_count:
        return ()
    spans, position = [], 0
    for number, segment in enumerate(segments, start=1):
        spans.append({"unit": "page", "number": number,
                      "start": position, "end": position + len(segment)})
        position += len(segment) + 1
    return tuple(spans)


def _slide_spans(text: str) -> tuple[dict[str, Any], ...]:
    markers = list(_SLIDE.finditer(text))
    if not markers or [int(m.group(1)) for m in markers] != list(range(1, len(markers) + 1)):
        return ()
    bounds = [m.start() for m in markers] + [len(text)]
    return tuple({"unit": "slide", "number": index + 1, "start": bounds[index],
                  "end": bounds[index + 1]} for index in range(len(markers)))


def convert(content: bytes, kind: str, *, backend: Backend,
            page_count: int | None = None,
            builtin: Conversion | None = None) -> Conversion:
    """Convert bytes exactly as Portal's ``convert_local`` does. Raises ``ConversionFailed``.

    ``builtin`` is the caller's stdlib conversion of the same bytes, reused when the
    format's converter is not ready so the bytes are not converted twice."""
    if kind not in _SUFFIXES.values():
        raise ValueError("unsupported_conversion_kind")
    missing = missing_extras(kind) if backend.real else ()
    if missing:
        # This format's converter is not ready: the stdlib extractor reads DOCX/PPTX,
        # with the reason recorded in the warnings; a PDF has no stdlib converter.
        reason = f"markitdown_{kind}_not_ready:missing_{'+'.join(missing)}"
        if kind == "pdf":
            raise ConversionFailed(reason)
        fallback = builtin if builtin is not None else convert_stdlib(content, kind)
        return replace(fallback, warnings=(reason, *fallback.warnings))
    # The private copy of the customer's bytes is removed even when the conversion is
    # abandoned (a budget, the eval watchdog's ``os._exit``): ``child_process``.
    with child_process.private_directory("plx-doc-convert-") as directory:
        path = Path(directory) / f"source.{kind}"
        path.write_bytes(content)
        path.chmod(0o600)
        try:
            converter = backend.factory(enable_plugins=False)
            result = converter.convert_local(str(path.resolve()))
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except BaseException as error:
            raise ConversionFailed(f"markitdown_convert_error:{type(error).__name__}") from error
    text = getattr(result, "markdown", None) or getattr(result, "text_content", None)
    if text is None:
        raise ConversionFailed("markitdown_returned_no_text")
    text = str(text)
    spans = (_pdf_spans(text, page_count) if kind == "pdf" else
             _slide_spans(text) if kind == "pptx" else ())
    return Conversion(kind, text, backend.version, sha256(content).hexdigest(), spans)


def convert_stdlib(content: bytes, kind: str) -> Conversion:
    """Convert DOCX/PPTX bytes in memory without MarkItDown. Raises ``ConversionFailed``
    with the explicit reason (not the format, encrypted, malformed, over a bound)."""
    from .office_text import OfficeFormatError, docx_markdown, pptx_markdown
    if kind not in ("docx", "pptx"):
        raise ValueError("unsupported_conversion_kind")
    try:
        text, warnings = (docx_markdown if kind == "docx" else pptx_markdown)(content)
    except OfficeFormatError as error:
        raise ConversionFailed(str(error)) from error
    return Conversion(kind, text, STDLIB_VERSION, sha256(content).hexdigest(),
                      _slide_spans(text) if kind == "pptx" else (), STDLIB_CONVERTER,
                      warnings)


_HEX64 = re.compile(r"[0-9a-f]{64}")
_RECORD_KEYS = {"label", "converter", "converter_version", "upstream", "options",
                "options_sha256", "format", "source_sha256", "output_sha256", "spans"}
MAX_SPANS = 500


def _valid_conversion(record: Any, text: str) -> bool:
    """One machine-converted record whose output hash and spans match ``text``."""
    if (not isinstance(record, dict) or set(record) != _RECORD_KEYS or
            record["label"] != LABEL or record["converter"] not in _PROFILES or
            not isinstance(record["converter_version"], str) or
            not 0 < len(record["converter_version"]) <= 64 or
            record["upstream"] != UPSTREAM or
            [record["options"], record["options_sha256"]] !=
            list(_PROFILES[record["converter"]]) or
            record["format"] not in _SUFFIXES.values() or
            (record["converter"] == STDLIB_CONVERTER and record["format"] == "pdf") or
            not isinstance(record["source_sha256"], str) or
            not _HEX64.fullmatch(record["source_sha256"]) or
            record["output_sha256"] != sha256(text.encode("utf-8")).hexdigest() or
            not isinstance(record["spans"], list) or len(record["spans"]) > MAX_SPANS):
        return False
    return all(isinstance(span, dict) and set(span) == {"unit", "number", "start", "end"} and
               span["unit"] in {"page", "slide"} and
               all(type(span[key]) is int for key in ("number", "start", "end")) and
               span["number"] >= 1 and 0 <= span["start"] <= span["end"] <= len(text)
               for span in record["spans"])


def valid_record(record: Any, *, text: str, source_sha256: str) -> bool:
    """Check a stored conversion record against the stored text and original hash.

    A record must name the document's own original hash. A message's text never
    holds converted attachment text (its attachments are container members), so a
    record of converted sections inside other text is refused.
    """
    if not isinstance(text, str) or not isinstance(record, dict):
        return False
    return (record.get("label") == LABEL and record.get("source_sha256") == source_sha256
            and _valid_conversion(record, text))


__all__ = ["Backend", "CONVERTER", "Conversion", "ConversionFailed", "LABEL",
           "OPTIONS", "OPTIONS_SHA256", "STDLIB_CONVERTER", "STDLIB_OPTIONS",
           "STDLIB_VERSION", "UPSTREAM", "available", "convert", "convert_stdlib",
           "convertible_kind", "installed_backend", "valid_record"]
