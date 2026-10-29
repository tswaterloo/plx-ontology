"""Live check of the employee object workspace (workspace.object_view): denominators only.

    python3 -m scripts.object_workspace_probe --db DIR/mirror.sqlite \\
        --store DIR/ontology.sqlite [--budget-seconds N] [--view-budget-seconds N] \\
        [--sample N] [--per-type N] [--max-opens N] [--workers N] [--bc-capture] \\
        [--mail-capture-scope FILE --live-audience FILE --capture-page-budget N] \\
        [--sharepoint-capture-scope FILE --sharepoint-state DIR \\
         --sharepoint-budget-seconds N] [--previous-report DAILY.json]

GET only. The L7 probe (``scripts.ontology_projection_probe --build-only``: one build,
no rebuild and no claim checks; a budget stop inside it prints the build's partial counts,
a failed build its ``code``, ``phase``, ``store`` and ``disk``: ``build_failure``)
builds the throwaway mirror and projection store (a GET-only Portal sync; with ``--bc-capture`` one GET-only
BC capture; with a mail scope one bounded Graph delta capture; with a SharePoint scope the
bounded capture ``ontology.document_links`` runs, kept in ``--sharepoint-state`` and named
by the served state) and lays them out as the served host state. Then, offline over that state and without any client, the console's
object workspace (``operator_ui.case_console.object_workspace_for``: the case host's
tools, grants, item gates and the case runtime's L1 resolver) opens sampled real roots
(first up to ``DOCUMENT_ROOTS`` records a current stored link joins to a mail or SharePoint
document, ``documents_linked_roots``; then up to ``--per-type`` per object type; each by
SHA-256 of the object id, ``--sample`` in all) for the served host's capture principal,
and records per panel:

* ``timeline``: events and truncated answers (WS-02);
* ``originals``: captures listed, openable and withheld, and up to ``--max-opens`` per
  root opened through the workspace's source route; an opened Portal row is re-hashed
  (SHA-256 of its canonical JSON, the mirror's row digest) against the digest its
  citation carries, an attached original's SHA-256 (re-hashed by the case runtime on
  the read) is compared with it; a BC or mail record read through ``plx.read`` returns
  the digest of what it read, not of the capture, so it counts
  ``rehash_not_applicable`` (WS-04);
* ``relationships``: link types listed, hops read, edges and edges by tier (WS-05);
* ``values``: card values by value type (quantity, money, enum among them) with unit and
  currency gaps (WS-06);
* ``documents``: related mail and documents listed, openable and withheld (WS-08);
* ``next_steps``: process stages read, missing and current, and gaps by state (WS-09);
* ``withheld_roots``: sampled roots the reader may not read. The store is sampled
  whatever the reader's grant, so a root whose open raises is classified by the host's
  own access decision (``root_not_found``: its ``plx.object`` for the reader, the call
  the workspace's card makes, answers not found, and the host types that answer,
  ``CaseHostTools.not_found_reason``): a root its grant, markings or item gate withhold
  counts here; a root the host answers not found for another reason (no current version,
  a raising item gate, absent) is a workspace error by that reason
  (``open_errors``: ``not_found_<reason>``), and a root the reader may read, or one the
  host gives no decision on, a workspace error by its class (``workspace_<Class>``);
* ``withheld``: the card's withheld markers and the sources withheld from the reader;
  and a reader with no grant opens every sampled root: each must read as not found
  (``denied_leaks`` 0), so the workspace is no raw graph and no existence oracle (WS-12);
* ``sharepoint_withheld_*``: each opened root a stored link joins to a SharePoint document
  (``documents_linked_roots_sharepoint``) is opened again as a reader granted the records
  but not the SharePoint folder (the L7 probe's ``MAIL_WITHHELD_PRINCIPAL``, which the
  live audience grants BC and the served mirror audience the Portal, never mail or
  SharePoint):
  ``sharepoint_withheld_probed`` such roots, ``sharepoint_withheld_read`` of them it reads
  (it sees the record), and ``sharepoint_withheld_leaks``, each SharePoint document the
  capture principal was shown on that root that this reader's view names (its citation
  anywhere in the view) or that opens for it through the workspace's source route (must
  be 0; any leak fails the run);
* ``find_by_type``: the employee's entry path, Find & open (``ObjectWorkspace.find``),
  searched with each opened root's own identifier values (the card values whose property
  the schema types with an identifier kind of ``ontology/schema/identifiers.json`` for
  the root's type, a foreign-key ``identifies: reference`` property excluded; up to
  ``FIND_QUERIES`` per root), per type: ``found_top1`` / ``found_top5`` (the root's best
  rank over its queries, cumulative), ``found_lower``, ``not_found``, ``ambiguous`` (the
  best query answered several candidates); an ambiguous answer listing the root first is
  no top-1 hit (the employee still chooses among several): it counts in ``found_top5``
  and apart in ``found_top1_ambiguous``, and an unambiguous rank 1 from another of the
  root's queries wins over it. A root with nothing to search by counts by why:
  ``no_identifier_property`` (the schema declares no own identifier for its type),
  ``identifier_invalid`` (the card gaps an own identifier value as not a typed identifier
  of its kind), ``unparsed`` (the card gaps an own identifier as ``value_unparsed``: the
  record holds a value the projection could not parse, an identifier no find reaches),
  ``unread`` (gapped ``value_unread``: absent or unparsed, the build report cannot tell)
  or ``no_value`` (the record holds no value for any own identifier). The same queries as
  the reader with no grant must return no candidate (``find_denied_leaks`` 0, WS-01); a
  run that probed none (``find_denied_probed`` 0) is no pass (the spec's floor). The
  searches are also counted by the root's binding source (``find_denied_probed_by_source``)
  and, over the sources the build did not report out (``blocked_upstream``), as
  ``find_denied_probed_unblocked``: an outage drops the floor for its own source's roots
  only, so a BC outage still needs a Portal root searched, and the reverse;
  ``find_totals``: the same counts summed over types (a record too long for the per-type
  counts keeps these);
* ``next_steps_explained``: the registered intents the open offers (each with its next
  safe step) and the follow-up proposals among them, explained when the step states a
  why and cites at least one record, else unexplained (WS-14), under ``status:
  measured``; when no opened root's intents were read (no action store in the host state:
  ``intents_not_configured``) it is ``status: unmeasured`` with that ``reason`` and the
  ``roots``, never zero counts;
* ``case_evaluations``: the cases citing each opened root that the reader may read and
  each one's evaluation as the object view shows it (``current``, ``stale``, ``unknown``,
  ``withheld``, ``none``, ``not_configured``, ``integrity_error``; WS-15), with the host
  state's playbook report store when it holds one; ``listing_full`` counts opens whose
  case listing hit its bound;
* ``panels``: per panel a WS row cites, the ``items`` it showed over the opened roots and
  the ``roots`` it was non-empty on: ``timeline_cited`` events with a citation (WS-02),
  ``originals_opened`` through the workspace's source route, which re-checks the reader's
  audience (WS-04), ``relationships_tiered`` edges with an evidence tier (WS-05),
  ``values_cited`` money, quantity and enum card values with a citation (WS-06),
  the WS-07 markers, each kind counted apart (a withheld marker never stands in for a
  gap): ``markers_gaps`` gaps, ``markers_conflicts`` conflicts, ``markers_leads`` edges
  below ``corroborated`` (tiers ``stored_by_text_join`` and ``lead``) and
  ``markers_withheld`` (the card's, withheld originals and documents), and ``documents_opened``, related mail and documents (up to
  ``--max-opens`` per root) opened through the same route, ``plx.read`` for mail and
  SharePoint (WS-08). A withheld or failed root shows no panel and adds nothing.

With no document-linked root sampled the documents panel had no input: the run carries
a ``blocked_upstream`` part, the build's refused mail parts (``mail.mailbox_lookup``) when
Graph refused the capture, else ``documents.linked_roots`` (cause ``source_gap``), so it
is never a pass (``documents_unlinked``). The build's mail and mention counts
(``MAIL_COUNTS``) are carried into the output.

With the Portal down the build reports ``blocked_upstream`` and the views run over what
was projected; with nothing projected the probe prints ``sources_blocked`` and measures
nothing. Output: counts, booleans, object type names and enums only; never a value, key,
identifier or citation.

With ``--session-replay N --case-dir DIR`` the probe measures stored Ask sessions instead
of the views (``session_replay``, AGT-02): the served runtime's case store is moved to DIR
(the check's own temporary state; the served state is shared and never written), one case
is opened for the capture principal with a case-only audience principal, and the console's
Ask (``ObjectWorkspace.ask`` with that ``case_id``) runs on sampled roots, in the same
order as the views, until N sessions are kept. The planner is deterministic, no model
(``record_planner``: read the root, cite up to three records its card returned), so this
measures keeping and replaying evidence, not a model's reasoning. Then, over the unchanged
store, each session is listed and replayed through the recorder's workspace (its own agent
session, the Ask's MCP host path): ``replay_identical`` and ``divergences`` (by kind). A
reader holding only the case's audience principal (in the case, granted no record) must
see each session as its withheld marker (``withheld_reader_markers``), get no replay and
open no agent session (``withheld_replayed``, ``withheld_sessions_opened``), and no
recorded object id or cited handle (``withheld_leaks``); a reader outside the case's
audience gets not found (``outsider_not_found``).

Two more measures ride the same run, both counts only. ``reextraction`` (DOC-08): in the
check's own ``--case-dir`` (an originals store of its own, the served one never written) a
synthetic placeholder PDF is attached to a case of its own through ``plx.case_attach_original``
and then re-extracted with a citation and a reason after the store's OCR setting changed; a
new immutable version is added (``new_version``) and every earlier citation still resolves to
the original bytes and the text it named (``changed_earlier``). ``resync`` (ING-08): with the
Portal configured, a second GET-only sync of up to ``RESYNC_RELATIONS`` of the session
mirror's smallest incrementally refreshable relations reads their overlap again from the
watermark and adds ``duplicate_rows`` 0 (the mirror's own capture counts).
"""

