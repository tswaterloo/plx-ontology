"""Who can answer a gap: one resolver, Portal Department codes.

Every ``who_can_answer`` the Workbench writes (a ``plx.investigate`` gap and the follow-up
``record_follow_ups`` makes of it, the action path's MC escalation follow-up, goal 2's
follow-ups) is a Portal Department code or a typed ``undeclared:`` label, never free text:

* the codes are the process format's ``owner_role`` enum
  (``ontology/schema/process/_process.schema.json``), faithful to
  plx-customer-portal@48d76e835 portal/src/lib/policies/seed-rules.ts:19-38
  ROUTING_DEPARTMENTS, the departments Portal's work routing targets;
* a record's answerer is the ``owner_role`` of each process stage its object type evidences
  (``operating.ontology_tools_process.load_graphs``), narrowed to the stages whose evidence
  names its binding, then its property, when any does;
* a type no stage evidences, or a relation no object type binds, is
  ``undeclared:<source owner>`` (e.g. ``undeclared:Portal owner of public.app_receipt``):
  the graph declares no department for it, and nothing guesses one;
* an answerer someone else named (an engine) stays only when it is a code or already
  typed; any other label becomes ``undeclared:<label>``.

A process graph directory that does not load declares no stage: every answer is then
undeclared (fail closed, never a guessed department)."""

from __future__ import annotations

from functools import lru_cache
import json
from typing import Any, Iterable, Sequence

UNDECLARED = "undeclared:"
MAX_LABEL = 200  # the gap contract's who_can_answer item maxLength


@lru_cache(maxsize=1)
def departments() -> frozenset[str]:
    """The Portal Department codes (ROUTING_DEPARTMENTS, the process format's owner_role)."""
    from .ontology_tools_process import FORMAT_PATH
    fmt = json.loads(FORMAT_PATH.read_text(encoding="utf-8"))
    return frozenset(fmt["$defs"]["owner_role"]["enum"])


def _stamp() -> tuple:
    from .ontology_tools_process import PROCESS_DIR
    try:
        return tuple(sorted((path.name, stat.st_mtime_ns, stat.st_size)
                            for path in PROCESS_DIR.glob("*.json") for stat in (path.stat(),)))
    except OSError:
        return ()


@lru_cache(maxsize=1)
def _stage_index(stamp: tuple) -> dict[str, list[tuple[str, frozenset[str] | None, str | None]]]:
    """object type -> ``(owner_role, bindings or None for any, predicate property)`` per
    evidence spec, in graph and stage order; empty when the graphs do not load."""
    from .ontology_tools_process import PROCESS_DIR, load_graphs
    try:
        graphs = load_graphs(PROCESS_DIR) if stamp else {}
    except (OSError, ValueError):   # ProcessGraphError is a ValueError
        return {}
    index: dict[str, list[tuple[str, frozenset[str] | None, str | None]]] = {}
    for graph in graphs.values():
        for stage in graph["stages"]:
            for spec in stage.get("evidence") or ():
                bindings = spec.get("bindings")
                index.setdefault(spec["object_type"], []).append(
                    (stage["owner_role"], frozenset(bindings) if bindings else None,
                     (spec.get("predicate") or {}).get("property")))
    return index


def stage_owners(type_id: str, prop: str = "", binding: str = "") -> list[str]:
    """The owner_role of every stage evidenced by ``type_id`` (its ``binding``'s stages and
    then those that read ``prop``, one of ``a|b``, when any stage does), in graph order."""
    specs = _stage_index(_stamp()).get(type_id) or []
    if binding:
        specs = [s for s in specs if s[1] is None or binding in s[1]] or specs
    if prop:
        props = set(prop.split("|"))
        specs = [s for s in specs if s[2] in props] or specs
    return list(dict.fromkeys(s[0] for s in specs if s[0] in departments()))


def typed(label: str) -> str:
    """``label`` as an answerer: a Department code or an ``undeclared:`` label as it is,
    anything else typed undeclared."""
    text = str(label).strip()
    if text in departments() or (text.startswith(UNDECLARED) and len(text) > len(UNDECLARED)):
        return text[:MAX_LABEL]
    return (UNDECLARED + (text or "unnamed"))[:MAX_LABEL]


def resolve(subjects: Iterable[Sequence[str]] = (), owners: Iterable[str] = (),
            named: Iterable[str] = ()) -> list[str]:
    """Who can answer: ``named`` (an engine's answerers) typed when given; else the stage
    owners of ``subjects`` (``(type, property, binding)``); else every source owner of
    ``owners`` typed undeclared; else nothing (the caller decides its fallback)."""
    found = [typed(name) for name in named]
    if not found:
        found = [code for subject in subjects for code in stage_owners(*subject)]
    if not found:
        found = [typed(owner) for owner in owners]
    return list(dict.fromkeys(found))


def for_citation(handle: Any, system: str = "") -> list[str]:
    """Who can answer for the record ``handle`` cites (``api://<relation>/...``, a BC
    ``bc://<environment>/<company>/<entity set>/...`` handle, any other scheme):
    the stage owners of the object types bound to that relation (``ontology_tools.shared_schema``),
    else ``undeclared:<system> owner of <relation>``. ``system`` names the owning system
    when the handle's scheme does not."""
    from .explanations import SOURCE_SYSTEMS
    from .ontology_tools import shared_schema
    text = str(handle or "")
    scheme, sep, body = text.partition("://")
    if not sep:
        return [typed(f"{system or 'source'} owner of an uncited record")]
    parts = body.split("@", 1)[0].split("/")
    relation = (parts[2] if len(parts) == 4 else "") if scheme == "bc" else parts[0]
    source = {"api": "portal_api", "bc": "bc"}.get(scheme)
    schema = shared_schema()
    subjects = [(type_id, "", binding["id"])
                for type_id, spec in (schema.object_types if schema is not None else {}).items()
                for binding in spec.get("bindings") or ()
                if source and relation and binding.get("source") == source
                and binding.get("relation") == relation]
    owner = f"{SOURCE_SYSTEMS.get(scheme) or system or scheme} owner of {relation or text}"
    return resolve(subjects, [owner])


__all__ = ["MAX_LABEL", "UNDECLARED", "departments", "for_citation", "resolve",
           "stage_owners", "typed"]
