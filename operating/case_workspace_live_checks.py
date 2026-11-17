"""Workspace structure, relationship, leak and summary checks over an opened case
(``operating.case_workspace_live``, which re-exports them)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .case_workspace_live_model import (
    CARD_IDENTITY,
    CARD_LINK_TYPES,
    LOWEST_TIER,
    MAP_OBJECTS,
    MAP_PAGE,
    MAP_PAGES,
    MIN_DIGIT_CHARS,
    MIN_LEAK_CHARS,
    MIRROR_EVIDENCE,
)
from .case_workspace_live_projection import _row_texts
from .case_workspace_live_evidence import _code


def _audience_digest(audience: Any) -> str | None:
    """SHA-256 of a mirror ``AudienceMap``'s grants and classifications (its content,
    whichever file it came from), or None for another ``allows`` object."""
    from hashlib import sha256

    grants = getattr(audience, "_audiences", None)
    levels = getattr(audience, "_classifications", None)
    if not isinstance(grants, Mapping) or not isinstance(levels, Mapping):
        return None
    return sha256(json.dumps({
        "tenant": getattr(audience, "tenant", None),
        "default_classification": getattr(audience, "default_classification", None),
        "audiences": {pattern: sorted(principals) for pattern, principals in grants.items()},
        "classifications": dict(levels)}, sort_keys=True).encode("utf-8")).hexdigest()


def _decisions(state_dir: Path, tenant: str) -> dict[str, list[int]] | None:
    """The state of ``tenant``'s decisions the projection is built from: per store (the
    review store, the confirmation store; both append-only), its last sequence number and
    row count, ``[]`` when the store is not there. Read-only; None when a store is there
    but cannot be read."""
    from ontology import reviews

    from . import kind_confirmation

    out: dict[str, list[int]] = {}
    for name, path, table in (("reviews", reviews.store_path(state_dir), "reviews"),
                              ("confirmations", kind_confirmation.store_path(state_dir),
                               "confirmation")):
        if not path.is_file():
            out[name] = []
            continue
        try:
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                                    uri=True, timeout=5)) as db:
                row = db.execute(f"SELECT MAX(seq), COUNT(*) FROM {table} WHERE tenant = ?",
                                 (tenant,)).fetchone()
        except sqlite3.Error:
            return None
        out[name] = [row[0] or 0, row[1]]
    return out


# ------------------------------------------------------------------ render


def workspace(runtime: Any, reader: Any, case_id: str, *, evidence: Mapping[str, Any] | None = None,
              action_path: Any = None, ingestion: Any = None) -> dict[str, Any] | None:
    """The console workspace of one case for ``reader``, or None when not readable."""
    from operator_api.workspace.case_workspace import CaseWorkspace

    view = CaseWorkspace(runtime, reader, evidence=evidence or MIRROR_EVIDENCE,
                         action_path=action_path, ingestion=ingestion)
    try:
        return view.case(case_id)
    except KeyError:
        return None


def _count(items: Any) -> int:
    return len(items) if isinstance(items, list) else 0


def panel_totals(cases: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Readers whose rendered case shows every panel, and readers by evaluation status."""
    readers = [r for e in cases for r in e.get("readers", []) if r.get("status") == "read"]
    by_status: dict[str, int] = {}
    for reader in readers:
        if reader.get("evaluation") is not None:
            status = str(reader["evaluation"])
            by_status[status] = by_status.get(status, 0) + 1
    return {"converged_readers": sum(1 for r in readers if r.get("converged")),
            "evaluation_by_status": dict(sorted(by_status.items()))}


def _resolved_handles(node: Any, out: set[str]) -> set[str]:
    """Every citation handle a rendered workspace marks ``resolved`` for its reader."""
    if isinstance(node, Mapping):
        if isinstance(node.get("handle"), str) and node.get("status") == "resolved":
            out.add(node["handle"])
        for value in node.values():
            _resolved_handles(value, out)
    elif isinstance(node, list):
        for value in node:
            _resolved_handles(value, out)
    return out