from __future__ import annotations

import argparse
import contextlib
from hashlib import sha256
import io
import json
import os
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence

PROBE = "object-workspace/v1"
DENIED_PRINCIPAL = "object-view-probe-denied"
SHAREPOINT_HANDLE = "sharepoint://"   # the L1 scheme of a captured SharePoint file
OPEN_ERRORS = (KeyError, LookupError, ValueError, PermissionError)
FIND_QUERIES = 3   # identifier values one root is searched by, at most
DOCUMENT_ROOTS = 4   # document-linked roots sampled first, at most
# The build's mail capture and mention counts, carried into the record.
MAIL_COUNTS = ("mail_capture_complete", "mail_capture_reasons", "mail_messages_seen",
               "mention_documents", "mention_links")
FIND_COUNTS = ("searched", "found_top1", "found_top1_ambiguous", "found_top5", "found_lower",
               "not_found", "ambiguous", "no_identifier_property", "identifier_invalid",
               "unparsed", "unread", "no_value")
# Card gap reasons of an own identifier the record holds but the projection left out.
LEFT_OUT = {"value_unparsed": "unparsed", "value_unread": "unread"}


def document_types() -> frozenset[str]:
    """The object types the documents panel shows: a schema ``Document`` interface bound
    to a document source (``plx.documents``' families: mail, SharePoint). A Portal type
    with the interface (a quote) is no related document there."""
    from ontology.schema import load_schema
    from operating.ontology_tools_evidence import DOCUMENT_FAMILIES

    served = set(DOCUMENT_FAMILIES.values())
    return frozenset(type_id for type_id, spec in load_schema().object_types.items()
                     if "Document" in spec.get("interfaces", ())
                     and any(b.get("source") in served for b in spec.get("bindings") or ()))


def document_linked_roots(store: str, *, limit: int = DOCUMENT_ROOTS,
                          source: str | None = None) -> list[tuple[str, str]]:
    """``(object_type, object_id)`` of up to ``limit`` records a current stored link joins
    to a document (``document_types``), documents themselves excluded, the lowest SHA-256
    of the object id first: the roots whose documents panel has an input. ``source``:
    only links to a document of that source family (``binding_sources``)."""
    documents = sorted(document_types())
    if not documents or limit <= 0:
        return []
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        marks = ", ".join("?" * len(documents))
        one_way = f"""SELECT o.object_type, o.object_id, d.object_id FROM links l
            JOIN objects o ON o.object_id = l.{{near}} JOIN objects d ON d.object_id = l.{{far}}
            WHERE l.superseded_at IS NULL AND d.object_type IN ({marks})
            AND o.object_type NOT IN ({marks})"""
        rows = conn.execute(" UNION ".join(
            one_way.format(near=near, far=far)
            for near, far in (("from_object", "to_object"), ("to_object", "from_object"))),
            documents * 4).fetchall()
    finally:
        conn.close()
    families = binding_sources() if source is not None else {}
    roots = {(object_type, object_id) for object_type, object_id, document in rows
             if source is None or root_source(document, families) == source}
    return sorted(roots, key=lambda r: sha256(r[1].encode("utf-8")).hexdigest())[:limit]


