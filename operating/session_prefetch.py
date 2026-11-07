"""The reads an agent session starts with: the records the question names, found and read
before the session's first turn (``agent_session.HostGate`` prefetch), whatever the
question asks about them.

Nothing here reads what a question asks. The question's identifiers come from the one
identifier grammar (``query_understanding.identifier_leads``) and its names and codes
from the resolver (``plx.find`` mode ``entities``); every record found as the one record
of a mention is then read the same way, by what the record is, never by what was asked:
its card (``plx.object``: every source's values with citations, conflicts, identity
edges and linked counts per link type) and, for a record a declared process runs on
(``ontology/schema/process``), its process (``plx.process``: stages, the next stage,
overdue stages, quantity checks, conflicts). The session sees every candidate and
chooses; an ambiguous mention is left to it. Reads only, as the reader.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

MAX_MENTIONS = 6     # identifiers of one question looked up first
MAX_ROOTS = 4        # records read in the follow-up round
MAX_PER_ENTITY = 2   # records of one resolved name read (the same thing in two systems)
PROCESS_DIR = Path(__file__).resolve().parents[1] / "ontology" / "schema" / "process"


@functools.lru_cache(maxsize=1)
def process_root_types() -> frozenset[str]:
    """The object types a declared process runs on (each graph's ``root_type``)."""
    out: set[str] = set()
    for path in PROCESS_DIR.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            root = json.loads(path.read_text(encoding="utf-8")).get("root_type")
        except (OSError, ValueError):
            continue
        if isinstance(root, str) and root:
            out.add(root)
    return frozenset(out)


def first_reads(question: str) -> list[tuple[str, dict[str, Any]]]:
    """The records ``question`` names, looked up as the reader: each identifier the
    grammar reads (typed when its label says the type) and the question's names and
    codes resolved; [] for an empty question."""
    from . import query_understanding as qu
    if not isinstance(question, str) or not question.strip():
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        leads = qu.identifier_leads(question)
    except Exception:  # noqa: BLE001 - unread: the resolver still looks
        leads = []
    seen: set[str] = set()
    for lead in leads:
        key = (lead.forms[0] if lead.forms else lead.text).upper()
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > MAX_MENTIONS:
            break
        find: dict[str, Any] = {"query": lead.text[:200], "limit": 10}
        if lead.types and len(lead.types) == 1:
            find["object_type"] = lead.types[0]
        out.append(("plx.find", find))
    out.append(("plx.find", {"mode": "entities", "text": question[:2000]}))
    return out


def _roots(result: Any) -> list[tuple[str, str]]:
    """(object_id, object_type) of the records a find result found as the one record of
    its mention: a find with one candidate, or each name the resolver matched."""
    if not isinstance(result, Mapping):
        return []
    out: list[tuple[str, str]] = []
    candidates = [c.get("object") for c in result.get("candidates") or ()
                  if isinstance(c, Mapping) and isinstance(c.get("object"), Mapping)]
    if result.get("status") == "found" and len(candidates) == 1:
        out.append((candidates[0].get("object_id"), candidates[0].get("object_type")))
    for entity in result.get("entities") or ():
        if not isinstance(entity, Mapping) or entity.get("status") != "match":
            continue
        records = [r.get("object") for r in entity.get("records") or ()
                   if isinstance(r, Mapping) and r.get("status") == "match"
                   and isinstance(r.get("object"), Mapping)]
        out += [(r.get("object_id"), r.get("object_type") or entity.get("object_type"))
                for r in records[:MAX_PER_ENTITY]]
    return [(oid, kind) for oid, kind in out if isinstance(oid, str) and oid]


def follow_reads(done: Sequence[tuple[str, Mapping[str, Any], Any]]
                 ) -> list[tuple[str, dict[str, Any]]]:
    """Given the first reads (each read's name, arguments and result), each record found
    read the same way (module doc), at most ``MAX_ROOTS``; [] when none was found."""
    roots: dict[str, str] = {}
    for name, _arguments, result in done:
        if name == "plx.find":
            for oid, kind in _roots(result):
                roots.setdefault(oid, kind or "")
    out: list[tuple[str, dict[str, Any]]] = []
    processes = process_root_types()
    for oid, kind in list(roots.items())[:MAX_ROOTS]:
        out.append(("plx.object", {"object_id": oid}))
        if kind in processes:
            out.append(("plx.process", {"from": oid}))
    return out


def root_reads(roots: Sequence[Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """The first reads of a question whose records the Ask already found (the lookup it
    made while the front door read the question, ``operator_api.preread``): each record
    and its copies in other systems read the way ``follow_reads`` reads a found record,
    at most ``MAX_ROOTS``, with no lookup made again; [] when none was found. ``roots``:
    mappings with ``object_id``, optionally ``object_type`` and ``same_as`` (ids)."""
    found: dict[str, str] = {}
    for root in roots:
        if not isinstance(root, Mapping):
            continue
        ids = [root.get("object_id"), *(root.get("same_as") or ())]
        for n, oid in enumerate(ids):
            if isinstance(oid, str) and oid:
                kind = root.get("object_type") if n == 0 else None
                found.setdefault(oid, str(kind or oid.split(":", 1)[0]))
    out: list[tuple[str, dict[str, Any]]] = []
    processes = process_root_types()
    for oid, kind in list(found.items())[:MAX_ROOTS]:
        out.append(("plx.object", {"object_id": oid}))
        if kind in processes:
            out.append(("plx.process", {"from": oid}))
    return out
