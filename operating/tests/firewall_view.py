"""Test helper: read an MCP tool result as a client sees it, through the output boundary.

``client_result(name, result)`` asserts the boundary held (no string of a case tool's
result outside an ``untrusted_text`` envelope unless it is a typed field or a typed
cell of a row envelope,
``injection_guard.untyped_strings``) and returns the result with its envelopes read back
to text, so tool-semantics tests keep asserting values while every call also checks the
firewall. Results of the self-enveloping tools (their own contract envelopes) and errors
are returned as they are.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from operating import injection_guard as guard
from operating.mcp_server import SELF_ENVELOPING_TOOLS


def as_text(value: Any) -> Any:
    """A wrapped value with every envelope read back to its text and every row envelope
    back to its row."""
    if guard.is_envelope(value):
        return value["untrusted_text"]
    if guard.is_row_envelope(value):
        return {column: _cell_text(cell) for column, cell in value["cells"].items()}
    if isinstance(value, dict):
        return {key: as_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [as_text(item) for item in value]
    return value


def _cell_text(cell: Any) -> Any:
    """A row envelope cell read back: a JSON cell (an object or array the boundary
    serialized compactly and marked whole) back to its value, any other marker to text."""
    if not guard.is_envelope(cell):
        return cell
    text = cell["untrusted_text"]
    if text[:1] in "{[" and not cell.get("truncated"):
        try:
            value = json.loads(text)
        except ValueError:
            return text
        if json.dumps(value, ensure_ascii=False, separators=(",", ":")) == text:
            return value
    return text


def client_result(name: str, result: Any) -> Any:
    if (not isinstance(result, dict) or result.get("isError")
            or "structuredContent" not in result or name in SELF_ENVELOPING_TOOLS):
        return result
    value = result["structuredContent"]
    untyped = guard.untyped_strings(value)
    if untyped:
        raise AssertionError(f"{name}: strings outside the boundary: {untyped[:10]}")
    out = copy.copy(result)
    out["structuredContent"] = as_text(value)
    return out