def sample_roots(store: str, *, sample: int, per_type: int,
                 linked: Sequence[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """``(object_type, object_id)`` pairs: first the document-linked roots (``linked``,
    else ``document_linked_roots``), so a store whose few document links the hash order
    misses still opens one; then up to ``per_type`` per type, the lowest SHA-256 of the
    object id first (spread over the store, the same on every run of one store), taken
    round-robin across types up to ``sample``."""
    linked = document_linked_roots(store) if linked is None else linked
    picked: list[tuple[str, str]] = list(linked)[:sample]
    taken = {object_id for _, object_id in picked}
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        by_type: dict[str, list[str]] = {}
        for object_type, object_id in conn.execute("SELECT object_type, object_id FROM objects"):
            if object_id not in taken:
                by_type.setdefault(object_type, []).append(object_id)
    finally:
        conn.close()
    queues = [sorted(ids, key=lambda i: sha256(i.encode("utf-8")).hexdigest())[:per_type]
              for _, ids in sorted(by_type.items())]
    types = sorted(by_type)
    while len(picked) < sample and any(queues):
        for object_type, queue in zip(types, queues):
            if queue and len(picked) < sample:
                picked.append((object_type, queue.pop(0)))
    return picked


def _add(total: dict[str, Any], key: str, amount: int = 1) -> None:
    total[key] = total.get(key, 0) + amount


def _row_digests(view: Mapping[str, Any]) -> set[str] | None:
    """The mirror's row digests of an opened Portal row (``integrations.source_mirror``
    stores SHA-256 of the canonical JSON), or None when the view holds no row."""
    from integrations.source_mirror import _canonical

    rows = [view["row"]] if isinstance(view.get("row"), Mapping) else view.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    return {sha256(_canonical(row).encode("utf-8")).hexdigest() for row in rows}


def measure_view(view: Mapping[str, Any], workspace: Any, counts: dict[str, dict[str, Any]],
                 max_opens: int) -> int:
    """Add one opened root's per-panel counts to ``counts``; open its originals and
    return how many opened."""
    timeline = view.get("timeline") or {}
    _add(counts["timeline"], "events", len(timeline.get("events") or []))
    _add(counts["timeline"], "roots_with_events", bool(timeline.get("events")))
    _add(counts["timeline"], "truncated", bool(timeline.get("truncated")))
    around = view.get("around") or {}
    _add(counts["relationships"], "link_types_listed", around.get("link_types_listed") or 0)
    for hop in around.get("hops") or []:
        result = hop.get("result") or {}
        _add(counts["relationships"], "hops")
        _add(counts["relationships"], "hop_errors", "error" in result)
        for edge in result.get("edges") or []:
            _add(counts["relationships"], "edges")
            _add(counts["relationships_by_tier"], str(edge.get("tier")))
    _add(counts["relationships"], "roots_with_edges", any(
        (hop.get("result") or {}).get("edges") for hop in around.get("hops") or []))
    card = view.get("card") or {}
    for value in card.get("values") or []:
        _add(counts["values"], str(value.get("value_type")))
        _add(counts["value_gaps"], "unit", "unit_gap" in value)
        _add(counts["value_gaps"], "currency", "currency_gap" in value)
        _add(counts["value_gaps"], "empty", value.get("value") is None)
    withheld = card.get("withheld") or {}
    _add(counts["withheld"], "cards_with_marker", bool(withheld))
    _add(counts["withheld"], "card_properties", withheld.get("properties") or 0)
    _add(counts["withheld"], "card_count_unrevealed", withheld.get("properties", 0) is None)
    _add(counts["withheld"], "sources", len(view.get("withheld_sources") or []))
    process = view.get("process") or {}
    _add(counts["next_steps"], "process_" + str(process.get("status")))
    for stage in (process.get("result") or {}).get("stages") or []:
        _add(counts["next_steps"], "stages")
        _add(counts["next_steps"], "stages_" + str(stage.get("state")))
    _add(counts["next_steps"], "current_stage_known",
         bool((process.get("result") or {}).get("current_stage")))
    for state, number in ((view.get("gaps") or {}).get("by_state") or {}).items():
        _add(counts["gaps_by_state"], state, number)
    _add(counts["next_steps"], "conflicts", len(view.get("conflicts") or []))
    documents = view.get("documents") or {}
    _add(counts["documents"], "panel_" + str(documents.get("status")))
    for document in (documents.get("result") or {}).get("documents") or []:
        _add(counts["documents"], "listed")
        _add(counts["documents"], "openable", document.get("openable") is True)
        _add(counts["documents"], "withheld", "not_openable_reason" in document)
    _add(counts["documents"], "roots_with_documents",
         bool((documents.get("result") or {}).get("documents")))
    originals = view.get("originals") or {}
    _add(counts["originals"], "listed", len(originals.get("items") or []))
    _add(counts["originals"], "total", originals.get("total") or 0)
    _add(counts["originals"], "withheld", (originals.get("withheld") or {}).get("total") or 0)
    _add(counts["originals"], "truncated", bool(originals.get("truncated")))
    opened = opened_ok = 0
    for item in originals.get("items") or []:
        _add(counts["originals_by_scheme"], str(item.get("scheme")))
        if not item.get("openable"):
            continue
        _add(counts["originals"], "openable")
        if opened >= max_opens:
            continue
        opened += 1
        try:
            source = workspace.source(item["l1_handle"])
        except OPEN_ERRORS as error:
            _add(counts["open_errors"], type(error).__name__)
            continue
        _add(counts["originals"], "opened")
        opened_ok += 1
        cited = set(item.get("sha256") or ())
        digests = _row_digests(source)
        if digests is None and source.get("kind") == "original" \
                and isinstance(source.get("sha256"), str):
            digests = {source["sha256"]}   # the case runtime re-hashed the stored bytes
        if digests is None or not cited:
            _add(counts["originals"], "rehash_not_applicable")
        elif digests & cited:
            _add(counts["originals"], "rehash_matched")
        else:
            _add(counts["originals"], "rehash_mismatch")
    return opened_ok


# Edge tiers below "corroborated" that the object view renders as leads (a contradicted
# edge is rendered as a conflict, not counted here).
LEAD_TIERS = ("stored_by_text_join", "lead")
CITED_VALUE_TYPES = ("money", "quantity", "enum")   # amounts, quantities, statuses (WS-06)
PANEL_COUNTS = ("timeline_cited", "originals_opened", "relationships_tiered", "values_cited",
                "markers_gaps", "markers_conflicts", "markers_leads", "markers_withheld",
                "documents_opened")


def measure_panels(view: Mapping[str, Any], workspace: Any, counts: dict[str, dict[str, Any]],
                   max_opens: int, originals_opened: int) -> None:
    """Add one opened root's ``panels`` counts (module doc); open its related documents."""
    documents = (view.get("documents") or {}).get("result") or {}
    opened = 0
    for document in documents.get("documents") or []:
        if document.get("openable") is not True or opened >= max_opens:
            continue
        try:
            workspace.source(document["citation"])
        except OPEN_ERRORS as error:
            _add(counts["open_errors"], "document_" + type(error).__name__)
            continue
        opened += 1
    card = view.get("card") or {}
    # A link shown on more than one hop counts once (by its link_id; one without counts).
    edges = list({edge.get("link_id") or id(edge): edge
                  for hop in (view.get("around") or {}).get("hops") or []
                  for edge in (hop.get("result") or {}).get("edges") or []}.values())
    shown = {
        "timeline_cited": sum(bool(event.get("citation"))
                              for event in (view.get("timeline") or {}).get("events") or []),
        "originals_opened": originals_opened,
        "relationships_tiered": sum(edge.get("tier") is not None for edge in edges),
        "values_cited": sum(value.get("value_type") in CITED_VALUE_TYPES
                            and bool(value.get("citation"))
                            for value in card.get("values") or []),
        "markers_gaps": (view.get("gaps") or {}).get("total") or 0,
        "markers_conflicts": len(view.get("conflicts") or []),
        "markers_leads": sum(edge.get("tier") in LEAD_TIERS for edge in edges),
        "markers_withheld": bool(card.get("withheld"))
        + (((view.get("originals") or {}).get("withheld") or {}).get("total") or 0)
        + sum("not_openable_reason" in document for document in documents.get("documents")
              or []),
        "documents_opened": opened}
    for panel, number in shown.items():
        _add(counts["panels"][panel], "items", number)
        _add(counts["panels"][panel], "roots", bool(number))


def own_identifiers() -> dict[str, frozenset[str]]:
    """Object type -> the properties the committed schema types with an identifier kind
    (``ontology/schema/identifiers.json``) that identify the object itself: a property
    declared ``identifies: reference`` (a foreign key) names another object."""
    from ontology.schema import load_schema

    return {type_id: frozenset(
        prop["name"] for prop in spec.get("properties") or ()
        if isinstance(prop.get("identifier_kind"), str) and "name" in prop
        and prop.get("identifies") != "reference")
        for type_id, spec in load_schema().object_types.items()}


def identifier_queries(object_type: str, view: Mapping[str, Any],
                       own: Mapping[str, frozenset[str]]) -> list[str]:
    """The root's own identifier values as the reader's card shows them (``own``: the
    schema's identifier properties per type, ``own_identifiers``; no literal kinds)."""
    from operator_ui.object_workspace import MAX_QUERY

    values = {value["value"] for value in (view.get("card") or {}).get("values") or []
              if value.get("value_type") == "identifier"
              and value.get("property") in own.get(object_type, ())
              and isinstance(value.get("value"), str)
              and 0 < len(value["value"].strip()) <= MAX_QUERY}
    return sorted(values)[:FIND_QUERIES]


def no_identifier_reason(object_type: str, view: Mapping[str, Any],
                         own: Mapping[str, frozenset[str]]) -> str:
    """Why a root has nothing to search by (module doc): a ``FIND_COUNTS`` key."""
    if not own.get(object_type):
        return "no_identifier_property"
    card = view.get("card") or {}
    if any(value.get("property") in own[object_type]
           and (value.get("value_gap") or {}).get("reason") == "identifier_invalid"
           for value in card.get("values") or []):
        return "identifier_invalid"
    left_out = {LEFT_OUT[gap["reason"]] for gap in card.get("gaps") or ()
                if gap.get("reason") in LEFT_OUT and str(gap.get("subject")) in
                {f"{object_type}.{name}" for name in own[object_type]}}
    return "unparsed" if "unparsed" in left_out else "unread" if left_out else "no_value"


def sharepoint_withheld(view: Mapping[str, Any], record_only: Any, object_id: str,
                        report: dict[str, Any]) -> None:
    """Open a SharePoint-linked root as the reader granted its records but not the folder
    (``record_only``, a workspace): it must read the record and be served none of the
    SharePoint documents the capture principal's ``view`` showed, neither named in its
    view nor opened through the source route (module doc)."""
    shown = {document["citation"] for document in
             ((view.get("documents") or {}).get("result") or {}).get("documents") or []
             if str(document.get("citation", "")).startswith(SHAREPOINT_HANDLE)}
    report["sharepoint_withheld_probed"] += 1
    try:
        other = record_only.workspace(object_id)
    except OPEN_ERRORS:
        other = None   # the record itself withheld: nothing read, nothing named
    if other is not None and other.get("status") == "read":
        report["sharepoint_withheld_read"] += 1
    named = json.dumps(other, sort_keys=True, default=str)
    for citation in sorted(shown):
        try:
            record_only.source(citation)
            opened = True
        except OPEN_ERRORS:
            opened = False
        report["sharepoint_withheld_leaks"] += opened or citation in named


def binding_sources() -> dict[str, str]:
    """Binding id -> its source family (``portal_api``, ``bc``, ``graph_mail``, ...)."""
    from ontology.schema import load_schema

    return {binding["id"]: binding["source"] for spec in load_schema().object_types.values()
            for binding in spec.get("bindings") or ()}


def root_source(object_id: str, sources: Mapping[str, str]) -> str:
    """The source family of the root's binding (``Type:binding:key``); else ``unknown``."""
    parts = object_id.split(":", 2)
    return sources.get(parts[1], "unknown") if len(parts) == 3 else "unknown"


def denied_probed_unblocked(report: Mapping[str, Any]) -> int:
    """The no-grant reader's searches on roots whose source no ``blocked_upstream`` part
    reports out: the spec's floor during an outage. A part naming a source this probe
    does not project from leaves every root counted (fails closed); a SharePoint part
    (Graph too) reports SharePoint out, never the mail."""
    from scripts.ontology_projection_probe import BC, GRAPH, PORTAL

    family = {PORTAL: "portal_api", BC: "bc", GRAPH: "graph_mail"}
    out = {"sharepoint" if str(part.get("part", "")).startswith("sharepoint")
           else family.get(part.get("source"))
           for part in report.get("blocked_upstream") or () if isinstance(part, Mapping)}
    return sum(count for source, count in
               (report.get("find_denied_probed_by_source") or {}).items() if source not in out)


def documents_unlinked(report: Mapping[str, Any]) -> dict[str, Any] | None:
    """The part that keeps a run with no document-linked root sampled from passing: the
    documents panel had no input, so it was not measured. None when a root was linked,
    or when a Graph part already names why (a refused or failed mail capture)."""
    from scripts.ontology_projection_probe import GRAPH

    if report.get("documents_linked_roots"):
        return None
    if any(isinstance(part, Mapping) and part.get("source") == GRAPH
           for part in report.get("blocked_upstream") or ()):
        return None
    return {"part": "documents.linked_roots", "source": GRAPH,
            "error_code": "no_document_links", "cause": "source_gap"}


def measure_find(object_type: str, object_id: str, queries: Sequence[str], granted: Any,
                 denied: Any, report: dict[str, Any], counts: dict[str, Any],
                 missing: str = "no_value", source: str = "unknown") -> None:
    """Search the root by its identifiers through Find & open as the reader (best rank,
    an unambiguous answer first) and as the reader with no grant (any candidate is a
    leak); ``missing`` counts a root with no query, ``source`` is its binding's source."""
    found = report["find_by_type"].setdefault(object_type, dict.fromkeys(FIND_COUNTS, 0))
    if not queries:
        found[missing] += 1
        return
    found["searched"] += 1
    best: tuple[int, bool] | None = None
    for query in queries:
        try:
            answer = granted.find(query)["find"]
        except OPEN_ERRORS as error:
            report["workspace_errors"] += 1
            _add(counts["open_errors"], "find_" + type(error).__name__)
            continue
        ids = [(c.get("object") or {}).get("object_id") for c in answer.get("candidates") or []]
        if object_id in ids:
            rank = (ids.index(object_id) + 1, answer.get("status") == "ambiguous")
            best = rank if best is None else min(best, rank)
            if best == (1, False):
                break
    if best is None:
        found["not_found"] += 1
    else:
        found["found_top1"] += best == (1, False)
        found["found_top1_ambiguous"] += best == (1, True)
        found["found_top5"] += best[0] <= 5
        found["found_lower"] += best[0] > 5
        found["ambiguous"] += best[1]
    report["find_denied_probed"] += 1
    _add(report.setdefault("find_denied_probed_by_source", {}), source)
    for query in queries:
        try:
            answer = denied.find(query)["find"]
        except OPEN_ERRORS as error:
            report["workspace_errors"] += 1
            _add(counts["open_errors"], "find_denied_" + type(error).__name__)
            continue
        if answer.get("candidates"):
            report["find_denied_leaks"] += 1
            break


def measure_next_steps(view: Mapping[str, Any], counts: dict[str, Any]) -> None:
    """The next steps the open offers (its registered intents) and the follow-up proposals
    among them: explained = a stated why (the next safe step's detail) and >= 1 citation."""
    from operator_ui.object_workspace import FOLLOW_UP_CAPABILITY

    intents = view.get("intents") or {}
    if intents.get("status") != "read":
        _add(counts, "intents_" + str(intents.get("status")))
        return
    counts["intents_read"] = True
    for intent in intents.get("intents") or []:
        why = (intent.get("next_safe_step") or {}).get("detail")
        explained = (isinstance(why, str) and bool(why.strip())
                     and bool(intent.get("cited_records")))
        kind = ("follow_ups" if intent.get("capability") == FOLLOW_UP_CAPABILITY
                else "next_steps")
        _add(counts, kind + "_offered")
        _add(counts, kind + ("_explained" if explained else "_unexplained"))


def measure_case_evaluations(view: Mapping[str, Any], counts: dict[str, Any]) -> None:
    """The cases the open lists for its root and each one's evaluation status."""
    panel = view.get("cases") or {}
    if panel.get("status") != "read":
        _add(counts, "panel_" + str(panel.get("status")))
        return
    _add(counts, "roots_with_cases", bool(panel.get("cases")))
    _add(counts, "listing_full", bool(panel.get("cases_listing_full")))
    for case in panel.get("cases") or []:
        _add(counts, "cases")
        _add(counts, str(case.get("evaluation_status")))


def root_not_found(workspace: Any, object_id: str) -> str | None:
    """The host's own access decision on a root whose open raised: its ``plx.object``
    through the workspace's tool table for the workspace's reader answers not found (a
    sampled root exists in the store, so this is a withheld or broken one) and the host
    types that answer (``operating.ontology_host.NOT_FOUND_REASONS``, ``withheld`` the
    only permission reason). A card, another error, a raising call or an untyped answer
    is no such decision: None, so the open error stands (fails closed)."""
    from operating.ontology_host import NOT_FOUND_REASONS

    try:
        with workspace.access.request():
            card = workspace.tools["plx.object"]["handler"]({"object_id": object_id},
                                                            workspace.actor)
            if (card.get("error") or {}).get("code") not in ("object_not_found", "not_found"):
                return None
            reason = workspace.access.host.not_found_reason(
                object_id, workspace.access.runtime.employee_gate(workspace.actor))
    except OPEN_ERRORS:
        return None
    return reason if reason in NOT_FOUND_REASONS else None


# The case-only audience principal of the session-replay case (module doc).
REPLAY_PRINCIPAL = "session-replay-probe-case"
REPLAY_QUESTION = "What is on record for this object?"
REPLAY_COUNTS = ("roots_asked", "roots_withheld", "sessions_recorded", "sessions_refused",
                 "recorder_withheld", "sessions_with_citations", "citations", "calls",
                 "citations_changed", "replayed", "replay_identical", "divergences",
                 "withheld_reader_markers", "withheld_reader_shown", "withheld_replayed",
                 "withheld_sessions_opened", "withheld_leaks")


REEXTRACT_QUESTION = "Re-extraction keeps earlier citations."
REEXTRACT_COUNTS = ("attached", "new_version", "earlier_citations", "changed_earlier",
                    "bytes_changed", "refused")
REEXTRACT_FIELDS = ("sha256", "text", "text_sha256", "text_source", "page", "extraction")


def _reextract_extractor(content: bytes, mime_type: str, *, original_url: str,
                         filename: str = "", local_pdf_ocr: bool = False) -> Any:
    """A deterministic stand-in for the extraction contract: page 1 is a text layer; with
    OCR on, a second page is read by OCR, so re-extraction after OCR is a new version."""
    from integrations.document_understanding import ExtractedDocument

    layer = "text layer of the probe original"
    if not local_pdf_ocr:
        return ExtractedDocument("application/pdf", "x", "extracted", "probe-text", layer,
                                 (), (), (), ({"page": 1, "start": 0, "end": len(layer)},))
    text = layer + "\fscanned page read by OCR"
    return ExtractedDocument("application/pdf", "x", "extracted", "probe-ocr", text, (), (),
                             (), ({"page": 1, "start": 0, "end": len(layer)},
                                  {"page": 2, "start": len(layer) + 1, "end": len(text)}),
                             ocr_pages=(2,))


def check_reextraction(runtime: Any, actor: Any, case_dir: str,
                       server: Any) -> dict[str, int]:
    """DOC-08: attach a placeholder PDF to a case of its own, then re-extract it through
    ``plx.case_attach_original`` (citation + reason) after OCR is switched on; counts only.
    ``runtime.originals`` is replaced by stores under ``case_dir`` (the served originals are
    never written); the first read's citations must resolve unchanged afterwards."""
    from pathlib import Path

    from operating import agent_loop
    from operating.case_originals import OriginalStore
    from operating.employee_permissions import gate_actor_methods

    report = dict.fromkeys(REEXTRACT_COUNTS, 0)
    root = Path(case_dir) / "originals"

    def store(ocr: bool) -> Any:
        return gate_actor_methods(OriginalStore(
            root, local_ocr=ocr, extractor=_reextract_extractor,
            source_gate=runtime._original_source_gate), runtime.employee_gate)

    runtime.originals = store(False)
    case_id = runtime.cases.open(actor, REEXTRACT_QUESTION, audience=[REPLAY_PRINCIPAL])
    inbox = runtime.originals.inbox / REPLAY_PRINCIPAL
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "probe.pdf").write_bytes(b"%PDF-1.4\n% session-replay probe placeholder\n")
    session = agent_loop._Session(server)
    attached, error = session.call("plx.case_attach_original", {
        "case_id": case_id, "inbox_name": "probe.pdf", "inbox_principal": REPLAY_PRINCIPAL})
    if error:
        report["refused"] += 1
        return report
    original = attached["original"]
    report["attached"] = 1

    def seen(handle: str) -> dict[str, Any]:
        view = runtime.originals.resolve(handle, actor)
        return {key: view[key] for key in REEXTRACT_FIELDS if key in view}

    handles = [original["citation"], *original["page_citations"]]
    before = {handle: seen(handle) for handle in handles}
    runtime.originals = store(True)
    again, error = session.call("plx.case_attach_original", {
        "case_id": case_id, "citation": original["citation"],
        "reason": "OCR switched on for the probe"})
    if error:
        report["refused"] += 1
        return report
    version = again["extraction_version"]
    report["new_version"] = int(again["duplicate"] is False and version["version"] == 2)
    report["earlier_citations"] = len(handles)
    report["changed_earlier"] = sum(seen(handle) != before[handle] for handle in handles)
    blob = runtime.originals.store / original["sha256"]
    report["bytes_changed"] = int(sha256(blob.read_bytes()).hexdigest() != original["sha256"])
    return report


RESYNC_RELATIONS = 3
RESYNC_COUNTS = ("relations", "incremental", "not_incremental", "overlap_rows", "keys_new",
                 "keys_changed", "keys_unchanged", "duplicate_rows")


def check_resync(db: str, environ: Mapping[str, str], *, relations: int = RESYNC_RELATIONS,
                 budget_seconds: float | None = None) -> dict[str, Any]:
    """The re-sync of ``_resync`` over a private copy of the session mirror in a temporary
    directory: the shared mirror other checks read is never written."""
    import tempfile

    url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
    if not url or not key:
        return _resync(db, environ, relations=relations, budget_seconds=budget_seconds)
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "mirror.sqlite")
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as source, \
                contextlib.closing(sqlite3.connect(copy)) as target:
            source.backup(target)
        return _resync(copy, environ, relations=relations, budget_seconds=budget_seconds)


