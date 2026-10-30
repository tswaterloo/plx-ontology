"""How a captured file's text is laid out by page: the page break between the pages of a
PDF's text, its split into pages, and which files are stored by page. Its own module so
the readers of captured files (``integrations.captured_text``) depend on nothing of the
projection for it; ``ontology.mentions`` re-exports these names. Stdlib only."""

from __future__ import annotations

from typing import Any, Mapping

PAGE_BREAK = "\f"


def text_pages(text: str) -> list[str]:
    return text.split(PAGE_BREAK)


def is_paged_file(record: Mapping[str, Any]) -> bool:
    """A captured file whose text is stored by page: a PDF, by MIME type or name (the
    test ``integrations.document_understanding.extract_document`` applies)."""
    mime = str(record.get("mime_type") or "").split(";", 1)[0].strip().lower()
    name = str(record.get("name") or "").lower()
    return mime == "application/pdf" or (mime in ("", "application/octet-stream")
                                         and name.endswith(".pdf"))


__all__ = ["PAGE_BREAK", "is_paged_file", "text_pages"]
