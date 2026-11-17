"""Evidence helpers of the case recorder: cited-row texts, document handles, shared values and
link paths (``operating.case_workspace_live``, which re-exports them)."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable, Collection, Mapping, Sequence

from .case_workspace_live_model import OpenError


_BARE_NUMBER = re.compile(r"(?<![\w.:/#@+-])[+-]?\d+(?:[.,]\d+)*(?![\w.:/#@%+-])")
# An ISO date or timestamp in uncited prose: no claim a row holds either (claim_checks).
_BARE_DATE = re.compile(r"(?<![\w.:/#@+-])\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}[\d:.]*"
                        r"(?:Z|[+-]\d{2}(?::?\d{2})?)?)?(?![\w/#@%-])")


def _document_types() -> set[str]:
    """The object types projected from a document's machine-read fields."""
    from ontology.document_fields import profiles
    return {profile["object_type"] for profile in profiles()["profiles"]}


def _l1_handle(value: Mapping[str, Any]) -> str | None:
    cite = value.get("citation")
    handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
    return handle if isinstance(handle, str) and handle else None


def _unnumbered(text: str, dates: Collection[str] = ()) -> str:
    """``text`` with every bare number and ISO date written as "#", except the dates in
    ``dates`` (the values of the item's own date claims, which the claim gate checks)."""
    return _BARE_NUMBER.sub("#", _BARE_DATE.sub(
        lambda match: match.group(0) if match.group(0) in dates else "#", str(text)))


def _code(result: Mapping[str, Any]) -> str:
    error = result.get("error")
    code = error.get("code") if isinstance(error, Mapping) else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,60}", code) else "error"


def _evidence_reached(answer: Mapping[str, Any]) -> list[str]:
    """The answer chain's objects joined to its root by evidence-tier links: an object
    reached only through lead-tier links is a lead, never the basis of an answer.

    Limitation, failing closed: ``plx.investigate`` exposes its root only for a single
    root (``answer["root"]``) and no root collection ids otherwise, so with several roots,
    or a root not among the chain's objects, nothing is reached and the answer's
    fallback cites only its own entries' citations (fewer citations, never a lead's)."""
    from .ontology_tools_investigate_plans import _EVIDENCE_TIERS
    chain = answer.get("chain") if isinstance(answer.get("chain"), Mapping) else {}
    linked: dict[Any, set[Any]] = {}
    for edge in chain.get("edges") or ():
        if isinstance(edge, Mapping) and edge.get("tier") in _EVIDENCE_TIERS:
            linked.setdefault(edge.get("from"), set()).add(edge.get("to"))
            linked.setdefault(edge.get("to"), set()).add(edge.get("from"))
    root = answer.get("root") if isinstance(answer.get("root"), Mapping) else {}
    seen = {root.get("object_id")}
    frontier = list(seen)
    while frontier:
        for other in linked.get(frontier.pop(), ()):
            if other not in seen:
                seen.add(other)
                frontier.append(other)
    return [ref["object_id"] for ref in chain.get("objects") or ()
            if isinstance(ref, Mapping) and ref.get("object_id") in seen]


def _rooted(candidate: Mapping[str, Any], identifier: str) -> Mapping[str, Any]:
    """The object a lone plx.find candidate roots. A candidate matched on a kind unique
    only within a scope (a customer PO) roots only when it passes the one root gate
    (``ontology_tools.roots_exactly``, read through ``scoped_root_lead``: a root reason or
    a linked record, its one scope and the identifier's literal text); else the request is
    refused ``root_lead_only:<why>``, never opened on a lead. Other kinds root as plx.find
    answers them."""
    from .ontology_tools import default_grammar
    from .ontology_tools_identity import scoped_root_lead

    why = scoped_root_lead(candidate, identifier, default_grammar())
    if why is not None:
        raise OpenError(f"root_lead_only:{why}")
    return candidate["object"]