def _resync(db: str, environ: Mapping[str, str], *, relations: int = RESYNC_RELATIONS,
            budget_seconds: float | None = None) -> dict[str, Any]:
    """ING-08: a second GET-only sync of the session mirror's ``relations`` smallest
    incrementally refreshable relations. Each is read again from its watermark minus the
    overlap (``incremental``, else ``not_incremental``); ``duplicate_rows`` is what its
    stored row count grew by beyond the capture's ``keys_new`` plus the rows received that
    no capture count accounts for. Counts only; ``status`` is ``measured``, ``not_run``
    (no Portal client) or ``not_exercised`` (no relation to refresh)."""
    from integrations.source_mirror import (PROBE_PRINCIPAL, AudienceMap, SourceMirror,
                                            load_ingest_config, mirror_options)
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from ontology.core import Actor

    report: dict[str, Any] = {"status": "measured", **dict.fromkeys(RESYNC_COUNTS, 0)}
    url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
    if not url or not key:
        return {**report, "status": "not_run"}
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as held:
        before = {name: count for name, count in held.execute(
            "SELECT relation, row_count FROM relation_state WHERE watermark IS NOT NULL "
            "AND since_column IS NOT NULL AND key_unique = 1 ORDER BY row_count, relation "
            "LIMIT ?", (relations,))}
        last = held.execute("SELECT COALESCE(MAX(capture_id), 0) FROM capture").fetchone()[0]
    if not before:
        return {**report, "status": "not_exercised"}
    names = sorted(before)
    schemas = {name.split(".", 1)[0] for name in names}
    with SourceMirror(db, WorkbenchReadApiClient(url, key), AudienceMap(
            {f"{schema}.*": [PROBE_PRINCIPAL] for schema in schemas}, tenant="plx"),
            tenant="plx", **mirror_options(load_ingest_config())) as mirror:
        mirror.sync(Actor("plx", frozenset({PROBE_PRINCIPAL})), relations=names, full=False,
                    budget_seconds=budget_seconds)
    report["relations"] = len(names)
    with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as held:
        for name in names:
            captured = held.execute(
                "SELECT mode, rows_received, keys_new, keys_changed, keys_unchanged FROM capture "
                "WHERE relation = ? AND capture_id > ? ORDER BY capture_id DESC LIMIT 1",
                (name, last)).fetchone()
            after = held.execute("SELECT row_count FROM relation_state WHERE relation = ?",
                                 (name,)).fetchone()
            if captured is None or captured[0] != "incremental" or after is None:
                report["not_incremental"] += 1
                continue
            mode, received, new, changed, unchanged = captured
            report["incremental"] += 1
            report["overlap_rows"] += received
            report["keys_new"] += new
            report["keys_changed"] += changed
            report["keys_unchanged"] += unchanged
            report["duplicate_rows"] += (max(0, after[0] - before[name] - new)
                                         + max(0, received - new - changed - unchanged))
    return report


