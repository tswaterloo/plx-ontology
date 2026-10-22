"""An MCP tool registration in upstream TypeScript (``server.tool("name", ...)`` or
``server.registerTool("name", ...)``): the one pattern the source registry
(``scripts/source_registry.py``), the MCP pin (``ontology/generator/mcp.py``) and the
live-wiring test read. Here, not under ``scripts/``, so the ontology never imports scripts."""

from __future__ import annotations

import re

TOOL_REGISTRATION = r"""\.(?:tool|registerTool)\(\s*["'`]({name})["'`]"""


def tool_registration(name: str = r"[A-Za-z_][A-Za-z0-9_]*") -> re.Pattern:
    """The registration of a tool whose name matches ``name`` (a pattern), captured."""
    return re.compile(TOOL_REGISTRATION.format(name=name))
