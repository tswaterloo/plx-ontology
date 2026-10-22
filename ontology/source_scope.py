"""``ontology/source_scope.json``: the registry entries and ``<system>.<kind>`` scopes the
Workbench decided not to capture, each with its decided ``docs/decisions`` record. Read
by the source registry (``scripts/source_registry.py``) and the MCP roles
(``ontology/generator/mcp.py``). Here, not under ``scripts/``, so the ontology never imports
scripts."""

from __future__ import annotations

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SCOPE_PATH = ROOT / "ontology" / "source_scope.json"
SCOPE_FORMAT = "plx-source-scope/v1"
SCOPE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z][a-z_]*(?::.+)?$")
_DECISION_PATH = re.compile(r"^docs/decisions/[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
DECIDED = re.compile(r"^[-*\s]*status:\s*decided\b", re.M)


class RegistryError(Exception):
    """A source registry input (the scope here, the registry's own in its script) is refused."""


def load_scope(root: Path = ROOT) -> dict[str, str]:
    """``ontology/source_scope.json`` under ``root``: scope (``<system>.<kind>`` or an entry
    id) -> the decided ``docs/decisions`` record that puts it out of scope. An absent file
    is no scope; anything malformed, or a decision file that is missing under ``root`` or not
    ``status: decided``, is refused."""
    path = root / SCOPE_PATH.relative_to(ROOT)
    if not path.is_file():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    scopes = doc.get("out_of_scope") if isinstance(doc, dict) else None
    if not isinstance(scopes, dict) or doc.get("format") != SCOPE_FORMAT or \
            set(doc) - {"format", "_usage", "out_of_scope"}:
        raise RegistryError(f"{SCOPE_PATH.name}: not {SCOPE_FORMAT} "
                            "({format, _usage, out_of_scope: {scope: decision}})")
    for scope, decision in scopes.items():
        if not SCOPE_KEY.match(scope):
            raise RegistryError(f"{SCOPE_PATH.name}: {scope!r} is neither <system>.<kind> "
                                "nor an entry id")
        if not isinstance(decision, str) or not _DECISION_PATH.match(decision):
            raise RegistryError(f"{SCOPE_PATH.name}: {scope}: {decision!r} is not a "
                                "docs/decisions/*.md path")
        record = root / decision
        if not record.is_file():
            raise RegistryError(f"{SCOPE_PATH.name}: {scope}: decision file {decision} missing")
        if not DECIDED.search(record.read_text(encoding="utf-8")):
            raise RegistryError(f"{SCOPE_PATH.name}: {scope}: {decision} is not status: decided")
    return dict(scopes)