def record_planner(current: list[str]) -> Any:
    """A deterministic Ask planner (no model) about ``current[0]``: read it with
    ``plx.object``, then answer with up to three values its card returned, each a
    structured claim on the cell it was read from (abstain when it returned none). The
    planner is shown each result without its citations' digests
    (``agent_loop._without_digests``), so it picks the cited cells by handle and leaves
    whether each was returned as evidence to the loop's claim gate, which reads the whole
    result: a claim on a handle not returned is refused there, never recorded."""

    def plan(request: Mapping[str, Any]) -> tuple[dict[str, Any], None]:
        arguments = {"object_id": current[0]}
        if not request["transcript"]:
            return {"action": "call", "calls": [{"tool": "plx.object",
                                                 "arguments": arguments}]}, None
        result = request["transcript"][0]["observations"][0].get("result")
        claims = []
        for entry in (result.get("values") or []) if isinstance(result, Mapping) else []:
            cite = entry.get("citation") if isinstance(entry, Mapping) else None
            value = entry.get("value") if isinstance(entry, Mapping) else None
            if isinstance(value, Mapping):   # the output boundary's untrusted_text envelope
                value = value.get("untrusted_text")
            if (value is None or not isinstance(cite, Mapping)
                    or not isinstance(cite.get("l1_handle"), str)):
                continue
            claims.append({"value": value, "citation": cite["l1_handle"],
                           "field": cite.get("field_path")})
        if not claims:
            return {"action": "abstain", "reason": "nothing_returned"}, None
        return {"action": "answer", "claims": claims[:3]}, None
    return plan


