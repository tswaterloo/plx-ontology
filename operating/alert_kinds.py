"""The alert registry (``alert_kinds.json``): which alert types are on by default, which
reach My work, and which presentation kind each merges into.

Detectors never read it: every type still runs. The briefing stamps each item with its
kind and whether it is on by default and on the home screen; the app shows only ``home``
items on My work and the rest in Watching and on record pages. A reader's own switch
(``reader_on``) can turn an off type on for that reader in Watching, never onto My work.
The rule the registry obeys is enforced by ``operating/tests/test_alert_kinds.py``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

PATH = Path(__file__).with_name("alert_kinds.json")
BAR = 0.90
MIN_CHECKED = 15


@lru_cache(maxsize=1)
def registry() -> dict[str, Any]:
    return json.loads(PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _types() -> dict[str, Mapping[str, Any]]:
    return {t["type"]: t for t in registry()["types"]}


@lru_cache(maxsize=1)
def _kinds() -> dict[str, Mapping[str, Any]]:
    return {k["kind"]: k for k in registry()["kinds"]}


def entry(event_type: str) -> Mapping[str, Any] | None:
    return _types().get(event_type)


def kind_of(event_type: str) -> str | None:
    found = entry(event_type)
    return found["kind"] if found else None


def default_on(event_type: str) -> bool:
    """On by default; an unknown type is off (fail closed)."""
    found = entry(event_type)
    return bool(found and found["default"] == "on")


def home(event_type: str) -> bool:
    """Reaches My work; an unknown type never does."""
    found = entry(event_type)
    return bool(found and found["default"] == "on" and found["home"])


def shown(event_type: str, reader_on: Iterable[str] = ()) -> bool:
    """Listed in Watching for this reader: on by default, or switched on by them."""
    return default_on(event_type) or event_type in set(reader_on)


def kind_label(kind: str) -> str | None:
    found = _kinds().get(kind)
    return found["label"] if found else None


def surface(kind: str) -> str | None:
    found = _kinds().get(kind)
    return found["surface"] if found else None


def off_gap_kinds(enabled: Iterable[str] = ()) -> frozenset[str]:
    """The gap kinds of every type that is off in the registry and not switched on by the
    reader (``enabled``: type names): what no reader-facing read returns (briefing, digest,
    agent tools, alerts). A gap kind of no registered type is not an alert and is kept."""
    from .briefing import EVENT_TYPES
    on = set(enabled)
    out: set[str] = set()
    for t in registry()["types"]:
        if t["default"] == "on" or t["type"] in on:
            continue
        spec = EVENT_TYPES.get(t["type"])
        out.update(spec["kinds"] if spec else (t["type"],))
    # a gap kind shared with an on type stays readable
    for t in registry()["types"]:
        if t["default"] == "on" or t["type"] in on:
            spec = EVENT_TYPES.get(t["type"])
            out.difference_update(spec["kinds"] if spec else (t["type"],))
    return frozenset(out)


def passes_bar(precision: Mapping[str, Any] | None) -> bool:
    return bool(precision) and float(precision.get("value") or 0) >= BAR \
        and int(precision.get("checked") or 0) >= MIN_CHECKED


def public() -> dict[str, Any]:
    """The registry as the app reads it (Watching's per-kind list)."""
    return {"rule": registry()["rule"], "kinds": registry()["kinds"],
            "types": [{k: t.get(k) for k in ("type", "kind", "label", "precision",
                                              "actionable", "default", "home", "roles")}
                      for t in registry()["types"]]}
