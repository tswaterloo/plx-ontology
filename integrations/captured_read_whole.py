"""Whole-file reading for the Spark document reader (kept apart from ``captured_text``,
which is step code: this module is not part of any step's digest)."""
from __future__ import annotations

from typing import Any, Mapping

from . import captured_text as ct


def read_whole(content: bytes, record: Mapping[str, Any]) -> tuple[str, str | None]:
    """(text status, the whole text) of a file of any size or page count, read by the
    extractor's own page-by-page readers with no byte, page or text bound and no OCR (a PDF
    page without a text layer leaves it ``ocr_unavailable``, the text layer kept). For the
    Spark document reader, which reads what the box's bounded extractor left unread
    (``TEXT_EXCEEDS_BOUND``); the zip-bomb bound of the office readers still applies."""
    from .doc_convert import convertible_kind
    from .document_understanding import is_ooxml_workbook, is_plain_text, read_pages
    from .legacy_office import extract_xls_text, is_misnamed_xlsx, is_xls
    from .xlsx_extract import extract_xlsx_text

    mime, name = ct._mime(record), ct._name(record)
    kind = mime.split(";", 1)[0].strip().lower()
    readable = convertible_kind(kind, name)
    blank = False
    try:
        if readable is not None:
            found = read_pages(content, readable, max_text=None, max_pages=None)
            if found.status != "extracted" or any(page.unread for page in found.pages):
                return "failed", None
            text = ct._joined(found.text, found.spans) if readable == "pdf" else found.text
            blank = readable == "pdf" and any(
                not found.text[p["start"]:p["end"]].strip() for p in found.spans)
        elif is_ooxml_workbook(kind, name) or is_xls(kind, name, content) or \
                is_misnamed_xlsx(kind, name, content):
            text, status, *_ = (extract_xls_text if is_xls(kind, name, content)
                                else extract_xlsx_text)(content, max_text=ct._WORKBOOK_TEXT)
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
    return (ct.OCR_UNAVAILABLE if blank else ct.TEXT_EXTRACTED), text