def case_store_in(runtime: Any, case_dir: str) -> None:
    """Move ``runtime``'s case store to ``case_dir``, with its resolver, contracts and
    employee gate (``CaseRuntime``'s own): the served state is never written."""
    from pathlib import Path

    from operating.case_runtime import CASES_FILE
    from operating.cases import CaseStore
    from operating.employee_permissions import gate_actor_methods

    Path(case_dir).mkdir(parents=True, exist_ok=True)
    runtime.cases = gate_actor_methods(CaseStore(
        Path(case_dir) / CASES_FILE, citation_resolver=runtime.resolve_citation,
        unit_contracts=runtime.unit_contracts, link_contracts=runtime.link_contracts),
        runtime.employee_gate)


def check_session_replay(host: Any, case_dir: str, *, sessions: int, sample: int,
                         per_type: int, budget_seconds: float | None) -> dict[str, Any]:
    """Keep Ask sessions on a case in ``case_dir`` and replay them over the served
    ``host``'s unchanged state, as the recorder, a withheld reader and an outsider;
    counts and booleans only (module doc)."""
    from integrations import capture_mail, capture_sharepoint
    from ontology.types import Actor
    from operating.host import projection_path
    from operator_ui.case_console import object_workspace_for

    started = time.monotonic()
    state, config = host.state_dir, host.config
    store = projection_path(state)
    roots = sample_roots(str(store), sample=sample, per_type=per_type)
    report: dict[str, Any] = {**dict.fromkeys(REPLAY_COUNTS, 0), "ask_errors": {},
                              "divergences_by_kind": {}, "outsider_not_found": False,
                              "budget_exhausted": False}
    captures = ({"mail_capture_dir": state if capture_mail.store_exists(state) else None,
                 "sharepoint_capture_dir": state if capture_sharepoint.store_exists(state)
                 else None} if config.live_audience is not None else {})
    recorder = Actor(config.tenant, frozenset({config.principal, REPLAY_PRINCIPAL}),
                     "restricted")
    in_case = Actor(config.tenant, frozenset({REPLAY_PRINCIPAL}), "restricted")
    outsider = Actor(config.tenant, frozenset({DENIED_PRINCIPAL}), "restricted")
    views: list[Any] = []
    current: list[str] = [""]
    with contextlib.ExitStack() as stack:
        runtime = stack.enter_context(host.served_tools(recorder)).runtime
        stack.callback(lambda: [view.close() for view in views])
        case_store_in(runtime, case_dir)

        def workspace(actor: Any, planner: Any = None) -> Any:
            views.append(object_workspace_for(runtime, store, actor, state_dir=state,
                                              planner=planner, **captures))
            return views[-1]

        asker, replayer = workspace(recorder, record_planner(current)), workspace(recorder)
        case_id = runtime.cases.open(recorder, REPLAY_QUESTION, audience=[REPLAY_PRINCIPAL])
        recorded: list[tuple[str, str]] = []
        for _, object_id in roots:
            if len(recorded) >= sessions:
                break
            if budget_seconds is not None and time.monotonic() - started > budget_seconds:
                report["budget_exhausted"] = True
                break
            report["roots_asked"] += 1
            current[0] = object_id
            try:
                kept = asker.ask({"object_id": object_id, "question": REPLAY_QUESTION,
                                  "case_id": case_id})["session"]
            except KeyError:
                report["roots_withheld"] += 1
                continue
            except OPEN_ERRORS as error:
                _add(report["ask_errors"], type(error).__name__)
                continue
            if not kept.get("recorded"):
                report["sessions_refused"] += 1
                continue
            recorded.append((kept["session_id"], object_id))
        report["sessions_recorded"] = len(recorded)
        shown = {item["session_id"]: item
                 for item in replayer.case_records(case_id)["sessions"]}
        handles: set[str] = set()
        for session_id, _ in recorded:
            item = shown.get(session_id) or {"withheld": True}
            if "withheld" in item:
                report["recorder_withheld"] += 1
                continue
            cited = item["citations"]
            handles |= {cite["handle"] for cite in cited}
            report["sessions_with_citations"] += bool(cited)
            report["citations"] += len(cited)
            report["calls"] += item["call_count"]
            report["citations_changed"] += sum(cite["status"] != "unchanged" for cite in cited)
            replay = replayer.replay({"case_id": case_id, "session_id": session_id})["replay"]
            report["replayed"] += replay["replayed"] is True
            report["replay_identical"] += replay.get("identical") is True
            for divergence in replay.get("divergences") or ():
                report["divergences"] += 1
                _add(report["divergences_by_kind"], str(divergence.get("kind")))
        report["reextraction"] = check_reextraction(
            runtime, recorder, case_dir, replayer.access.agent_server())
        withheld = workspace(in_case)
        server = withheld.access.agent_server
        withheld.access.agent_server = lambda: (_add(report, "withheld_sessions_opened"),
                                                server())[1]
        seen = withheld.case_records(case_id)["sessions"]
        replays = [withheld.replay({"case_id": case_id, "session_id": session_id})["replay"]
                   for session_id, _ in recorded]
        report["withheld_reader_markers"] = sum("withheld" in item for item in seen)
        report["withheld_reader_shown"] = sum("withheld" not in item for item in seen)
        report["withheld_replayed"] = sum(replay["replayed"] is not False for replay in replays)
        said = json.dumps([seen, replays])
        report["withheld_leaks"] = sum(value in said for value in
                                       {object_id for _, object_id in recorded} | handles)
        try:
            workspace(outsider).case_records(case_id)
        except KeyError:
            report["outsider_not_found"] = True
    report["replay_seconds"] = round(time.monotonic() - started, 3)
    return report


