"""A tools/call result's value as every in-process and stdio client reads it.

``MCPServer.handle`` sends a successful result twice, as its JSON text (``content``) and
as ``structuredContent``. A result that fits once but not twice is sent once, as its
text, with no ``structuredContent`` (a client that shows structuredContent in place of
the text, as Claude Code does, then shows the whole text) and ``TEXT_ONLY_STUB`` under
``_meta[TEXT_ONLY_KEY]``: a structured note that it was truncated and that the whole
result is the JSON text. ``result_value`` gives the value either way, so no client takes
a missing structuredContent (or the stub) for the result.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

TEXT_ONLY_KEY = "plx/result_in_text"
TEXT_ONLY_STUB: dict[str, Any] = {
    "truncated": True,
    "detail": ("structuredContent left out: the result is too large to send twice; "
               "the whole result is the JSON in content[0].text (read it there; to "
               "get a smaller result, page or narrow the call)")}


class ToolResultUnreadable(ValueError):
    """A text-only result whose text is no JSON value."""


def text_only(result: Mapping[str, Any]) -> bool:
    """Whether a tools/call result carries its value as its text alone."""
    meta = result.get("_meta")
    return "structuredContent" not in result or (
        isinstance(meta, Mapping) and isinstance(meta.get(TEXT_ONLY_KEY), Mapping))


def result_text(result: Mapping[str, Any]) -> list[str]:
    """A tools/call result's text parts."""
    return [str(part.get("text") or "") for part in result.get("content") or ()
            if isinstance(part, Mapping)]


def result_value(result: Mapping[str, Any]) -> Any:
    """A successful tools/call result's value: ``structuredContent``, or, sent as its text
    alone, the JSON of its text (its first text part when a client appended another, a
    note of its own). Raises ``ToolResultUnreadable`` when that text is no JSON."""
    if not text_only(result):
        return result["structuredContent"]
    parts = result_text(result)
    for text in ("".join(parts), parts[0] if parts else ""):
        try:
            return json.loads(text)
        except ValueError:
            continue
    raise ToolResultUnreadable("tool_result_unreadable")