def _versions_citing(projection: Path, handles: Sequence[str]) -> list[str] | None:
    """Ids of the projection objects with a captured version under one of ``handles``
    (sorted), or None when the projection cannot be read. Ids only: whether a reader may
    read one is that reader's ``plx.object`` answer."""
    if not projection.is_file():
        return None
    try:
        db = sqlite3.connect(projection.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return [row[0] for row in db.execute(
                "SELECT DISTINCT object_id FROM object_versions WHERE l1_handle IN "
                "(SELECT value FROM json_each(?)) ORDER BY object_id",
                (json.dumps(sorted(handles)),))]
        finally:
            db.close()
    except sqlite3.Error:
        return None


def relationships(call: Callable[[str, dict], dict], projection: Path,
                  view: Mapping[str, Any] | None) -> dict[str, Any]:
    """Counts of the projection's ontology links around one reader's case workspace,
    read through that reader's generic tools (``call``): ``relationship_nodes`` (the case
    objects the reader may read), ``relationship_edges`` (link type -> distinct links the
    reader sees with an end on one of them; identity edges included, and the lead-tier
    links ``plx.around`` lists apart as possibly related: a document field's machine-read
    reference to a purchase order is one) and ``relationship_truncated`` (the sorted parts
    the bounded read could not finish, so a count there is a floor, never complete)."""
    if view is None:
        return {}
    from .ontology_tools_traverse import with_leads
    truncated: set[str] = set()
    found = _versions_citing(projection, sorted(_resolved_handles(view, set())))
    if found is None:
        found, truncated = [], {"projection_unavailable"}
    if len(found) > MAP_OBJECTS:
        truncated.add("objects")
    links: dict[str, str] = {}   # link id -> link type
    nodes = 0
    for object_id in found[:MAP_OBJECTS]:
        card = call("plx.object", {"object_id": object_id})
        if "error" in card:
            if _code(card) != "object_not_found":   # not visible to this reader: not counted
                truncated.add("object_error")
            continue
        nodes += 1
        if len(card.get("identity") or []) >= CARD_IDENTITY or len(
                card.get("linked_counts") or []) >= CARD_LINK_TYPES:
            truncated.add("card")   # a card lists at most this many of either
        for edge in card.get("identity") or []:
            links[str(edge.get("link_id"))] = str(edge.get("link_type"))
        for entry in card.get("linked_counts") or []:
            if not entry.get("count"):
                continue
            arguments = {"from": object_id, "tier_floor": LOWEST_TIER, "limit": MAP_PAGE,
                         "path": [{"link_type": entry["link_type"],
                                   "direction": entry["direction"]}]}
            for _page in range(MAP_PAGES):
                around = call("plx.around", arguments)
                if "error" in around:
                    truncated.add(_code(around))
                    break
                for edge in with_leads(around).get("edges") or []:
                    links[str(edge.get("link_id"))] = str(edge.get("link_type"))
                if any(gap.get("reason") == "edge_cap" for gap in around.get("gaps") or []):
                    truncated.add("links")
                cursor = (around.get("completeness") or {}).get("next_cursor")
                if not cursor:
                    break
                arguments = {**arguments, "cursor": cursor}
            else:
                truncated.add("links")
    counts: dict[str, int] = {}
    for link_type in links.values():
        counts[link_type] = counts.get(link_type, 0) + 1
    return {"relationship_nodes": nodes, "relationship_edges": dict(sorted(counts.items())),
            "relationship_truncated": sorted(truncated)}


def checkable(value: str) -> bool:
    """A value the leak checks look for: an identifier/number shape (a digit and
    ``MIN_DIGIT_CHARS`` or more characters: PO5447, 1509, 2026-01-07) or any text of
    ``MIN_LEAK_CHARS`` or more. Shorter values (``20``, ``EA``, ``open``) cannot be told
    from the view's own counts and words and are counted as skipped."""
    return (len(value) >= MIN_LEAK_CHARS
            or (len(value) >= MIN_DIGIT_CHARS and any(ch.isdigit() for ch in value)))


def _strings(node: Any, out: set[str]) -> None:
    texts: set[str] = set()
    _row_texts(node, texts)
    out.update(texts)


def _found(value: str, text: str) -> bool:
    """``value`` occurs in ``text`` as a whole token (not inside a longer id or digest)."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", text) is not None


# Timestamps the Workbench writes itself (case events, reads, refreshes): never source
# values, so they are left out of the text a leak check searches.
_OWN_TIME_KEYS = frozenset({"at", "opened_at", "updated_at", "closed_at", "read_at",
                            "recorded_at", "last_refresh_at", "relation_last_refresh_at",
                            "content_last_seen_at", "decided_at"})


def _without_own_times(node: Any) -> Any:
    if isinstance(node, Mapping):
        return {key: _without_own_times(value) for key, value in node.items()
                if key not in _OWN_TIME_KEYS}
    if isinstance(node, list):
        return [_without_own_times(item) for item in node]
    return node


def leak_check(runtime: Any, full_actor: Any, reader: Any, full_view: Mapping[str, Any] | None,
               reader_view: Mapping[str, Any] | None, audience: Any) -> dict[str, Any]:
    """Does ``reader_view`` hold any value of a row ``reader`` may not read?

    Every row cited on the case (as ``full_actor`` resolves it now) whose relation the
    mirror ``audience`` does not grant ``reader`` is withheld. Its citation handle, and
    every ``checkable`` value no source the reader may read also holds (a readable row, a
    readable original or live citation), must not occur as a whole token anywhere in the
    reader's workspace, the case's own question, roots and uncited items included (a
    case script writes identifiers it read from rows into them; ``case_text_leaks``
    counts those apart, inside ``row_leaks``). Counts only; ``values_skipped_short``
    states the blind spot."""
    from .cases import CitationUnresolvable

    if full_view is None or reader_view is None:
        return {"withheld_rows": 0, "values_checked": 0, "values_skipped_short": 0,
                "row_leaks": 0, "case_text_leaks": 0, "withheld_relation_refs": 0}
    own_words, rest = _split_uncited(_without_own_times(reader_view))
    text = json.dumps(rest, sort_keys=True, default=str, ensure_ascii=False)
    hidden: set[str] = set()
    shown: set[str] = set()
    handles: list[str] = []
    withheld_relations: set[str] = set()
    for record in full_view.get("records") or []:
        handle = record.get("handle")
        try:
            view = runtime.resolve_citation(handle, full_actor)
        except CitationUnresolvable:
            continue
        rows = [view["row"]] if "row" in view else list(view.get("rows") or [])
        if audience.allows(reader, view["relation"]):
            _strings(rows, shown)
        else:
            handles.append(handle)
            withheld_relations.add(view["relation"])
            _strings(rows, hidden)
    # What the reader holds from a source it may read: its originals (file name, path,
    # citation), each re-read for this reader by the workspace, and its readable rows'
    # values (a withheld value that is a whole token of a readable one, such as a code
    # inside the readable AO number, is what the reader may already read).
    held = json.dumps([[original.get("filename"), original.get("citation"),
                        (original.get("source") or {}).get("path"),
                        (original.get("source") or {}).get("name"),
                        (original.get("source") or {}).get("citation")]
                       for original in reader_view.get("originals") or []]
                      + sorted(shown), default=str, ensure_ascii=False)
    candidates = {value for value in hidden - shown if not _found(value, held)}
    values = {value for value in candidates if checkable(value)}
    in_text = sum(1 for value in values if _found(value, text))
    in_words = sum(1 for value in values if _found(value, own_words))
    refs = sum(1 for handle in handles if handle in text or handle in own_words)
    refs += sum(1 for record in reader_view.get("records") or []
                if record.get("relation") in withheld_relations)
    return {"withheld_rows": len(handles), "values_checked": len(values),
            "values_skipped_short": len(candidates) - len(values),
            "row_leaks": in_text + in_words, "case_text_leaks": in_words,
            "withheld_relation_refs": refs}


def _split_uncited(view: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """(the case's uncited text as JSON, the rest of the view).

    Uncited text is what the case store shows every case-audience member whatever the
    sources: the question, the roots and every finding, gap, proposal or calculation
    recorded without a citation."""
    own: list[Any] = [view.get("question"), view.get("roots")]
    rest = {key: value for key, value in view.items() if key not in ("question", "roots")}

    def uncited(item: Any) -> bool:
        return (isinstance(item, Mapping) and not item.get("citations")
                and not (item.get("inputs") or {}))

    for name in ("gaps", "proposals", "calculations"):
        items = list(view.get(name) or [])
        own.extend(item for item in items if uncited(item))
        rest[name] = [item for item in items if not uncited(item)]
    findings = {}
    for bucket, items in (view.get("findings") or {}).items():
        own.extend(item for item in items if uncited(item))
        findings[bucket] = [item for item in items if not uncited(item)]
    rest["findings"] = findings
    return json.dumps(own, sort_keys=True, default=str), rest


def summary_check(runtime: Any, full_actor: Any, views: Sequence[Mapping[str, Any] | None],
                  text: str) -> dict[str, int]:
    """No cited row value (``checkable``, whole token) and no handle in ``text``."""
    from .cases import CitationUnresolvable

    values: set[str] = set()
    for view in views:
        for record in (view or {}).get("records") or []:
            values.add(record["handle"])
            try:
                resolved = runtime.resolve_citation(record["handle"], full_actor)
            except CitationUnresolvable:
                continue
            _strings([resolved.get("row"), resolved.get("rows")], values)
        for original in (view or {}).get("originals") or []:
            _strings([original.get("citation"), original.get("filename"),
                      (original.get("source") or {}).get("path")], values)
    checked = {value for value in values if checkable(value)}
    return {"values_checked": len(checked), "values_skipped_short": len(values) - len(checked),
            "leaked": sum(1 for v in checked if _found(v, text))}