def session_replay_passed(report: Mapping[str, Any]) -> bool:
    """Every kept session replays identically for its recorder and is withheld, unreplayed,
    from the reader in the case who may read none of it; at least one session was kept."""
    recorded = report["sessions_recorded"]
    reextracted, resync = report["reextraction"], report.get("resync") or {}
    return (recorded >= 1 and report["replay_identical"] == recorded
            and reextracted["new_version"] == 1 and reextracted["changed_earlier"] == 0
            and reextracted["bytes_changed"] == 0 and not reextracted["refused"]
            and not resync.get("duplicate_rows") and not resync.get("not_incremental")
            and report["divergences"] == 0 and report["recorder_withheld"] == 0
            and report["withheld_reader_markers"] == recorded
            and not any(report[key] for key in ("withheld_reader_shown", "withheld_replayed",
                                                "withheld_sessions_opened",
                                                "withheld_leaks", "ask_errors",
                                                "budget_exhausted"))
            and report["outsider_not_found"] is True)


PANELS = ("timeline", "relationships", "relationships_by_tier", "values", "value_gaps",
          "withheld", "next_steps", "gaps_by_state", "documents", "originals",
          "originals_by_scheme", "open_errors", "next_steps_explained", "case_evaluations",
          "panels")
NEXT_STEP_COUNTS = tuple(f"{kind}_{count}" for kind in ("next_steps", "follow_ups")
                         for count in ("offered", "explained", "unexplained"))
def next_steps_outcome(counts: Mapping[str, Any], opened: int) -> dict[str, Any]:
    """WS-14 as measured, or typed unmeasured (no opened root's intents were read):
    ``reason`` is the one status the roots' intents had, else ``intents_not_read``."""
    if counts.get("intents_read"):
        return {"status": "measured", **{k: v for k, v in counts.items() if k != "intents_read"}}
    unread = sorted(k for k in counts if k.startswith("intents_"))
    reason = (unread[0] if len(unread) == 1 else
              "no_root_opened" if not opened else "intents_not_read")
    return {"status": "unmeasured", "reason": reason, "roots": opened}


ORIGINAL_COUNTS = ("listed", "total", "withheld", "truncated", "openable", "opened",
                   "rehash_matched", "rehash_mismatch", "rehash_not_applicable")