def _one_record(call: Callable[[str, dict], dict], candidates: Sequence[Mapping[str, Any]]
                ) -> bool:
    """True when every candidate is one record held in several systems: the candidates
    are joined to one another by same-record identity links the tools report at a
    verified or reviewed tier (``plx.object`` ``identity``, read by
    ``ontology_tools.same_party``). Then the first ranked roots the case and the plans
    reach the others as its twins; anything less stays ambiguous."""
    from .ontology_tools import same_party

    ids = [c.get("object_id") for c in candidates]
    if len(ids) < 2 or len(set(ids)) != len(ids):
        return False
    edges: list[tuple[Any, Any, Any]] = []
    for object_id in ids:
        card = call("plx.object", {"object_id": object_id})
        if "error" in card:
            return False
        edges += [(edge["from"], edge["to"], edge.get("tier"))
                  for edge in card.get("identity") or []]
    return all(same_party(ids[0], other, edges) is True for other in ids[1:])


def _shared_values(db: sqlite3.Connection, object_type: str, linked: str,
                   carriers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Which held ``object_type`` records carry, at a join carrier's ``field``, a value a
    held ``linked`` record holds at that carrier's ``to_field`` (the reverse direction of a
    join pick: the two populations intersected by value, however old either record is).
    Only carriers that compare the two records directly (no ``then`` hop) are read; a
    value naming a row of a prefix-setting database (``UAT-``) is skipped, as ``_join``
    never searches it. Values are compared trimmed and case-folded: a candidate only,
    which ``_join`` then decides through the tools by the owning system's literal rule.
    ``{"start_held", "linked_held", "linked_matched", "start": [object ids],
    "start_scoped": [object ids]}``: counts of the projection's records carrying any such
    value (under the relation grants the caller filtered ``carriers`` by, never a row's
    own ACL: a candidate the investigator may not read is ``root_not_readable`` when it is
    tried), and the start records that share one through a carrier unique on its own
    (``start``) or only through a scoped one (``start_scoped``: a customer PO, where a
    common text such as a verbal-order marker matches many customers' orders, so these are
    tried after every order-number match)."""
    from ontology import identifiers
    from ontology.store import removed_at_sql

    from .ontology_tools import default_grammar
    from .ontology_tools_identity import scoped_kind
    from .ontology_tools_results import _column_path

    grammar = default_grammar()
    pairs = {(c["from_binding"], c["field"], c["to_binding"], c["to_field"])
             for c in carriers if c.get("then") is None and c.get("find_type") == linked}
    scoped = {(c["from_binding"], c["field"], c["to_binding"], c["to_field"])
              for c in carriers if c.get("then") is None and c.get("find_type") == linked
              and scoped_kind(grammar, c["identifier_kind"])}

    def load(type_id: str, fields: dict[str, set[str]]) -> dict[str, set[tuple[str, str, str]]]:
        out: dict[str, set[tuple[str, str, str]]] = {}
        if not fields:
            return out
        for object_id, binding, path, value in db.execute(
                "SELECT v.object_id, v.binding_id, v.field_path, v.value FROM object_values v "
                "JOIN objects o ON o.object_id = v.object_id WHERE o.object_type = :type "
                "AND v.superseded_at IS NULL AND v.value IS NOT NULL "
                "AND v.binding_id IN (SELECT value FROM json_each(:bindings)) "
                f"AND NOT {removed_at_sql('o.object_id')}",
                {"type": type_id, "bindings": json.dumps(sorted(fields))}):
            column, text = _column_path(str(path)), str(value).strip()
            if column in fields.get(binding, ()) and text \
                    and not identifiers.environment_prefix(text):
                out.setdefault(object_id, set()).add((binding, column, text.casefold()))
        return out

    starts: dict[str, set[str]] = {}
    ends: dict[str, set[str]] = {}
    for start_binding, field, end_binding, end_field in pairs:
        starts.setdefault(start_binding, set()).add(field)
        ends.setdefault(end_binding, set()).add(end_field)
    held = load(object_type, starts)
    targets = load(linked, ends)
    index: dict[tuple[str, str, str], set[str]] = {}
    for object_id, carried in targets.items():
        for key in carried:
            index.setdefault(key, set()).add(object_id)
    start: set[str] = set()
    start_scoped: set[str] = set()
    matched: set[str] = set()
    for object_id, carried in held.items():
        for binding, column, text in carried:
            for pair in {p for p in pairs if p[:2] == (binding, column)}:
                found = index.get((pair[2], pair[3], text))
                if found:
                    (start_scoped if pair in scoped else start).add(object_id)
                    matched |= found
    return {"start_held": len(held), "linked_held": len(targets),
            "linked_matched": len(matched), "start": sorted(start),
            "start_scoped": sorted(start_scoped - start)}


def _held(db: sqlite3.Connection, object_type: str) -> bool:
    """Whether the projection still holds an object of ``object_type`` (no open removal)."""
    from ontology.store import removed_at_sql
    return db.execute("SELECT 1 FROM objects o WHERE o.object_type = ? AND NOT "
                      f"{removed_at_sql('o.object_id')} LIMIT 1", (object_type,)
                      ).fetchone() is not None


def _freshness(db: sqlite3.Connection) -> dict[str, Any]:
    """The projection's ``store_meta.source_freshness`` (source -> relation -> facts); {}
    when absent or unreadable."""
    row = db.execute("SELECT value FROM store_meta WHERE key = 'source_freshness'").fetchone()
    try:
        freshness = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        freshness = {}
    return freshness if isinstance(freshness, dict) else {}


def _link_paths(db: sqlite3.Connection, object_id: str, linked: str
                ) -> list[list[dict[str, str]]]:
    """``plx.around`` paths (one or two links, schema directions) from ``object_id`` to an
    object of type ``linked``, as the store's links run."""
    def hops(source: str, end_type: str | None = None) -> list[tuple[str, str, str]]:
        """Links at ``source`` (to an object of ``end_type`` only, when given)."""
        return [(row[0], row[1], row[2]) for row in db.execute(
            "SELECT l.link_type, CASE WHEN l.from_object = :at THEN 'forward' ELSE 'reverse' "
            "END, o.object_id FROM links l JOIN objects o ON o.object_id = CASE WHEN "
            "l.from_object = :at THEN l.to_object ELSE l.from_object END WHERE "
            "(l.from_object = :at OR l.to_object = :at) AND (:end IS NULL OR "
            "o.object_type = :end) LIMIT 200", {"at": source, "end": end_type})]

    def type_of(oid: str) -> str | None:
        row = db.execute("SELECT object_type FROM objects WHERE object_id = ?",
                         (oid,)).fetchone()
        return row[0] if row else None

    paths: list[list[dict[str, str]]] = []
    for link, direction, middle in hops(object_id, linked) + hops(object_id):
        first = {"link_type": link, "direction": direction}
        if type_of(middle) == linked:
            paths.append([first])
            continue
        for link2, direction2, end in hops(middle, linked):
            if end != object_id:
                paths.append([first, {"link_type": link2, "direction": direction2}])
    unique: list[list[dict[str, str]]] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique[:4]


def _reaches(call: Callable[[str, dict], dict], object_id: str,
             path: list[dict[str, str]]) -> tuple[str | None, list[str]]:
    """How ``plx.around`` reaches a readable object along ``path``: ``evidence`` at its
    default tier floor, ``lead`` only when asked again at lead tier by the plans' own rule
    (``ontology_tools_investigate.ask_again``: the tool said the path's links lie below its
    floor, as a machine-read document reference does), else None; with the ids of the
    objects it reached."""
    from .ontology_tools_investigate_plans import LEAD_RETRY, ask_again

    first, found = ask_again(lambda args: call("plx.around", args),
                             {"from": object_id, "path": path, "limit": 1}, LEAD_RETRY,
                             lambda answer: not answer.get("objects"))
    if not found.get("objects"):
        return None, []
    reached = [str(o["object_id"]) for o in found["objects"]
               if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)]
    return ("evidence" if found is first else "lead"), reached