def check_views(host: Any, *, sample: int, per_type: int, max_opens: int,
                budget_seconds: float | None) -> dict[str, Any]:
    """Open sampled roots in the object workspace for the capture principal and for a
    reader with no grant, and each SharePoint-linked one for a reader granted the records
    only, over the served ``host``'s state; counts only (module doc)."""
    from integrations import capture_mail, capture_sharepoint
    from ontology.types import Actor
    from operating.host import projection_path
    from operator_ui.case_console import (ACTIONS_FILE, REPORTS_FILE, object_workspace_for,
                                          report_store_for)
    from operator_ui.object_workspace import EVALUATION_STATES
    from scripts.ontology_projection_probe import MAIL_WITHHELD_PRINCIPAL, reader_actor

    started = time.monotonic()
    state, config = host.state_dir, host.config
    store = projection_path(state)
    linked = document_linked_roots(str(store))
    roots = sample_roots(str(store), sample=sample, per_type=per_type, linked=linked)
    # of them, linked to a SharePoint document (that input's share; a root of linked
    # ranks no lower among those)
    sharepoint_linked = set(linked) & set(roots) & set(document_linked_roots(
        str(store), limit=len(linked), source="sharepoint"))
    own, sources = own_identifiers(), binding_sources()
    counts: dict[str, dict[str, Any]] = {panel: {} for panel in PANELS}
    counts["originals"] = dict.fromkeys(ORIGINAL_COUNTS, 0)
    counts["next_steps_explained"] = dict.fromkeys(NEXT_STEP_COUNTS, 0)
    counts["case_evaluations"] = dict.fromkeys(
        ("cases", "roots_with_cases", "listing_full") + EVALUATION_STATES, 0)
    counts["panels"] = {panel: {"items": 0, "roots": 0} for panel in PANEL_COUNTS}
    by_type: dict[str, int] = {}
    report: dict[str, Any] = {"roots_sampled": len(roots), "roots_opened": 0,
                              "documents_linked_roots": len(set(linked) & set(roots)),
                              "documents_linked_roots_sharepoint": len(sharepoint_linked),
                              "sharepoint_withheld_probed": 0,
                              "sharepoint_withheld_read": 0,
                              "sharepoint_withheld_leaks": 0,
                              "workspace_errors": 0, "withheld_roots": 0,
                              "denied_probed": 0,
                              "denied_not_found": 0, "denied_leaks": 0,
                              "budget_exhausted": False, "find_by_type": {},
                              "find_denied_probed": 0, "find_denied_leaks": 0,
                              "find_denied_probed_by_source": {}}
    captures = ({"mail_capture_dir": state if capture_mail.store_exists(state) else None,
                 "sharepoint_capture_dir": state if capture_sharepoint.store_exists(state)
                 else None} if config.live_audience is not None else {})
    reader = reader_actor(host)
    readers = []
    # The served host's case runtime (its mirror audience and live audience, no client).
    with contextlib.ExitStack() as stack:
        runtime = stack.enter_context(host.served_tools(reader)).runtime
        stack.callback(lambda: [view.close() for view in readers])
        # The intents an open offers come from the host state's action store, as the
        # console reads it (read only: list_for_case); without one none are offered.
        action_file = state / ACTIONS_FILE
        action_path = None
        if action_file.exists():
            from operating.action_path import ActionPath
            action_path = ActionPath(action_file, runtime.cases)
        # Each case's evaluation comes from the host state's report store, when it has one.
        reports = (report_store_for(runtime, state / REPORTS_FILE)
                   if (state / REPORTS_FILE).exists() else None)
        denied = Actor(config.tenant, frozenset({DENIED_PRINCIPAL}), "restricted")
        # granted the records, not the SharePoint folder (sharepoint_withheld)
        record_only = Actor(config.tenant, frozenset({MAIL_WITHHELD_PRINCIPAL}), "restricted")
        for actor in (reader, denied, record_only):
            readers.append(object_workspace_for(runtime, store, actor, state_dir=state,
                                                action_path=action_path,
                                                report_store=reports, **captures))
        granted, withheld, records_only = readers
        for object_type, object_id in roots:
            if budget_seconds is not None and time.monotonic() - started > budget_seconds:
                report["budget_exhausted"] = True
                break
            try:
                view = granted.workspace(object_id)
            except OPEN_ERRORS as error:
                reason = root_not_found(granted, object_id)
                if reason == "withheld":
                    report["withheld_roots"] += 1
                else:
                    report["workspace_errors"] += 1
                    _add(counts["open_errors"], f"not_found_{reason}" if reason
                         else "workspace_" + type(error).__name__)
                continue
            report["roots_opened"] += 1
            by_type[object_type] = by_type.get(object_type, 0) + 1
            if view.get("status") != "read":
                report["workspace_errors"] += 1
                continue
            measure_panels(view, granted, counts, max_opens,
                           measure_view(view, granted, counts, max_opens))
            if (object_type, object_id) in sharepoint_linked:
                sharepoint_withheld(view, records_only, object_id, report)
            measure_next_steps(view, counts["next_steps_explained"])
            measure_case_evaluations(view, counts["case_evaluations"])
            measure_find(object_type, object_id, identifier_queries(object_type, view, own),
                         granted, withheld, report, counts,
                         no_identifier_reason(object_type, view, own),
                         root_source(object_id, sources))
            report["denied_probed"] += 1
            try:
                withheld.workspace(object_id)
                report["denied_leaks"] += 1
            except KeyError:
                report["denied_not_found"] += 1
    counts["next_steps_explained"] = next_steps_outcome(counts["next_steps_explained"],
                                                        report["roots_opened"])
    report.update(counts)
    # The find counts summed over types: what the record keeps when per type is too long.
    report["find_totals"] = {key: sum(found[key] for found in report["find_by_type"].values())
                             for key in FIND_COUNTS}
    report.update(roots_by_type=by_type, view_seconds=round(time.monotonic() - started, 3))
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.object_workspace_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="the L7 build's budget")
    parser.add_argument("--view-budget-seconds", type=float, default=None)
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--per-type", type=int, default=4)
    parser.add_argument("--max-opens", type=int, default=5,
                        help="originals opened (and re-hashed) per root")
    parser.add_argument("--previous-report", default=None,
                        help="last daily verifier report, passed to the L7 build")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--bc-capture", action="store_true")
    parser.add_argument("--mail-capture-scope", default=None)
    parser.add_argument("--live-audience", default=None)
    parser.add_argument("--capture-page-budget", type=int, default=None)
    parser.add_argument("--sharepoint-capture-scope", default=None)
    parser.add_argument("--sharepoint-state", default=None)
    parser.add_argument("--sharepoint-budget-seconds", type=float, default=None)
    parser.add_argument("--session-projection", default=None,
                        help="the verifier session's projection (the L7 probe's)")
    parser.add_argument("--session-replay", type=int, default=None,
                        help="keep and replay N Ask sessions instead of the views")
    parser.add_argument("--case-dir", default=None,
                        help="the session-replay case store's own directory")
    args = parser.parse_args(argv)
    if (args.session_replay is None) != (args.case_dir is None) \
            or (args.session_replay is not None and not 1 <= args.session_replay <= 20):
        parser.error("--session-replay (1 to 20) and --case-dir go together")
    phase = None   # the consumer's own phase, once the build succeeded
    try:
        from scripts import ontology_projection_probe as projection

        build_args = ["--db", args.db, "--store", args.store, "--build-only"]
        for flag, value in (("--budget-seconds", args.budget_seconds),
                            ("--previous-report", args.previous_report),
                            ("--workers", args.workers),
                            ("--mail-capture-scope", args.mail_capture_scope),
                            ("--live-audience", args.live_audience),
                            ("--capture-page-budget", args.capture_page_budget),
                            ("--sharepoint-capture-scope", args.sharepoint_capture_scope),
                            ("--sharepoint-state", args.sharepoint_state),
                            ("--sharepoint-budget-seconds", args.sharepoint_budget_seconds),
                            ("--session-projection", args.session_projection)):
            if value is not None:
                build_args += [flag, str(value)]
        if args.bc_capture:
            build_args.append("--bc-capture")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            built = projection.main(build_args, environ=environ, bc_sender=bc_sender,
                                    bc_books=bc_books)
        if built == projection.EXIT_BUDGET:
            partial = projection.budget_partial(json.loads(out.getvalue()))   # its counts
            print(json.dumps({**partial, "probe": PROBE, "status": "budget_exhausted",
                              "phase": partial.get("phase"), "stage": "projection_build"},
                             sort_keys=True))
            return projection.EXIT_BUDGET
        if built != 0:
            # the build's own error line: its code, phase, store and disk (build_failure)
            print(json.dumps({**projection.build_failure(out.getvalue()), "probe": PROBE,
                              "error": "projection_build_failed",
                              "stage": "projection_build"}, sort_keys=True))
            return 1
        build = json.loads(out.getvalue())
        parts = {key: build[key] for key in ("blocked_upstream", "bc_capture", "sources",
                                            "sharepoint_capture", "partial_upstream",
                                            "build_seconds", "session_projection",
                                            *MAIL_COUNTS)
                 if key in build}
        if build.get("sources_blocked"):   # nothing was projected: measured nothing
            print(json.dumps({"probe": PROBE, "sources_blocked": build["sources_blocked"],
                              **parts}, sort_keys=True))
            return 0
        if args.session_replay is not None:
            phase = "session_replay"
            replayed = check_session_replay(
                projection.served_host(args.store, environ), args.case_dir,
                sessions=args.session_replay, sample=args.sample, per_type=args.per_type,
                budget_seconds=args.view_budget_seconds)
            replayed["resync"] = check_resync(args.db, environ if environ is not None
                                              else os.environ,
                                              budget_seconds=args.view_budget_seconds)
            print(json.dumps({"probe": PROBE, "session_replay": replayed, **parts,
                              "mirror_complete": build.get("mirror_complete") is True},
                             sort_keys=True))
            return 0 if session_replay_passed(replayed) else 1
        phase = "object_view"
        report = check_views(projection.served_host(args.store, environ), sample=args.sample,
                             per_type=args.per_type, max_opens=args.max_opens,
                             budget_seconds=args.view_budget_seconds)
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts, probe=PROBE, mirror_complete=build.get("mirror_complete") is True)
    report["find_denied_probed_unblocked"] = denied_probed_unblocked(report)
    unlinked = documents_unlinked(report)
    if unlinked is not None:   # after the find floor: no outage of any record's source
        report["blocked_upstream"] = [*report.get("blocked_upstream", ()), unlinked]
    print(json.dumps(report, sort_keys=True))
    ok = (report["denied_leaks"] == 0 and report["find_denied_leaks"] == 0
          and report["sharepoint_withheld_leaks"] == 0
          and report["workspace_errors"] == 0
          and report["originals"].get("rehash_mismatch", 0) == 0
          and not report["budget_exhausted"]
          and not (report.get("sharepoint_capture") or {}).get("read_back_leaks"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
