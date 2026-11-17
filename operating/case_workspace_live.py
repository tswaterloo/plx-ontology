"""The case workspace over the live source mirror: open a real case, render it per reader.

The ingestion runner (``python3 -m operating.ingestion run``) fills a state directory:
Portal relations into the case runtime's source mirror, mail/SharePoint/BC listings into
its own store; a persistent host (``operating.host``) may hold BC, mail and SharePoint
captures there too. This module never fills or refreshes them. It opens a case
**offline** over those captures (``CaseRuntime(client=None)``: no Workbench API client
exists here, so no Portal request can be made) through the generic path every MCP client
uses, then builds the console's workspace (``operator_api.workspace.case_workspace``: timeline,
originals, gaps, conflicts and actions together) for each reader, every
part re-resolved for that reader. A reader the mirror audience does not grant a relation
sees withheld markers, never the rows.

**Opening a case (the general path, no per-kind code).** The captures the state
directory holds (the Portal mirror, and BC, mail and SharePoint captures when present)
are read through one projection per run (``run_projection``): the persistent host's
served generation (``<DIR>/projection``), read-only and pinned for the run, when the
directory has one; else projected into ``case-workspace-live/ontology-<tenant digest>.sqlite``
(the one projector, ``ontology.project.build_store``; refused below 4 GiB free; a BC-only
directory answers BC-only). In one
in-process MCP session over that projection, for the investigator:

* the root: ``plx.find`` for ``KIND=IDENTIFIER`` (``KIND`` names the object type to look
  for: ``ao`` an assembly order, ``sales_order``, ``customer_po``, ``petra_po`` a purchase
  order, ``find`` any type). Exactly one candidate roots the case; several are refused
  (``ambiguous_identifier``), never picked. A customer PO (a kind unique only within a
  scope) roots only within its one customer and by its literal text; any other match of
  it is refused (``root_lead_only:<why>``), never opened on a lead. ``pick=TYPE[+LINKED]`` chooses at run time the
  newest object of an ontology type (by its latest event) that the investigator may read
  and, with ``+LINKED``, from which ``plx.around`` reaches a readable object of the linked
  type (its candidates are the objects within two links of one, however many are newer;
  a root reached only through a lead-tier link is labelled: ``pick.lead_tier`` and a
  ``pick_rests_on_lead`` case gap); nothing about any case is in code. A pick that finds
  nothing names its typed cause (``CaseOpener._none_found``: e.g.
  ``pick_source_not_captured:bc`` while a BC capture unit did not finish). When that cause
  is a capture's state (a source of the linked type, or of either type for an unjoined
  pick, not captured in full, or never configured: ``pick_source_not_configured:<source>``)
  the case is still rooted at the newest readable ``TYPE`` object, with a
  ``pick_linked_not_captured`` gap, and reported ``unlinked`` with that cause (never a
  rendered linked case: the run's ``cases_rendered`` does not count it, ``cases_unlinked``
  and ``failed`` do). ``pick=TYPE>LINKED`` (a join pick) roots the
  case in another system: the newest readable ``TYPE`` object (``join_carriers``: a
  declared ``TYPE -> LINKED`` link binding between two systems, or a declared path of two
  hops with one between two systems, such as a BC invoice's order renumbered in the Portal,
  the Portal order whose own number BC kept, or its shipment's order) whose carried
  identifier ``plx.find`` resolves to exactly one
  ``LINKED`` record of the other system (a root-making match at the carrier's declared
  field, the owning system's literal comparison; a carrier of a kind unique only within a
  scope, a customer PO, roots only a record whose scope party is the same record as the
  candidate's: ``scope_links``, joined at a verified or reviewed tier as
  ``ontology_tools.same_party`` reads it; a pair joined only by a weaker link is
  ``scope_unread``: a Portal<->BC customer link stays a lead until a person accepts it in
  the state directory's review store, ``project_state``); that record roots the case and
  the ``TYPE`` object (and a two-hop path's matched record) is cited beside it. A number
  carried from a BC record names a record only within its BC company: a match while the
  find shows another company's record holding it is never joined (``cross_scope``). Every
  newer candidate that joined nothing gets one typed reason (``JOIN_REASONS``, e.g.
  ``no_portal_row_for_external_document_number`` or ``scope_mismatch``), and every
  candidate one outcome per carrier (``carrier_outcomes``, keyed ``carrier_key``), counted
  in the result's ``pick``; the reasons are recorded on the case as one gap. When none of
  the newest candidates joins, the pick turns around: the ``LINKED`` records whose
  carried values a held ``TYPE`` record carries too propose older candidates, each
  decided by the same join (``pick.directions`` counts both directions);
* the evidence: ``plx.object`` of the root (every cited value, the value conflicts the
  tools report, such as a paper packet against FileMaker) and ``plx.investigate`` for
  every question family whose root type is the root's (``ontology/plans``);
* the case: ``plx.case_open``, then each cited value as a fact finding citing its row
  (with a structured value claim), each computed or selected answer as an interpretation,
  each conflict as an evidence-conflict gap citing both sides (never resolved, never
  summed), and each tool gap as a case gap. An absence the tools report is recorded as
  ``unknown`` (the case store accepts absence only with a cited row showing it); with
  ``--sharepoint-original`` the file the originals folder holds for the requested
  identifier is attached through the GET-only Graph reader (``operating.sharepoint_originals``).

**Portal outage.** When the latest ingestion run could not read Portal relations (a
``source_error`` on a ``portal_relation`` unit, e.g. the reporting API's HTTP 500), or
the host's last Portal capture failed or served its persisted copy, every case opened
records that as an ``inaccessible`` gap, an absence the tools report becomes
``inaccessible`` too, and a request whose root is not held is ``blocked`` with
``blocked_upstream:portal``: never ``absent`` and never a failure of the case.

**Source refusals.** The host ledger records, per configured source, the parts its latest
read was refused or not answered on (``host.blocked_parts``: e.g. Graph refusing every
mailbox lookup without User.Read.All); the run lists them in ``blocked_upstream`` beside
the Portal's, and a pick that finds nothing while a source of its linked (or picked) type
was refused is ``blocked`` with ``blocked_upstream:<source>`` (``host_refusals``), never a
failure: what the source refused is no evidence of none. A source the host's budget cut
(``host_capture_gaps``: ``source_state.complete = 0``) that a pick needed is blocked the
same way, its part ``cause: capture_gap`` (``error_code: capture_budget_cut``).

Case audience: the case store shows a case's question and roots to its whole audience,
and they hold the requested identifier (or, for a pick, the root's display value read
from a source row). ``plx.case_open`` is therefore given the investigator principals the
mirror audience grants on the relations those words came from (``_Session``): a reader
who may not read the source of a case's words cannot read the case. The per-reader leak
check counts any withheld value in those words as a row leak.

A request already opened by the same investigator with the same options (originals)
after the same ingestion run and the same review and confirmation decisions of its tenant
is reused (``case-workspace-live/opened.json``) unless ``--reinvestigate``: a case opened
before a person accepted or rejected an identity link is investigated again after it
(``_decisions``; a store that cannot be read reuses nothing). Picks are never reused.
Cases are written only to this state directory's own case store; no business system is
written. The projection the cases are opened over is the host's served generation when
it can stand for the tenant's (``served_generation``), else one file per tenant
(``projection_file``), built read-only from the captures and the tenant's decisions
(``project_state``).

Relationships (``relationships``): per reader, the summary counts the projection's own
ontology links around the case, never a join of this module's: the objects whose captured
versions the reader's workspace cites (resolved for that reader now) are read with that
reader's ``plx.object``, and every link the reader's ``plx.around`` (all tiers, leads and
contradicted ones included) and identity edges show at one of them is counted once per
link type (``relationship_edges``, e.g. ``Email.mentions_purchase_order`` or a document
link such as ``SupplierConfirmation.confirms``), beside the readable objects
(``relationship_nodes``) and what the bounded read left out (``relationship_truncated``).

Evidence: what the workspace shows is ``MIRROR_EVIDENCE`` (captures the ingestion runner
wrote; paged, non-atomic, possibly older than the source). Whether that runner read live
sources is recorded by its own run, never asserted here; ``live-verified`` comes only
from a the live verifier result (live check ``case.workspace_live``).

CLI (prints counts, booleans and enums only: no identifier, value, name or citation)::

    python3 -m operating.case_workspace_live --state-dir D --mirror-audience A \\
        --principal staff:operations --principal staff:quality --clearance confidential \\
        --open ao=<AO code> --open pick=PurchaseOrder+Receipt \\
        --reader staff:operations [--reader staff:operations,staff:quality] \\
        [--ingest-sources S] [--live-source-audience L --sharepoint-original \\
         --originals-expected E] [--output-dir OUT]

``--ingest-sources`` first runs ``python3 -m operating.ingestion run`` into ``D`` (the
credentialed live check; GET only), then opens and renders offline. ``--host-config``
instead runs one capture cycle of the host (``operating.host``: the Portal into the
source mirror, BC into ``D/bc-capture``; GET only), so a join pick has both systems. ``--output-dir``
writes each reader's full workspace JSON (row data: keep it local). The readers' row-data
check compares each reader's workspace with the rows of every relation that reader may
not read (``row_leaks``) and the printed summary with every cited row and ingested item
(``summary_row_data_check``); both must be 0.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Collection, Mapping, Sequence
import warnings

from .case_workspace_live_model import (
    BLOCKED_UPSTREAM, FORWARD_SHARE, INDEX_DIR, INDEX_FILE, JOIN_REASONS, KIND_TYPES, LEAD_GAPS,
    LEAD_KINDS, LEAD_STATEMENT, MAX_CITATIONS, MAX_FINDINGS, MAX_LEAD_PAGES, MAX_LEAD_PAGE_READS,
    MAX_REQUESTS, MAX_TWINS, MIRROR_EVIDENCE, MissingData, OPEN_KINDS, OPEN_LIVE_SECONDS, OWN_GAPS,
    OpenError, SCHEMA, UNLINKED_CAUSES, WHO, WITHHELD, _CLAIM_FIELD, _DOCUMENT_FIELD, _LEAD_KIND,
    _OBJECT_ID, _OPEN_SCHEMA, _SCOPE_WHY, _TYPE, _UNREAD_FIND_GAPS, _WITHHELD_FIND_GAPS, _hops,
    _system, carrier_key)
from .case_workspace_live_projection import (
    RunProjection, _StampedRuntime, _mirror_relation, _plain, _read_only, case_tokens, cited_rows,
    host_capture_gaps, host_refusals, ingested, projection_file, run_projection, upstream_parts)
from .case_workspace_live_evidence import (
    _code, _document_types, _evidence_reached, _freshness, _held, _l1_handle, _link_paths,
    _one_record, _reaches, _rooted, _shared_values, _unnumbered)
from .case_workspace_live_checks import (
    _audience_digest, _count, _decisions, leak_check, panel_totals, relationships, summary_check,
    workspace)
from .case_workspace_live_cli import (
    GAP_REASON, _GAP_ROOT_REASONS, _StderrCauses, _direction_totals, _host_books, _host_cycle,
    _ingest, _months, _reader, _totals, _write)

PICK_SCAN = 50  # candidates a pick confirms through the tools at most

# Wall clock one join pick may spend, both directions together (its plx.find calls may
# each probe BC live across companies and entity sets: the live check of 73775fc89 spent
# 37 minutes in the forward direction alone). Every live request inside it is refused
# before I/O once it has passed, and one sent waits at most the time left
# (``live_source_guard.RequestDeadline``); a candidate a refusal cut is not decided.
PICK_BUDGET_SECONDS = 420.0

MAX_GAPS = 60

MAX_REACHED = 30  # records the question families reached whose cards a case records


def _schema() -> Any:
    from .ontology_tools import default_schema

    held = _OPEN_SCHEMA.get()
    if held:
        return held[0]
    schema = default_schema()
    if schema is None:
        raise OpenError("ontology_schema_unavailable")
    if held is not None:
        held.append(schema)
    return schema


def parse_pick(text: str) -> tuple[str, str | None, bool]:
    """``TYPE``, ``TYPE+LINKED`` or ``TYPE>LINKED`` (ontology object types) -> (type,
    linked type, join). A join needs a declared link between two systems
    (``join_carriers``)."""
    join = ">" in str(text)
    kind, _sep, linked = str(text).partition(">" if join else "+")
    types = _schema().object_types
    if not _TYPE.fullmatch(kind) or kind not in types or (join and not linked) or (
            linked and (not _TYPE.fullmatch(linked) or linked not in types)):
        raise OpenError("pick must be OBJECT_TYPE, OBJECT_TYPE+LINKED_TYPE or "
                        "OBJECT_TYPE>LINKED_TYPE of the ontology schema")
    if join and not join_carriers(kind, linked):
        raise OpenError("pick TYPE>LINKED needs a declared link between two systems")
    return kind, linked or None, join


def join_carriers(object_type: str, linked: str) -> list[dict[str, Any]]:
    """The declared joins from ``object_type`` to ``linked`` across two systems, in schema
    order, each ``{from_binding, field, property, identifier_kind, to_binding, to_field,
    find_type, path, then}``: the ``field`` of a ``from_binding`` record carries a value
    that ``plx.find`` (for ``find_type``) matches at the ``to_field`` of a ``to_binding``
    record of the other system (the one cross-system hop); ``path`` names the declared
    links it follows. First the direct carriers (each pair of an ``object_type -> linked``
    link binding between two systems; the first a record carries is its direct carrier),
    then the paths of two hops, exactly one between two systems, each hop reaching one
    record (``_hops``), one per ``carrier_key``:

    * a hop within the record's own system, then the cross-system one, composed on the
      field both hops name on the record between them (``SalesInvoice.bills`` orderNumber
      -> a BC order's number <- ``SalesOrder.same_record_as`` renumberedTo, or the Portal
      order's own number when BC kept it, Manual Nos): the carried value is matched at the
      far field, no record between is read (BC deletes an order once invoiced); both hops
      must read it as one identifier kind when they name one, or as two kinds the
      identifier registry declares aliases of one another (bc_document_no and so_number:
      plx-customer-portal@340ef1e portal/src/lib/business-central/align-portal-so-number.ts
      sets order_number and renumberedTo to the BC No. together); such a path is a lead,
      as every join pick's start record is (``_investigate``);
    * the cross-system hop to another type, then ``then`` (``{link_type, direction,
      binding}``): the matched record's one ``linked`` record of ``binding`` reached by
      ``plx.around`` (``SalesInvoice.for_shipment`` shipment code -> ``Shipment.ships``).

    ``property`` is the first property the field is a source of, except for a kind unique
    only within a scope (a customer PO), which the root gate compares by its literal text:
    its first text property (``SalesInvoice.customer_po``), never a normalised identifier
    reading of the same field. A two-hop path never carries a scoped kind (its scope party
    is read along one declared link only) nor a hop whose binding has a ``where`` (the
    card shows no row to test it on). ``bc_company``: the carried value is read from a BC
    capture, so it names a record only within that record's BC company (a scoped kind's
    carrier excepted: its scope party is a record of that company); ``_join`` links its
    match only when no other company holds the number. Data only: the schema and the
    identifier registry."""
    from .ontology_tools import default_grammar
    from .ontology_tools_identity import scoped_kind

    schema = _schema()
    sources: dict[tuple[str, str], str] = {}
    texts: dict[tuple[str, str], str] = {}
    for prop in schema.object_types[object_type].get("properties") or ():
        for source in prop.get("sources") or () if isinstance(prop, Mapping) else ():
            sources.setdefault((source.get("binding"), source.get("path")), prop["name"])
            if prop.get("value_type") == "text":
                texts.setdefault((source.get("binding"), source.get("path")), prop["name"])
    grammar = default_grammar()

    def aliases(kinds: set[str]) -> bool:
        """Two kinds the identifier registry declares aliases of one another."""
        first, second = sorted(kinds)
        try:
            return (second in grammar.kind_spec(first).get("alias_kinds", ())
                    and first in grammar.kind_spec(second).get("alias_kinds", ()))
        except Exception:  # noqa: BLE001 - no grammar or an unknown kind: no alias
            return False

    def bc_company(binding_id: str, kind: Any) -> bool:
        spec = _binding(binding_id) or {}
        return spec.get("source") == "bc" and "from_capture" in (spec.get("lineage") or {}) \
            and not scoped_kind(grammar, kind)

    out: list[dict[str, Any]] = []
    for hop in _hops(schema, object_type, single=False):
        if hop["to_type"] != linked or _system(hop["from_binding"]) == _system(hop["to_binding"]):
            continue
        name = (texts if scoped_kind(grammar, hop["identifier_kind"])
                else sources).get((hop["from_binding"], hop["field"]))
        if name is not None:
            out.append({"from_binding": hop["from_binding"], "field": hop["field"],
                        "property": name, "identifier_kind": hop["identifier_kind"],
                        "to_binding": hop["to_binding"], "to_field": hop["to_field"],
                        "find_type": linked, "path": [hop["link_type"]], "then": None,
                        "bc_company": bc_company(hop["from_binding"], hop["identifier_kind"])})
    for first in _hops(schema, object_type, single=True):
        if first["where"] or first["to_type"] == object_type:
            continue
        for second in _hops(schema, first["to_type"], single=True):
            cross = [h for h in (first, second)
                     if _system(h["from_binding"]) != _system(h["to_binding"])]
            if second["to_type"] != linked or second["where"] or len(cross) != 1 \
                    or second["from_binding"] != first["to_binding"] \
                    or scoped_kind(grammar, cross[0]["identifier_kind"]):
                continue
            if cross[0] is second:  # composed on the field between the two hops
                kinds = {first["identifier_kind"], second["identifier_kind"]} - {None}
                if second["field"] != first["to_field"] or len(kinds) > 1 and not aliases(kinds):
                    continue
                carrier = {"find_type": linked, "then": None}
            elif first["to_type"] != linked:  # a linked record is the direct carrier's
                carrier = {"find_type": first["to_type"], "then": {
                    "link_type": second["link_type"], "direction": second["direction"],
                    "binding": second["to_binding"]}}
            else:
                continue
            name = sources.get((first["from_binding"], first["field"]))
            if name is None:
                continue
            carrier.update({"from_binding": first["from_binding"], "field": first["field"],
                            "property": name, "identifier_kind": cross[0]["identifier_kind"],
                            "to_binding": cross[0]["to_binding"],
                            "to_field": cross[0]["to_field"],
                            "path": [first["link_type"], second["link_type"]],
                            "bc_company": bc_company(first["from_binding"],
                                                     cross[0]["identifier_kind"])})
            same = ("from_binding", "field", "find_type", "to_binding", "to_field", "then")
            if any(all(c[k] == carrier[k] for k in same) for c in out):
                continue  # one try per comparison (a stand-in binding names it again)
            taken = {carrier_key(c) for c in out}
            for key in (carrier_key(carrier), f"{carrier_key(carrier)}/{name}"):
                if key not in taken:  # else only its last hop differs: not tried
                    out.append({**carrier, "key": key})
                    break
    return out


def scope_links(object_type: str, carrier: Mapping[str, Any], linked: str
                ) -> tuple[str, str] | None:
    """For a join carrier whose identifier kind is unique only within a scope
    (identifiers.json ``identity_scope``: a customer PO per customer), the two single-valued
    links that read the scope party: (from the ``object_type`` record, from the ``linked``
    record). The second is the ``linked`` link whose target-binding pair starts at the
    registry's ``scope_field`` of the target carrier (``customer_id``: ``ordered_by``), the
    first the one ``object_type`` link of the carrier's own binding to that party's type
    (``billed_to``). None when the registry or the schema declares no one such pair: the
    scope cannot be read. Data only: the registry and the schema."""
    from ontology import identifiers

    spec = _binding(carrier["to_binding"]) or {}
    try:
        found = identifiers.identity_scope(carrier.get("identifier_kind"), spec.get("relation"),
                                           carrier.get("to_field"))
    except Exception:  # noqa: BLE001 - an unknown kind declares no scope
        found = None
    if found is None:
        return None
    links = _schema().link_types

    def single(link: Mapping[str, Any], start: str, binding: str, field: str | None) -> bool:
        return link.get("from") == start and link.get("cardinality") in ("N:1", "1:1") and any(
            b.get("from_binding") == binding and (field is None or any(
                p.get("from") == field for p in b.get("pairs") or ()))
            for b in link.get("bindings") or ())

    there = [name for name, link in links.items()
             if single(link, linked, carrier["to_binding"], found[1])]
    if len(there) != 1:
        return None
    party = links[there[0]].get("to")
    here = [name for name, link in links.items() if link.get("to") == party
            and single(link, object_type, carrier["from_binding"], None)]
    return (here[0], there[0]) if len(here) == 1 else None


def parse_open(text: str) -> tuple[str, str]:
    """``KIND=IDENTIFIER`` -> (kind, identifier)."""
    kind, sep, identifier = str(text).partition("=")
    if not sep or kind not in OPEN_KINDS or not identifier.strip():
        raise OpenError(f"--open must be KIND=IDENTIFIER, KIND one of {', '.join(OPEN_KINDS)}")
    if kind == "pick":
        parse_pick(identifier.strip())
    return kind, identifier.strip()


def portal_upstream(state_dir: str | Path) -> list[str] | None:
    """Why the Portal captures this state directory holds may not be current, read-only:
    the ``source_error`` reasons of the Portal relation units of the latest finished
    ingestion run, and (``host_error:``) the host's last Portal capture when it failed or
    served its persisted copy; None when they read the Portal cleanly or neither ran. A
    Portal outage is then known, and named, instead of read as absence."""
    from .host import HOST_DIR, LEDGER_FILE
    from .ingestion import STORE_DIR, STORE_FILE

    reasons: set[str] = set()
    path = Path(state_dir) / STORE_DIR / STORE_FILE
    for (text,) in (_read_only(path, (
            "SELECT reasons_json FROM run_unit WHERE kind = 'portal_relation' AND run_id = "
            "(SELECT MAX(run_id) FROM run WHERE state = 'finished')")) or []
                    ) if path.is_file() else ():
        try:
            reasons.update(r for r in json.loads(text) if isinstance(r, str)
                           and r.startswith("source_error:"))
        except ValueError:
            continue
    host = Path(state_dir) / HOST_DIR / LEDGER_FILE
    for error, served in (_read_only(host, (
            "SELECT error, served_from FROM source_state WHERE source = 'portal' AND "
            "configured = 1")) or []) if host.is_file() else ():
        if error or served != "live":
            code = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(error or served))[:79]
            reasons.add(f"host_error:{code}")
    return sorted(reasons) or None


def offline_runtime(state_dir: str | Path, mirror_audience: Any, tenant: str, *,
                    live: Any = None, originals_options: Mapping[str, Any] | None = None,
                    environ: Mapping[str, str] | None = None):
    """The case runtime over the ingested mirror, with no Workbench API client.

    ``environ`` (default ``os.environ``) names the people exclusion the captured mail and
    Teams a case cites are served under; without one they are all withheld."""
    from integrations import capture_mail, capture_sharepoint
    from integrations.people_exclusion import PeopleExclusion

    from .case_runtime import CaseRuntime
    from .case_tools_host import CaseCaptures

    runtime = CaseRuntime(state_dir, client=None, audience=mirror_audience, tenant=tenant,
                          live=live, originals_options=originals_options,
                          people_exclusion=PeopleExclusion.from_env(
                              os.environ if environ is None else environ))
    # A case citing a captured mail or SharePoint document resolves it through the gates
    # the session's plx.read reads the state directory's captures with (as the MCP host).
    held = {f"{kind}_capture_dir": Path(state_dir) for kind, module in (
        ("mail", capture_mail), ("sharepoint", capture_sharepoint))
        if module.store_exists(Path(state_dir))}
    if held:
        runtime.captures = CaseCaptures(runtime, **held)
    return runtime


class _Session:
    """In-process MCP case sessions for one investigator over the projection, with the
    case audience bound to the provenance of the case's own words.

    The case store shows a case's question, roots and uncited items to every audience
    member (``operating.cases``: audience = the opener's principals). The opener writes
    an identifier into that text (the requested one, or a picked root's display value).
    So at ``plx.case_open`` every such token is traced to the relations of the cited rows
    read so far that hold it (``cited_rows``), and the case audience is narrowed to the
    investigator principals the mirror audience grants on one of those relations, for
    every token: a reader who may not read where an identifier came from cannot read the
    case. A token no row read so far holds does not narrow; the per-reader leak check
    (``leak_check``) still fails on it.

    The narrowed audience is frozen at open, so the per-token relation sets are also
    recorded with the case (``header_relations``): the case store re-checks them
    against the current mirror audience on every read, and a later revocation of the
    source grant hides the case (``operating.cases``).

    The mirror is offline, so the per-session call budget protects no source here; a
    new session replaces a spent one (the server refuses before executing, so nothing
    is repeated)."""

    def __init__(self, runtime: Any, actor: Any, audience: Any, store_path: Path | None,
                 name: str = "case-workspace-live", author_kind: str | None = None,
                 captures: Path | None = None,
                 bc_books: Mapping[str, Mapping[str, str]] | None = None,
                 marking_grants: Mapping[str, Any] | None = None,
                 warm: bool = True) -> None:
        """``warm=False``: no background SharePoint warm-up (``case_tools_host.
        warm_sharepoint``), for a batch reader that does not answer people's questions."""
        from integrations import capture_mail, capture_sharepoint

        from .case_tools_host import serving_tools
        from .mcp_server import META_RESERVE_BYTES, MCPServer, local_client

        served = runtime if author_kind is None else _StampedRuntime(runtime, author_kind)
        # The mail and SharePoint captures of the state directory (``captures``), as the
        # MCP host serves them: their units granted per reader from the capture audience
        # and every item re-gated (``CaptureCaseHostTools``); without one, default deny.
        held = {kind: captures if captures is not None and module.store_exists(captures)
                else None for kind, module in (("mail_capture_dir", capture_mail),
                                               ("sharepoint_capture_dir", capture_sharepoint))}
        # The opener's BC books (``_host_books``), so a live find pins the companies the
        # reverse direction and the capture read; none: the default map.
        bc = getattr(getattr(runtime, "live", None), "bc", None)
        books = None if bc_books is None or bc is None else bc_books.get(
            str(bc.environment).lower(), {})
        if held["sharepoint_capture_dir"] is not None and not warm:
            held["warm_sharepoint"] = False
        self.host = serving_tools(runtime, actor, store_path=store_path,
                                  reserve_bytes=META_RESERVE_BYTES, bc_books=books,
                                  marking_grants=marking_grants, **held)
        self._new = lambda: local_client(MCPServer(case_runtime=served, actor=actor,
                                                   ontology=self.host), name)
        self.current = self._new()
        self.actor, self.audience = actor, audience
        # An employee session presents only ``employee:<oid>``: narrow over what the
        # runtime's gate resolves for that employee now, never the bare principal.
        self._resolve = runtime.employee_gate
        self.opened: list[str] = []
        self.seen: list[tuple[str, set[str]]] = []  # (relation, value texts of one row)
        self.restricted: bool | None = None

    def close(self) -> None:
        self.host.close()

    def header_relations(self, question: Any, roots: Any) -> list[list[str]]:
        """Per identifier token of the case's words that rows read so far hold, the
        sorted relations of those rows (tokens no row holds are left out)."""
        groups: list[list[str]] = []
        for token in case_tokens([question, *(roots or [])]):
            upper = token.upper()
            relations = sorted({relation for relation, texts in self.seen
                                if any(text.upper() == upper or re.search(
                                    rf"(?<![A-Za-z0-9]){re.escape(upper)}(?![A-Za-z0-9])",
                                    text.upper()) for text in texts)})
            if relations and relations not in groups:
                groups.append(relations)
        return groups

    def case_audience(self, question: Any, roots: Any) -> list[str] | None:
        """The narrowed audience for ``plx.case_open``, or None (no narrowing needed)."""
        from ontology.core import Actor

        actor = self._resolve(self.actor)
        held = sorted(set(actor.principals) - {"*"})
        allowed = set(held)
        for relations in self.header_relations(question, roots):
            allowed &= {p for p in held if any(
                self.audience.allows(Actor(actor.tenant, frozenset({p}),
                                           actor.clearance), relation)
                for relation in relations)}
        if not allowed:
            raise OpenError("case_words_readable_by_no_investigator_principal")
        self.restricted = allowed != set(held)
        return sorted(allowed) if self.restricted else None

    def __call__(self, tool: str, arguments: dict) -> dict:
        if tool == "plx.case_open" and "audience" not in arguments:
            audience = self.case_audience(arguments.get("question"), arguments.get("roots"))
            if audience is not None:
                arguments = {**arguments, "audience": audience}
        if tool == "plx.case_open" and "header_relations" not in arguments:
            groups = self.header_relations(arguments.get("question"), arguments.get("roots"))
            if groups:
                arguments = {**arguments, "header_relations": groups}
        try:
            result = self.current(tool, arguments)
        except RuntimeError as error:
            if "rate_limit_exceeded" not in str(error):
                raise
            self.current = self._new()
            result = self.current(tool, arguments)
        if tool.startswith("plx.") and not tool.startswith("plx.case_"):
            self.seen.extend(cited_rows(result))
        if tool == "plx.case_open" and isinstance(result.get("case_id"), str):
            self.opened.append(result["case_id"])
        return result


class _Recorder:
    """Writes what the generic tools answered onto one case, through the case tools.

    Cited values become fact findings citing their row (with a value claim on the cited
    field when it is a row field), computed or selected answers interpretations, value
    conflicts evidence-conflict gaps citing every side, and tool gaps case gaps. Nothing
    is computed, summed or resolved here.

    The case store shows an uncited item to the whole case audience, so nothing derived
    from a source row is written uncited: an answer cites the rows it rests on (its own
    citation, its basis objects', or for a selected or computed value every source the
    answer reached), and the store withholds it from a reader who cannot read one of them.
    An answer whose sources cannot be cited is not written (fail closed); a tool gap naming
    an object cites that object or names only its type."""

    def __init__(self, call: Callable[[str, dict], dict], case_id: str, *,
                 upstream: Sequence[str] | None) -> None:
        self.call, self.case_id, self.upstream = call, case_id, upstream
        self.findings = self.gaps = 0
        self.skipped = {"findings": 0, "gaps": 0, "gap_cap": 0}
        self._seen: set[Any] = set()
        self._handles: dict[str, list[str] | None] = {}
        # Each lead document's pages as read ((text, citation) in page order; ended).
        self._pages: dict[str, tuple[list[tuple[str, str | None]], list[bool]]] = {}
        self.page_reads = 0   # plx.read page reads of every lead (MAX_LEAD_PAGE_READS)

    def object_handles(self, object_ids: Sequence[Any]) -> list[str] | None:
        """The row citations of each object (its card's cited values), or None when one
        of them cannot be read or cites nothing."""
        handles: list[str] = []
        for object_id in dict.fromkeys(object_ids):
            if not isinstance(object_id, str):
                return None
            if object_id not in self._handles:
                card = self.call("plx.object", {"object_id": object_id})
                found = [] if "error" in card else [
                    value["citation"]["l1_handle"] for value in card.get("values") or []
                    if isinstance(value, Mapping) and isinstance(value.get("citation"), Mapping)
                    and isinstance(value["citation"].get("l1_handle"), str)]
                self._handles[object_id] = list(dict.fromkeys(found)) or None
            if self._handles[object_id] is None:
                return None
            handles += self._handles[object_id]
        return handles

    @staticmethod
    def scoped(handles: Sequence[str]) -> list[str] | None:
        """One citation per mirror relation (the mirror grants by relation), every other
        citation kept; None when more than one finding may cite."""
        out: list[str] = []
        scopes: set[Any] = set()
        for handle in handles:
            found = _mirror_relation(handle)
            scope = ("relation", found[0]) if found else handle
            if scope not in scopes:
                scopes.add(scope)
                out.append(handle)
        return out if 0 < len(out) <= MAX_CITATIONS else None

    def _once(self, key: Any) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def value(self, item: Mapping[str, Any], object_type: Any, *, lead: bool = False) -> None:
        """One cited value as a fact finding. ``lead``: the record was reached only through
        lead-tier links (or a join pick's text match), so the finding is ``unknown`` and
        says its connection to the case is a lead, never evidence of it."""
        cite = item.get("citation") if isinstance(item.get("citation"), Mapping) else {}
        handle, field = cite.get("l1_handle"), cite.get("field_path")
        value = _plain(item.get("value"))
        if not isinstance(handle, str) or value is None or not self._once(
                ("value", handle, field, item.get("property"))):
            return
        if self.findings >= MAX_FINDINGS:
            self.skipped["findings"] += 1
            return
        name = f"{object_type}.{item.get('property')}"
        claims = None
        on_document = f"{name}: a value read from the cited document."
        document = object_type in _document_types()
        if isinstance(item.get("value"), Mapping):
            statement = f"{name}: a text value, in the cited source."
        elif (handle.startswith("graph-mail://") and document) or (
                handle.startswith("sharepoint://")
                and not (document and _DOCUMENT_FIELD.match(str(field)))):
            # A mail attachment's machine-read field (the claim gate re-reads the message
            # row, never the extraction), or a captured SharePoint file's own field (its
            # view carries no row): no figure is claimed; the value is read on the cited
            # document.
            statement = on_document
        elif isinstance(value, bool):
            statement = f"{name} = {'true' if value else 'false'}."
        else:
            text = str(value)
            extra = [str(x) for x in (item.get("unit"), item.get("currency"))
                     if isinstance(x, str)]
            role = item.get("quantity_role")
            statement = (f"{name} = {text}" + (f" {' '.join(extra)}" if extra else "")
                         + (f" (quantity role: {role})" if isinstance(role, str) else "") + ".")
            # A claim names the cited field's own value; a date or timestamp is claimed
            # too: the gate compares it as a date with the cell (its date as written, or
            # its instant), as the projection parsed it, and the statement's date needs it.
            if isinstance(field, str) and _CLAIM_FIELD.match(field) and len(text) <= 200:
                claims = [{"value": text, "citation": handle, "field": field}]
        if handle.startswith("sharepoint://") and not claims:
            statement = on_document   # a document's value is stated only with its claim
        caveat = (" Reached only through lead-tier links: this record's connection to the "
                  "case is a lead, not evidence.") if lead else ""
        arguments = {"case_id": self.case_id, "kind": "fact",
                     "status": "unknown" if lead else "supported",
                     "statement": statement + caveat, "citations": [handle]}
        # A SharePoint document's figure stands only with its claim (the claim gate
        # re-reads the pinned bytes' ``documentFields`` row, ``CaseCaptures``); without it
        # the value is read on the cited document, never uncovered prose.
        unclaimed = {"statement": on_document + caveat} if claims and handle.startswith(
            "sharepoint://") else {}
        for attempt in ([{"value_claims": claims}] if claims else []) + [unclaimed]:
            try:
                self.call("plx.case_add_finding", {**arguments, **attempt})
            except RuntimeError:
                continue
            self.findings += 1
            return
        self.gap("unknown", "citation_not_recordable",
                 f"{name}: a value the tools cite that this case host cannot cite")

    def interpretation(self, title: str, entry: Mapping[str, Any],
                       citations: Sequence[str] | None) -> None:
        value = entry.get("value")
        if value is None or isinstance(value, Mapping) or not self._once(
                ("answer", title, entry.get("name"), str(value))):
            return
        if citations is None:  # never written uncited: the store would show it to all
            self.gap("unknown", "answer_not_citable",
                     f"{title}: {entry.get('name')} (its sources could not be cited on this "
                     "case, so the answer is not recorded)")
            return
        lead = entry.get("lead_basis") is True
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        caveat = ", rests on lead-tier links: not evidence of the connection" if lead else ""
        claims: list[dict[str, str]] = []
        if entry.get("kind") == "count":
            # The count is the structure, never prose: one citation per counted record
            # (``answer``), so the claim gate has no uncovered number to check.
            statement = (f"{title}: {entry.get('name')} (count{caveat}) is the number of "
                         "records this finding cites.")
        elif entry.get("kind") == "property" and isinstance(entry.get("object_type"), str) \
                and isinstance(entry.get("property"), str):
            # Named as the cited property (as a fact is): the answer's own name may be a
            # quantity word, which would type the number it sits beside.
            statement = (f"{title}: {entry['object_type']}.{entry['property']} = {text} "
                         f"(answer {entry.get('name')}, property{caveat}).")
        else:
            # A computed quantity (a total over one unit's lines) is stated in its unit.
            extra = "".join(f" {x}" for x in (entry.get("unit"), entry.get("currency"))
                            if isinstance(x, str))
            statement = (f"{title}: {entry.get('name')} = {text}{extra} "
                         f"({entry.get('kind')}{caveat}).")
        if entry.get("kind") == "property":
            cite = entry.get("citation") if isinstance(entry.get("citation"), Mapping) else {}
            field = cite.get("field_path")
            if cite.get("l1_handle") in citations \
                    and isinstance(field, str) and _CLAIM_FIELD.match(field) \
                    and not isinstance(value, bool) and len(text) <= 200:
                claims = [{"value": text, "citation": cite["l1_handle"], "field": field}]
        status = ("supported" if entry.get("check") in ("passed", "not_applicable")
                  and not lead else "unknown")
        for attempt in [(state, extra) for state in dict.fromkeys((status, "unknown"))
                        for extra in ([{"value_claims": claims}] if claims else []) + [{}]]:
            try:
                self.call("plx.case_add_finding", {
                    "case_id": self.case_id, "kind": "interpretation", "status": attempt[0],
                    "statement": statement[:4000], "citations": list(citations),
                    **attempt[1]})
            except RuntimeError:
                continue
            self.findings += 1
            return
        self.skipped["findings"] += 1

    def conflict(self, conflict: Mapping[str, Any]) -> None:
        values = [v for v in conflict.get("values") or [] if isinstance(v, Mapping)]
        handles, claims = [], []
        for value in values:
            cite = value.get("citation") if isinstance(value.get("citation"), Mapping) else {}
            handle, field = cite.get("l1_handle"), cite.get("field_path")
            if not isinstance(handle, str):
                continue
            if handle not in handles:
                handles.append(handle)
            plain = value.get("value")
            if isinstance(plain, (str, int)) and not isinstance(plain, bool) \
                    and isinstance(field, str) and _CLAIM_FIELD.match(field):
                claims.append({"value": str(plain), "citation": handle, "field": field})
        subject = str(conflict.get("subject") or conflict.get("kind") or "values")
        why = str(conflict.get("note") or "The cited sources disagree; nothing here picks "
                  "one or adds them up.")
        self.gap("unknown", str(conflict.get("kind") or "value_disagreement"),
                 f"Evidence conflict: {subject}", detail=why, citations=handles[:20],
                 claims=claims[:40])

    def lead(self, lead: Mapping[str, Any]) -> None:
        """One ``missing_in_record`` lead of ``plx.object`` (a value a machine-read
        document states that the attested record fields hold empty) as an ``unknown``
        fact finding citing the document's pages and the empty record rows: never a value
        of the record and never a proposal (a person confirms the document belongs to the
        record). Without a document and a record citation it is not written.

        A lead it cannot record is a gap citing the lead's own sources (the document and
        the record rows), so the store withholds it from a reader of either side who may
        not read the other, as it withholds the lead; a lead whose sources cannot be
        cited is not written (``skipped``), never an uncited gap naming its fields."""
        from ontology.mentions import page_citation

        subject = str(lead.get("subject") or "a field")
        values = [v for v in lead.get("values") or [] if isinstance(v, Mapping)]
        sources = list(dict.fromkeys(h for h in map(_l1_handle, values) if h))
        if lead.get("kind") not in LEAD_KINDS:
            # Never recorded as a kind this case does not know; named, never dropped.
            self._lead_gap("lead_kind_unknown", f"{subject}: a plx.object lead of a kind "
                           "this case does not record", sources)
            return
        document = _l1_handle(values[0]) if values else None
        rows = list(dict.fromkeys(h for h in map(_l1_handle, values[1:]) if h))
        if document is None or not rows \
                or not self._once(("lead", subject, document, tuple(rows))):
            return
        if self.findings >= MAX_FINDINGS:
            self.skipped["findings"] += 1
            return
        stated = _plain(values[0].get("value"))
        text = str(stated).strip() if isinstance(stated, (str, int)) \
            and not isinstance(stated, bool) else ""
        # The pages of the field's extraction when this reader may read it, else the
        # first page of the captured text that states the value.
        pages = list(dict.fromkeys(
            page_citation(c["l1_handle"], c["page"])
            for c in lead.get("document_citations") or []
            if isinstance(c, Mapping) and c.get("l1_handle") == document
            and type(c.get("page")) is int and c["page"] > 0)) or self._page_of(document, text)
        unread = ", ".join(str(f) for f in lead.get("fields_not_read") or [])
        # The value itself is read on the cited page: the claim gate cannot re-read a
        # captured document's text, so the statement claims no figure.
        statement = _unnumbered(
            f"{LEAD_STATEMENT.format(kind=lead['kind'])}{subject}: the cited document "
            f"{'page ' if pages else ''}states a value the cited record fields hold empty"
            + (f" (fields not read: {unread})" if unread else "") + ". "
            + str(lead.get("note") or ""))
        try:
            self.call("plx.case_add_finding", {
                "case_id": self.case_id, "kind": "fact", "status": "unknown",
                "statement": statement[:4000],
                "citations": ((pages or [document]) + rows)[:MAX_CITATIONS]})
        except RuntimeError:
            self._lead_gap("lead_not_recordable", f"{subject}: a missing-in-record lead the "
                           "tools cite that this case host cannot cite", [document, *rows])
            return
        self.findings += 1

    def _lead_gap(self, reason: str, subject: str, sources: Sequence[str]) -> None:
        """A gap about a lead, citing the lead's sources (at most ``MAX_CITATIONS``, all
        or none): the case store shows an uncited gap to the whole case audience, so one
        whose sources cannot all be cited is not written."""
        if not sources or len(sources) > MAX_CITATIONS:
            self.skipped["gaps"] += 1
            return
        self.gap("unknown", reason, subject, citations=sources)

    def _page_of(self, document: str, text: str) -> list[str]:
        """The page citation (as ``plx.read`` cites it) of the first captured page of
        ``document`` whose text holds ``text``, within ``MAX_LEAD_PAGES``; [] when none
        does, the document has no captured pages or the case's page reads are spent.
        A page is read once per case (``_pages``, shared by every lead citing the
        document), and all leads share ``MAX_LEAD_PAGE_READS`` reads: past it a lead
        cites the document itself."""
        wanted = " ".join(text.split()).casefold()
        if not wanted:
            return []
        pages = self._pages.setdefault(document, ([], [False]))
        read_pages, ended = pages
        for held, citation in read_pages:
            if wanted in held and citation is not None:
                return [citation]
        while not ended[0] and len(read_pages) < MAX_LEAD_PAGES \
                and self.page_reads < MAX_LEAD_PAGE_READS:
            self.page_reads += 1
            try:
                read = self.call("plx.read", {"citation": document,
                                              "page": len(read_pages) + 1,
                                              "max_chars": 20000})
            except RuntimeError:
                read = {"error": "unreadable"}
            content = read.get("content") if "error" not in read else None
            if not isinstance(content, Mapping):
                ended[0] = True   # past the last captured page, or not readable
                break
            origin = content.get("origin") if isinstance(content.get("origin"), Mapping) \
                else {}
            held = " ".join(str(content.get("untrusted_text") or "").split()).casefold()
            citation = origin["citation"] if isinstance(origin.get("citation"), str) else None
            read_pages.append((held, citation))
            if wanted in held and citation is not None:
                return [citation]
        return []

    def gap(self, state: str, reason: str, subject: str, *, detail: str | None = None,
            citations: Sequence[str] = (), claims: Sequence[Mapping[str, Any]] = (),
            own: bool = False) -> None:
        # ``own``: a gap about the case itself (how its root was picked, a cap, an outage),
        # which the tools' and answers' gaps never crowd out (OWN_GAPS is kept for them).
        # A gap's text is uncited prose: a bare number or ISO date in it (a tool's count,
        # a capture time) is no claim any row holds, so it is written as "#" and the claim
        # gate has nothing unchecked; a date one of the gap's own claims states is kept
        # while the gap is recorded with that claim (the gate checks it).
        given, said = subject, detail
        subject, detail = _unnumbered(subject), _unnumbered(detail) if detail else detail
        why = reason + (f": {detail}" if detail else "")
        absent = ""
        if state == "absent_in_source":
            # The case store records absence only with a cited row showing it.
            state = "inaccessible" if self.upstream else "unknown"
            absent = (" (the tools reported it absent; recorded as "
                      + ("inaccessible: the Portal could not be read at ingestion, so this is "
                         "no evidence of absence)" if self.upstream else
                         "unknown: no cited row shows the absence)"))
        if not self._once(("gap", state, subject)):
            return
        if self.gaps >= MAX_GAPS - (0 if own else OWN_GAPS):
            self.skipped["gaps"] += 1
            self.skipped["gap_cap"] += 1
            return
        arguments = {"case_id": self.case_id, "missing": subject[:4000],
                     "why": (why + absent)[:4000], "who_can_answer": WHO, "state": state,
                     "citations": list(citations)}
        dates = {str(claim.get("value")) for claim in claims}
        claimed = {"missing": _unnumbered(given, dates)[:4000], "why": (reason + (
            f": {_unnumbered(said, dates)}" if said else "") + absent)[:4000]}
        for attempt in ([{"value_claims": list(claims), **claimed}] if claims else []) + [{}]:
            try:
                self.call("plx.case_record_gap", {**arguments, **attempt})
            except RuntimeError:
                continue
            self.gaps += 1
            return
        self.skipped["gaps"] += 1

    def tool_gaps(self, gaps: Any, origin: str, leads: Any = ()) -> None:
        """The tools' gaps as case gaps. A gap a tool derived from leads (``LEAD_GAPS``)
        also cites the documents of the ``leads`` shown with it and is written only cited
        (never the uncited types-only form): what documents state is read by their readers
        only."""
        documents = list(dict.fromkeys(
            handle for found in leads or () if isinstance(found, Mapping)
            for value in (found.get("values") or [])[:1] if isinstance(value, Mapping)
            for handle in [_l1_handle(value)] if handle))
        for gap in gaps or []:
            if not isinstance(gap, Mapping) or not isinstance(gap.get("subject"), str):
                continue
            subject = gap["subject"]
            detail = gap.get("detail") if isinstance(gap.get("detail"), str) else None
            named = _OBJECT_ID.findall(f"{subject}\n{detail or ''}")
            citations: Sequence[str] = ()
            claims: list[dict[str, str]] = []
            if gap.get("reason") in LEAD_GAPS:
                handles = self.object_handles(named) if named else None
                citations = (self.scoped([*handles, *documents])
                             if handles and documents else None) or ()
                if not citations:
                    self.skipped["gaps"] += 1
                    continue
            elif named:
                handles = self.object_handles(named)
                citations = (self.scoped(handles) if handles else None) or ()
                for object_id in dict.fromkeys(named) if citations else ():
                    # A number in a cited object's key (``{"id":900101}``) is claimed on
                    # the object's own cited row, so the claim gate checks it.
                    own = [h for h in self.object_handles([object_id]) or () if h in citations]
                    try:
                        key = json.loads(object_id.split(":", 2)[2])
                    except ValueError:
                        continue
                    if own and isinstance(key, dict):
                        claims += [{"value": str(value), "citation": own[0],
                                    "field": f"key.{name}"} for name, value in key.items()
                                   if type(value) is int and _CLAIM_FIELD.match(f"key.{name}")]
                if not citations:  # the objects cannot be cited: name only their types
                    def typed(match: re.Match) -> str:
                        return f"a {match.group(0).split(':', 1)[0]} record"
                    subject = _OBJECT_ID.sub(typed, subject)
                    detail = _OBJECT_ID.sub(typed, detail) if detail else None
            self.gap(str(gap.get("state") or "unknown"),
                     f"{origin}:{gap.get('reason') or 'gap'}", subject, detail=detail,
                     citations=citations, claims=claims[:40])

    def citations(self, entry: Mapping[str, Any],
                  everything: Callable[[], list[str] | None] | None) -> list[str] | None:
        """What one answer entry rests on: its own citation and its basis objects' rows; a
        selected or computed value (``everything`` given) rests on every source the answer
        reached. None when that cannot be cited."""
        cite = entry.get("citation")
        handles = [cite["l1_handle"]] if isinstance(cite, Mapping) and isinstance(
            cite.get("l1_handle"), str) else []
        basis = [b for b in entry.get("basis") or [] if b is not None]
        if basis:
            more = self.object_handles(basis)
            if more is None:
                return None
            handles += more
        if entry.get("kind") not in ("property", "count") and everything is not None:
            more = everything()
            if more is None:
                return None
            handles += more
        return handles or ([] if everything is None else None)

    def counted(self, entry: Mapping[str, Any]) -> list[str] | None:
        """One citation per record a count counts (its basis), or None when that is not
        exactly ``value`` records or more than one finding cites."""
        basis = list(dict.fromkeys(b for b in entry.get("basis") or [] if b is not None))
        if len(basis) != entry.get("value") or not 0 < len(basis) <= MAX_CITATIONS:
            return None
        handles = []
        for object_id in basis:
            own = self.object_handles([object_id])
            if not own:
                return None
            handles.append(own[0])
        return handles if len(set(handles)) == len(handles) else None

    def answer(self, plan: Mapping[str, Any], answer: Mapping[str, Any]) -> None:
        title = str(plan.get("title") or plan.get("family"))
        if "error" in answer:
            self.gap("unknown", f"plx.investigate:{_code(answer)}",
                     f"{title}: the question family did not run")
            return
        entries = [entry for entry in answer.get("answer") or [] if isinstance(entry, Mapping)]
        reached: list[str] | None | bool = False  # what it reached by evidence, lazily

        def everything() -> list[str] | None:
            nonlocal reached
            if reached is False:
                handles: list[str] | None = []
                for each in entries:
                    own = self.citations(each, None)
                    if own is None:
                        handles = None
                        break
                    handles += own
                more = (self.object_handles(_evidence_reached(answer))
                        if handles is not None else None)
                reached = None if more is None else self.scoped(handles + more)
            return reached

        for entry in entries:
            if entry.get("kind") == "property" and entry.get("lead_basis") is not True:
                self.value(entry, entry.get("object_type"))
            elif entry.get("kind") == "count" and isinstance(entry.get("value"), int):
                self.interpretation(title, entry, self.counted(entry))
            elif entry.get("value") is not None:
                own = self.citations(entry, everything)
                self.interpretation(title, entry, self.scoped(own) if own else None)
        for conflict in answer.get("conflicts") or []:
            self.conflict(conflict)
        # The family's verdict (truncated, partial, blocked) is recorded before its own
        # detail gaps, so a family with many detail gaps never loses its verdict to the
        # case's gap cap (the cap then cuts details, and names the cut).
        completeness = answer.get("completeness") or {}
        if completeness.get("truncated"):
            # The family answered a page of what it reached: the cut is named.
            self.gap("unknown", "answer_truncated", f"{title}: the answer is truncated",
                     detail=str(completeness.get("reason") or "truncated")[:60])
        state = (answer.get("answerability") or {}).get("state")
        if state in ("partial", "blocked"):
            reasons = ", ".join(str(r) for r in (answer.get("answerability") or {})
                                .get("reasons") or [])
            self.gap("unknown", f"plan_{state}", f"{title}: the answer is {state}",
                     detail=reasons or None)
        self.tool_gaps(answer.get("gaps"), "plx.investigate")

    def finish(self) -> None:
        if self.upstream:
            self.gap("inaccessible", "blocked_upstream",
                     "Current Portal records: the latest ingestion run could not read the "
                     "Portal reporting API",
                     detail=", ".join(self.upstream) + ". This case shows the last captures "
                     "held; nothing on it is absent because of the outage.", own=True)
        if self.skipped["findings"]:
            self.gap("unknown", "record_cap",
                     "More cited values than one case records",
                     detail=f"{self.skipped['findings']} values or answers were not recorded",
                     own=True)
        if self.skipped["gap_cap"]:
            self.gap("unknown", "gap_cap", "More gaps than one case records",
                     detail="the tools' later gaps were not recorded; ask the question "
                            "families again with plx.investigate to read them", own=True)


def _newest(db: sqlite3.Connection, object_type: str, near: str | None = None, *,
            only: Collection[str] | None = None) -> list[str]:
    """Object ids of ``object_type`` still held (no open removal: missing from the source
    or deleted by it, ``ontology.store.removed_at_sql``), newest (latest event) first; with
    ``near``, only those within two links of a held object of that type, so a record a
    document names is a candidate however many newer records it has; with ``only``, those
    of these ids (all of them, however old)."""
    from ontology.store import removed_at_sql
    if only is not None:
        return [row[0] for row in db.execute(
            "SELECT o.object_id FROM objects o "
            "LEFT JOIN event_objects eo ON eo.object_id = o.object_id "
            "LEFT JOIN events e ON e.event_id = eo.event_id "
            f"WHERE o.object_type = :type AND NOT {removed_at_sql('o.object_id')} "
            "AND o.object_id IN (SELECT value FROM json_each(:ids)) "
            "GROUP BY o.object_id ORDER BY MAX(COALESCE(e.time, e.window_end)) IS NULL, "
            "MAX(COALESCE(e.time, e.window_end)) DESC, o.first_recorded_at DESC, "
            "o.object_id DESC", {"type": object_type, "ids": json.dumps(sorted(only))})]
    reach = ("" if near is None else
             "AND o.object_id IN (WITH hop(id) AS ("
             "SELECT l.to_object FROM links l JOIN objects n ON n.object_id = l.from_object "
             f"WHERE n.object_type = :near AND NOT {removed_at_sql('n.object_id')} UNION "
             "SELECT l.from_object FROM links l JOIN objects n ON n.object_id = l.to_object "
             f"WHERE n.object_type = :near AND NOT {removed_at_sql('n.object_id')}) "
             "SELECT id FROM hop UNION "
             "SELECT l.to_object FROM links l JOIN hop ON l.from_object = hop.id UNION "
             "SELECT l.from_object FROM links l JOIN hop ON l.to_object = hop.id) ")
    return [row[0] for row in db.execute(
        "SELECT o.object_id FROM objects o "
        "LEFT JOIN event_objects eo ON eo.object_id = o.object_id "
        "LEFT JOIN events e ON e.event_id = eo.event_id "
        f"WHERE o.object_type = :type AND NOT {removed_at_sql('o.object_id')} " + reach +
        "GROUP BY o.object_id ORDER BY MAX(COALESCE(e.time, e.window_end)) IS NULL, "
        "MAX(COALESCE(e.time, e.window_end)) DESC, o.first_recorded_at DESC, "
        "o.object_id DESC LIMIT :cap", {"type": object_type, "near": near,
                                         "cap": PICK_SCAN * 10})]


def _join_counts(db: sqlite3.Connection, object_type: str, linked: str,
                 aliases: Any = None) -> dict[str, int]:
    """Why a ``+LINKED`` pick's linked objects join what they join, counts only:
    ``linked_held`` (objects of the type held), ``linked_to_start`` (of those, directly
    linked to a held ``object_type`` object; the pick also joins through one intermediate
    object, so this can read 0 while ``candidates_joined`` does not) and
    ``linked_refs_unheld`` (of those, the documents whose current reading states a
    reference in a ``refers_to`` field, e.g. a confirmation's PO number, that no held
    object answers as the document links are written: the writer's own resolver,
    ``mentions.mention_candidates``, its alias kinds, the reviewed aliases ``aliases``
    (``ontology.reviews.BuildAliases``) and its time-window overlap with the document's
    current version included; a PO the capture missed or a number the join misreads). The
    store indexes links by ``from_object`` and ``to_object``, identifiers by kind and value."""
    from ontology import mentions
    from ontology.store import removed_at_sql
    held = (f"SELECT o.object_id FROM objects o WHERE o.object_type = :type AND NOT "
            f"{removed_at_sql('o.object_id')}")
    ends = (f"SELECT l.from_object AS id, l.to_object AS other FROM links l "
            f"WHERE l.from_object IN ({held}) UNION ALL "
            f"SELECT l.to_object, l.from_object FROM links l WHERE l.to_object IN ({held})")
    alive = f"SELECT o.object_id FROM objects o WHERE NOT {removed_at_sql('o.object_id')}"
    row = db.execute(
        f"SELECT (SELECT COUNT(*) FROM ({held})), "
        f"(SELECT COUNT(DISTINCT e.id) FROM ({ends}) e JOIN objects t "
        f"ON t.object_id = e.other WHERE t.object_type = :start AND e.other IN ({alive}))",
        {"type": linked, "start": object_type}).fetchone()
    unheld = 0
    versions = db.cursor()
    versions.row_factory = sqlite3.Row
    for version in versions.execute(
            f"SELECT c.object_id, c.recorded_at, c.superseded_at, v.value, "
            f"{mentions.REMOVALS_COLUMN} FROM object_values v JOIN object_versions c ON "
            f"c.object_id = v.object_id AND c.l1_handle = v.l1_handle AND c.superseded_at "
            f"IS NULL JOIN objects o ON o.object_id = c.object_id "
            f"WHERE v.property = 'evidence' AND v.object_id IN ({held})", {"type": linked}):
        try:
            references = json.loads(version["value"]).get("references") or ()
        except (TypeError, ValueError, AttributeError):
            continue
        for ref in references:
            if not (isinstance(ref, Mapping) and ref.get("field") and ref.get("normalized")
                    and ref.get("identifier_kind")):
                continue
            answers = {found["object_id"] for found, _window, _span
                       in mentions.mention_candidates(db, ref, version, aliases)}
            if not answers or not db.execute(
                    f"SELECT 1 FROM objects o WHERE o.object_id IN "
                    f"({','.join('?' * len(answers))}) AND NOT "
                    f"{removed_at_sql('o.object_id')} LIMIT 1", sorted(answers)).fetchone():
                unheld += 1
                break
    return {"linked_held": row[0], "linked_to_start": row[1], "linked_refs_unheld": unheld}


def _bindings(object_type: str) -> list[Mapping[str, Any]]:
    return sorted(_schema().object_types[object_type].get("bindings") or (),
                  key=lambda b: str(b.get("id")))


def _unfinished(db: sqlite3.Connection, object_type: str) -> str | None:
    """The first source (by binding) of ``object_type`` whose capture this projection holds
    but did not finish (``store_meta.source_freshness``: a relation of its bindings, or of
    that source when the binding's relation is derived, never fully traversed or with a
    company unit not settled), else None. A relation the capture never configured is no
    claim either way (``_unconfigured``)."""
    freshness = _freshness(db)
    for binding in _bindings(object_type):
        relations = freshness.get(binding.get("source"))
        if not isinstance(relations, dict):
            continue
        facts = relations.get(binding.get("relation"))
        if any(isinstance(f, Mapping) and (f.get("last_full_traversal_at") is None or any(
                isinstance(c, Mapping) and c.get("complete") is False
                for c in f.get("scopes") or ()))
               for f in ([facts] if facts is not None else relations.values())):
            return str(binding["source"])
    return None


def _unconfigured(db: sqlite3.Connection, object_type: str) -> str | None:
    """The first source (by binding) of ``object_type`` the projection's captures never
    read (``source_freshness`` names other sources but none of this one's relations), else
    None; no freshness at all is no claim either way (a projection built before captures
    recorded it)."""
    freshness = _freshness(db)
    if not freshness:
        return None
    for binding in _bindings(object_type):
        source = binding.get("source")
        relations = freshness.get(source) if isinstance(source, str) and source else None
        if source and not (isinstance(relations, dict) and relations):
            return str(source)   # an unreadable entry is no claim the source was read
    return None


def _root_record(object_id: str, object_type: str) -> dict[str, Any]:
    """The root's object id and type, binding, and (a mirror binding) its relation and
    row key, read from the object id (``Type:binding:{key}``)."""
    _type, binding, key = (object_id.split(":", 2) + ["", ""])[:3]
    spec = _binding(binding) or {}
    try:
        parsed = json.loads(key)
    except ValueError:
        parsed = None
    return {"object_id": object_id, "object_type": object_type, "binding": binding,
            "relation": spec.get("relation") if spec.get("source") == "portal_api" else None,
            "key": parsed if isinstance(parsed, dict) else None}


def _binding(binding_id: str) -> Mapping[str, Any] | None:
    from ontology.resolve import binding_spec

    found = binding_spec(_schema(), binding_id)
    return found[1] if found else None


class CaseOpener:
    """Opens cases for one investigator over an offline runtime (see module docstring).

    ``originals``: a ``sharepoint_originals.folder_source`` whose file for the requested
    identifier is attached, or None; ``author_kind``: the opening session's
    ``SessionIdentity.author_kind`` (``human`` for a console person), recorded as the case
    question's author; None keeps the in-process server's ``agent``; ``internal_domains``:
    the mail capture's own domains (projection). The projection is built on the first
    open of this opener."""

    def __init__(self, runtime: Any, actor: Any, state_dir: str | Path, *, audience: Any,
                 originals: Mapping[str, Any] | None = None,
                 internal_domains: Sequence[str] = (),
                 author_kind: str | None = None,
                 bc_books: Mapping[str, str] | None = None) -> None:
        if author_kind not in (None, "human", "agent"):
            raise OpenError("author_kind_invalid")
        self.author_kind = author_kind
        if getattr(runtime, "client_configured", True):
            # The workspace reads the ingested mirror only: never the Portal API.
            raise OpenError("runtime_must_be_offline")
        if not callable(getattr(audience, "allows", None)):
            raise OpenError("mirror_audience_required")
        self.runtime, self.actor, self.audience = runtime, actor, audience
        self.state_dir = Path(state_dir)
        self.originals = dict(originals) if originals else None
        self.internal_domains = list(internal_domains)
        self.index_path = Path(state_dir) / INDEX_DIR / INDEX_FILE
        self.projection_path = projection_file(state_dir, actor.tenant)
        self.upstream = portal_upstream(state_dir)
        self.refused = host_refusals(state_dir)
        # Sources whose latest capture the host's budget cut (``host_capture_gaps``) and
        # those of them a case was blocked on (the summary names only these).
        self.capture_gaps = host_capture_gaps(state_dir)
        self.gap_blocked: dict[str, dict[str, Any]] = {}
        self._projected = False
        self.run_projection: RunProjection | None = None
        self._pick_stats: dict[str, Any] | None = None
        # The objects a ``lead_tier`` pick reached through its lead-tier link (never
        # printed: the case's pick_rests_on_lead gap cites them).
        self._pick_lead: list[str] = []
        # The capture-state cause a ``+LINKED`` pick rooted its case without the linked
        # record under (``UNLINKED_CAUSES``), else None.
        self._unlinked: str | None = None
        # The BC company books a live lookup pins (``bc_read.load_company_books``'s
        # environment -> company -> book; None: ``bc_company_books.json``) and
        # the cards of records a join pick read live, by their card's object id.
        self.bc_books = bc_books
        self._live_cards: dict[str, dict[str, Any]] = {}
        self._open_budget: Any = None  # the open's live-request deadline, while one runs
        self.root: dict[str, Any] | None = None  # the last opened case's root (in-process)

    def _index(self) -> dict[str, str]:
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(data, dict) else {}

    def _remember(self, key: str, case_id: str) -> None:
        index = self._index()
        index[key] = case_id
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile

        handle, name = tempfile.mkstemp(prefix=".opened-", suffix=".tmp",
                                        dir=self.index_path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(index, stream, indent=1, sort_keys=True)
            os.replace(name, self.index_path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def _key(self, kind: str, identifier: str) -> str | None:
        """Reuse key: investigator, request, the options that shape the case (originals),
        the latest finished ingestion run, the mirror audience the case audience was
        narrowed under and the tenant's review and confirmation decisions (``_decisions``),
        so a case opened with other options, before a newer ingestion, under other source
        grants or before a person's decision is investigated again. None (never reused or
        remembered) while a decision store cannot be read."""
        from hashlib import sha256

        from .cases import actor_label

        decisions = _decisions(self.state_dir, self.actor.tenant)
        if decisions is None:
            return None
        options = sha256(json.dumps({
            "originals": None if self.originals is None else {
                key: self.originals.get(key) for key in ("drive_id", "folder")},
            "ingestion_run": _last_ingestion_run(self.state_dir),
            "audience": _audience_digest(self.audience),
            "decisions": decisions},
            sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        return f"{actor_label(self.actor)}|{kind}={identifier.strip().upper()}|{options}"

    def projection(self) -> Path:
        """The projection of this state directory's captures, chosen once per opener
        (:func:`run_projection`: the host's served generation, pinned, when there is one
        this tenant may use; else built privately). :meth:`close` releases it."""
        if not self._projected:
            self.run_projection = run_projection(self.state_dir, self.actor.tenant,
                                                 internal_domains=self.internal_domains)
            self.projection_path = self.run_projection.path
            self._projected = True
        return self.projection_path

    def close(self) -> None:
        """Release the pinned served generation, if any (idempotent)."""
        if self.run_projection is not None:
            self.run_projection.close()

    def open_case(self, kind: str, identifier: str, *, reinvestigate: bool = False
                  ) -> dict[str, Any]:
        """``{"request", "status": opened|reused|unlinked|blocked|failed, "case_id"?,
        "missing"?, "pick"?}`` (``pick``: a join pick's counts and reason codes, also when it
        failed; ``unlinked``: a ``+LINKED`` pick rooted its case without the linked record
        under a capture-state cause, ``missing``).
        ``self.root`` then names the opened case's root (object id and type, and its
        mirror row: relation and key) for an in-process caller; never printed."""
        token = _OPEN_SCHEMA.set([])
        try:
            result = self._open(kind, identifier, reinvestigate)
        finally:
            _OPEN_SCHEMA.reset(token)
        if self._pick_stats is not None:
            result["pick"] = self._pick_stats
        return result

    def _open(self, kind: str, identifier: str, reinvestigate: bool) -> dict[str, Any]:
        from .cases import CaseNotFound

        request = kind
        self._pick_stats = self.root = self._unlinked = None
        self._pick_lead = []
        if kind not in OPEN_KINDS:  # an unknown request kind is refused, never guessed
            return {"request": request, "status": "failed", "missing": "unknown_kind"}
        key = self._key(kind, identifier)
        if kind != "pick" and key is not None and not reinvestigate:
            known = self._index().get(key)
            if known is not None:
                try:
                    self.runtime.cases.verify_chain(self.actor, known)
                except CaseNotFound:
                    pass  # no longer readable for this investigator: investigate anew
                else:
                    return {"request": request, "status": "reused", "case_id": known}
        try:
            call = _Session(self.runtime, self.actor, self.audience, self.projection(),
                            author_kind=self.author_kind, captures=self.state_dir,
                            bc_books=self.bc_books)
        except OpenError as error:
            return {"request": request, "status": "failed", "missing": error.code}
        except Exception as error:  # a failure is named, never a silent pass
            return {"request": request, "status": "failed",
                    "missing": f"error:{type(error).__name__}"}
        opened = call.opened
        from integrations.live_source_guard import RequestDeadline

        try:
            with RequestDeadline(OPEN_LIVE_SECONDS).active() as self._open_budget:
                case_id = self._investigate(call, kind, identifier)
        except OpenError as error:
            status = "blocked" if error.code.startswith("blocked_upstream:") else "failed"
            return {"request": request, "status": status, "missing": error.code}
        except MissingData as error:
            return {"request": request, "status": "failed",
                    "missing": f"missing_data:{error.code}"}
        except RuntimeError as error:
            tool, sep, reason = str(error).partition(" refused: ")
            code = re.match(r"[a-z_]{1,60}", reason or "")
            return {"request": request, "status": "failed",
                    "missing": f"refused:{tool if sep else 'tool'}"
                               + (f":{code[0]}" if code else "")}
        except Exception as error:  # a failure is named, never a silent pass
            return {"request": request, "status": "failed",
                    "missing": f"error:{type(error).__name__}"}
        finally:
            self._open_budget = None
            call.close()
        case_id = case_id or (opened[-1] if opened else None)
        if case_id is None:
            return {"request": request, "status": "failed", "missing": "case_not_opened"}
        if kind != "pick" and key is not None:
            self._remember(key, case_id)
        if self._unlinked:   # rooted without the linked record: the cause stays named
            return {"request": request, "status": "unlinked", "case_id": case_id,
                    "missing": self._unlinked, "audience_narrowed": bool(call.restricted)}
        return {"request": request, "status": "opened", "case_id": case_id,
                "audience_narrowed": bool(call.restricted)}

    # ---- the general path ----------------------------------------------------

    def _find(self, call: Callable[[str, dict], dict], identifier: str,
              object_type: str | None) -> Mapping[str, Any]:
        arguments: dict[str, Any] = {"query": identifier[:200]}
        if object_type is not None:
            arguments["object_type"] = object_type
        found = call("plx.find", arguments)
        if "error" in found:
            raise OpenError(f"find_error:{_code(found)}")
        listed = found.get("candidates") or []
        candidates = [c["object"] for c in listed]
        if found.get("status") == "found" and len(candidates) == 1:
            return _rooted(listed[0], identifier)
        if found.get("status") == "ambiguous":
            if _one_record(call, candidates):
                return _rooted(listed[0], identifier)
            raise OpenError("ambiguous_identifier")
        if self.upstream:
            raise OpenError(BLOCKED_UPSTREAM)
        states = sorted({str(gap.get("state")) for gap in found.get("gaps") or []
                         if isinstance(gap, Mapping) and gap.get("state")})
        raise MissingData("not_found" + (f":{states[0]}" if len(states) == 1 else ""))

    def _pick(self, call: Callable[[str, dict], dict], spec: str
              ) -> tuple[Mapping[str, Any], str, list[str]]:
        """The newest readable object of the type (by its latest event) from which a
        readable object of the linked type is reached within two links (root, its display
        value, objects to cite beside it); a join pick is ``_join_pick``. A ``+LINKED``
        pick's counts are ``self._pick_stats`` (``lead_tier``: the root was chosen only
        because a lead-tier link reaches the linked object); nothing picked raises its
        typed cause (``_none_found``)."""
        object_type, linked, join = parse_pick(spec)
        if join:
            return self._join_pick(call, object_type, linked)
        # Only the order of candidate ids and the link path to try come from the store;
        # whether the investigator may read the object and what the path reaches is the
        # tools' answer.
        db = sqlite3.connect(self.projection().resolve().as_uri() + "?mode=ro", uri=True)
        try:
            ordered = _newest(db, object_type, linked)
            candidates = [(oid, _link_paths(db, oid, linked) if linked else [None])
                          for oid in ordered]
            joins = _join_counts(db, object_type, linked, self._aliases()) if linked else {}
            cause = self._none_found(db, object_type, linked,
                                     any(paths for _oid, paths in candidates))
            # A cause that is a capture's state, not the records': the case is still
            # rooted at the newest readable object of the type, its linked record a gap.
            fallback = _newest(db, object_type) if linked and cause.startswith(
                UNLINKED_CAUSES) else []
        finally:
            db.close()
        stats: dict[str, Any] | None = None
        if linked:
            stats = self._pick_stats = {"mode": "linked", "scanned": 0, "lead_tier": False,
                                        "candidates": len(candidates),
                                        "candidates_joined": sum(
                                            1 for _oid, paths in candidates if paths),
                                        **joins}
        tried = 0
        for object_id, paths in candidates:
            if not paths or tried >= PICK_SCAN:
                continue
            tried += 1
            if stats is not None:
                stats["scanned"] = tried
            card = call("plx.object", {"object_id": object_id})
            if "error" in card:
                continue
            if linked:
                tiers: dict[str | None, list[str]] = {}
                for path in paths:
                    tier, reached = _reaches(call, object_id, path)
                    tiers.setdefault(tier, reached)
                    if "evidence" in tiers:
                        break
                if not tiers.keys() - {None}:
                    continue
                stats["lead_tier"] = "evidence" not in tiers
                self._pick_lead = tiers.get("lead", []) if stats["lead_tier"] else []
            display = _plain(card["object"].get("display"))
            return card["object"], str(display or object_id), []
        # Nothing picked while the Portal could not be read, or a source of either type
        # was refused, is no evidence of none.
        blocked = BLOCKED_UPSTREAM if self.upstream else self._refused_block(linked,
                                                                            object_type)
        if blocked:
            raise OpenError(blocked)
        # A linked source the host's budget cut is a typed capture gap: the record it did
        # not read may be the one, so the case is blocked on it, not rooted without it.
        missing = cause.partition(":")[2] if cause.startswith(
            "pick_source_not_captured:") else None
        if missing in getattr(self, "capture_gaps", {}):
            self.__dict__.setdefault("gap_blocked", {})[missing] = self.capture_gaps[missing]
            raise OpenError(f"blocked_upstream:{missing}")
        for object_id in fallback[:PICK_SCAN]:
            card = call("plx.object", {"object_id": object_id})
            if "error" in card:
                continue
            self._unlinked = cause
            display = _plain(card["object"].get("display"))
            return card["object"], str(display or object_id), []
        raise OpenError(cause)

    def _refused_block(self, *object_types: str | None) -> str | None:
        """``blocked_upstream:<source>`` for the first source (by binding) of
        ``object_types`` whose latest host read was refused (``host_refusals``), else None."""
        for object_type in filter(None, object_types):
            for binding in sorted(_schema().object_types[object_type].get("bindings") or (),
                                  key=lambda b: str(b.get("id"))):
                if binding.get("source") in self.refused:
                    return f"blocked_upstream:{binding['source']}"
        return None

    def _aliases(self) -> Any:
        """The reviewed aliases the projection's document links were written with
        (``build_steps.post_project``'s ``mentions`` step: the state directory's review
        store for this tenant, read-only)."""
        from ontology import reviews

        from .build_steps import ReviewsIfPresent
        return reviews.BuildAliases(ReviewsIfPresent(reviews.store_path(self.state_dir),
                                                     strict=True), self.actor.tenant)

    @staticmethod
    def _none_found(db: sqlite3.Connection, object_type: str, linked: str | None,
                    joined: bool) -> str:
        """Why a pick would find nothing, from the projection only: no ``linked`` object
        held (``pick_linked_absent``), none within two links of an ``object_type`` object
        (``pick_linked_unjoined``), both while a capture of that type's source (for an
        unjoined pick: of either type's source, the picked type's first) did not finish
        (``pick_source_not_captured:<source>``: a record the capture missed is no evidence
        of none), no ``linked`` object held while a source of that type was never captured
        (``pick_source_not_configured:<source>``, ``_unconfigured``; after an unfinished
        one), else the candidates the tools did not show this investigator reaching it
        (``pick_not_reached``; a plain pick: ``pick_none_found``). A held linked document
        naming a record no held object answers does not change the cause: the pick still
        counts it (``linked_refs_unheld``, ``_join_counts``), and the linked capture that
        did not finish may hold the document naming a record that is held."""
        if linked and not _held(db, linked):
            missing = _unfinished(db, linked)
            if missing:
                return f"pick_source_not_captured:{missing}"
            # None held while a source of the type was never captured (a customer PO
            # mailed, not filed, when the check captures no mail): no evidence of none.
            missing = _unconfigured(db, linked)
            return f"pick_source_not_configured:{missing}" if missing else "pick_linked_absent"
        if not joined:
            # The linked type is held but joins no candidate: a link the unfinished
            # capture of either side missed is no evidence of none (a mail capture that
            # read 2 of 10 folders holds some confirmations, not the ones naming a PO).
            missing = _unfinished(db, object_type) or (_unfinished(db, linked)
                                                       if linked else None)
            if missing:
                return f"pick_source_not_captured:{missing}"
            return "pick_linked_unjoined" if linked else "pick_none_found"
        return "pick_not_reached" if linked else "pick_none_found"

    def _join_pick(self, call: Callable[[str, dict], dict], object_type: str, linked: str
                   ) -> tuple[Mapping[str, Any], str, list[str]]:
        """The newest readable ``object_type`` record whose carried identifier joins
        exactly one ``linked`` record of the other system (``_join``): that record is the
        root, the candidate is cited beside it. Every candidate scanned before it (at most
        ``PICK_SCAN``) gets its one reason in ``self._pick_stats``, and every scanned
        candidate one outcome per carrier (``carrier_outcomes``).

        When none of those newest candidates joins (the two systems may hold different
        populations: BC documents naming rows the served replica never holds), the pick
        turns around (``directions.reverse``): the ``linked`` records whose carried values
        some held ``object_type`` record carries too (``_shared_values``, over the
        projection, however old either record is; a target relation the mirror audience
        does not grant the investigator is never read) propose candidates, newest first, at
        most ``PICK_SCAN`` of them (those sharing an order number first, then those sharing
        only a scoped value such as a customer PO, which many orders may share), each
        decided by the same ``_join`` through the tools, so a picked case exists in both
        systems whenever the projection shows one that may; then BC itself is asked
        (``_bc_reverse``). The reverse direction is not run when no carrier it may read
        could join (a Portal that could not be read: ``BLOCKED_UPSTREAM``).

        Both directions share one wall clock (``PICK_BUDGET_SECONDS``, every live request
        bounded by it), the forward one at most ``FORWARD_SHARE`` of it, so the reverse
        one always gets the rest: a direction a clock cut says ``budget_spent``, the
        candidate a refused request cut is not decided (neither counted scanned nor given
        a reason), the reverse direction is not run once the whole clock has passed, and
        a pick cut with nothing joined fails ``pick_budget_spent``, never
        ``pick_none_found``. ``directions`` counts each
        direction (``forward``: candidates scanned; ``reverse``: records carrying a joinable
        value on either side under the relation grants, linked records matched, candidates
        scanned and their reasons, and the BC lookups), counts only."""
        from integrations.live_source_guard import RequestDeadline

        budget = RequestDeadline(PICK_BUDGET_SECONDS)
        with budget.active():
            return self._join_pick_within(call, object_type, linked, budget)

    def _refusals(self, *budgets: Any) -> int:
        """Requests the pick's deadlines (``budgets``) and the open's refused so far."""
        return sum(d.refused for d in (*budgets, self._open_budget) if d is not None)

    def _join_pick_within(self, call: Callable[[str, dict], dict], object_type: str,
                          linked: str, budget: Any
                          ) -> tuple[Mapping[str, Any], str, list[str]]:
        """``_join_pick`` with its deadline ``budget`` active."""
        from integrations.live_source_guard import RequestDeadline

        carriers = join_carriers(object_type, linked)
        stats: dict[str, Any] = {"mode": "join", "scanned": 0, "joined": False,
                                 "root_reasons": {}, "carrier_outcomes": {}}
        forward = {"scanned": 0, "joined": False, "budget_spent": False}
        reverse = {"tried": False, "start_held": 0, "linked_held": 0, "linked_matched": 0,
                   "scanned": 0, "joined": False, "reasons": {}, "budget_spent": False,
                   "bc": {"reader": False, "orders": 0, "orders_unreadable": 0,
                          "requests": 0, "lookups": 0, "lookup_states": {}, "hits": 0,
                          "scanned": 0}}
        stats["directions"] = {"forward": forward, "reverse": reverse}
        self._pick_stats = stats
        self._live_cards = {}
        db = sqlite3.connect(self.projection().resolve().as_uri() + "?mode=ro", uri=True)
        try:
            ordered = _newest(db, object_type)
        finally:
            db.close()
        memo: dict[tuple, Any] = {}  # scope parties and same-record answers of this pick

        def note(counts: dict[str, int], reason: str) -> None:
            counts[reason] = counts.get(reason, 0) + 1

        def mine_of(object_id: str) -> list[dict[str, Any]]:
            return [c for c in carriers if c["from_binding"] == object_id.split(":", 2)[1]]

        def picked(root: Mapping[str, Any], object_id: str, through: list[str]
                   ) -> tuple[Mapping[str, Any], str, list[str]]:
            stats["joined"] = True
            return root, str(_plain(root.get("display")) or root["object_id"]), [
                object_id, *through]

        def failed() -> OpenError:
            # Nothing joined while the Portal could not be read or a source of either
            # type was refused is no evidence of none, nor is a pick its deadline cut.
            if self.upstream:
                return OpenError(BLOCKED_UPSTREAM)
            return OpenError(self._refused_block(linked, object_type) or (
                "pick_budget_spent" if forward["budget_spent"] or reverse["budget_spent"]
                else "pick_none_found"))

        seen: set[str] = set()
        # The forward direction spends at most its share; the rest is the reverse one's.
        share = RequestDeadline(max(0.0, budget.left() * FORWARD_SHARE))
        with share.active():
            for object_id in ordered:
                mine = mine_of(object_id)
                if not mine or stats["scanned"] >= PICK_SCAN:
                    continue  # a record of a binding that carries no join is no candidate
                if share.passed:
                    forward["budget_spent"] = True
                    break
                refused = self._refusals(budget, share)
                card = call("plx.object", {"object_id": object_id})
                if "error" not in card:
                    reason, root, outcomes, through = self._join(call, linked, card, mine,
                                                                 memo)
                if self._refusals(budget, share) > refused:
                    forward["budget_spent"] = True  # cut by the deadline: not decided
                    break
                seen.add(object_id)
                stats["scanned"] += 1
                forward["scanned"] = stats["scanned"]
                if "error" in card:
                    note(stats["root_reasons"], "root_not_readable")
                    continue
                for key, outcome in outcomes:
                    note(stats["carrier_outcomes"].setdefault(key, {}), outcome)
                if root is not None:
                    forward["joined"] = True
                    return picked(root, object_id, through)
                note(stats["root_reasons"], reason)
        # The reverse direction: the linked records whose values a start record carries.
        actor = self.runtime.employee_gate(self.actor)
        readable = [c for c in carriers if not any(
            spec.get("source") == "portal_api"
            and not self.audience.allows(actor, spec.get("relation"))
            for spec in [_binding(c["to_binding"]) or {}])]
        if self.upstream:
            # A Portal carrier joins nothing while the Portal could not be read (``_join``).
            readable = [c for c in readable if _system(c["to_binding"]) != "portal"
                        and _system((c["then"] or {}).get("binding") or "") != "portal"]
        if not readable or budget.passed:
            # Nothing the reverse direction could join, or no time left: it is not tried.
            raise failed()
        reverse["tried"] = True
        db = sqlite3.connect(self.projection().resolve().as_uri() + "?mode=ro", uri=True)
        try:
            shared = _shared_values(db, object_type, linked, readable)
            proposed = [oid for key in ("start", "start_scoped") for oid in _newest(
                db, object_type, only=[oid for oid in shared[key] if oid not in seen])]
        finally:
            db.close()
        reverse.update({key: shared[key]
                        for key in ("start_held", "linked_held", "linked_matched")})
        for object_id in proposed:
            mine = mine_of(object_id)
            if not mine or reverse["scanned"] >= PICK_SCAN:
                continue
            if budget.passed:
                reverse["budget_spent"] = True
                break
            refused = self._refusals(budget)
            card = call("plx.object", {"object_id": object_id})
            if "error" not in card:
                reason, root, _outcomes, through = self._join(call, linked, card, mine, memo)
            if self._refusals(budget) > refused:
                reverse["budget_spent"] = True
                break
            reverse["scanned"] += 1
            if "error" in card:
                note(reverse["reasons"], "root_not_readable")
                continue
            if root is not None:
                reverse["joined"] = True
                return picked(root, object_id, through)
            note(reverse["reasons"], reason)
        if not reverse["budget_spent"]:
            found = self._bc_reverse(call, object_type, linked, readable, memo, budget)
            if found is not None:
                return picked(*found)
        raise failed()

    def _bc_reverse(self, call: Callable[[str, dict], dict], object_type: str, linked: str,
                    carriers: Sequence[Mapping[str, Any]], memo: dict[tuple, Any],
                    budget: Any) -> tuple[Mapping[str, Any], str, list[str]] | None:
        """The reverse direction's second half, when the start records the projection
        holds name none of the linked ones (the BC capture may hold only a few invoices):
        the newest held ``linked`` records the investigator may read (``plx.object``, at
        most ``PICK_SCAN``), each value they hold at a direct carrier's ``to_field`` asked
        through ``plx.find`` for the start type, by the carrier's identifier kind, over
        BC only (``ontology.resolve.only_sources``): the one resolver path, so a BC
        company unit the read policy withholds from the investigator is never asked
        (``withheld``) and every other is one ``$filter`` equality per pinned company
        through the live BC reader (GET only, its live audience deciding too). A value of
        a prefix-setting database, or of a scoped kind such as a customer PO, unique only
        within a customer, is never asked; a find the projection store answers asked
        nothing live, and its records were the first half's to propose. Each record found
        live (a hit, a candidate of a direct carrier's start binding) becomes a card of
        the value plx.find matched, cited at its live record as untrusted source text,
        and is decided by the same ``_join`` (literal match, scope, ``cross_scope`` across
        BC companies); the first that joins is the pick, its card cited beside the root
        as a lead (``self._live_cards``). A value whose find leaves a company unsearched
        (read cut short or failed, or outside the books) or shows records of two companies
        is ``cross_scope``. Counts only in ``directions.reverse.bc``: linked records read
        and refused, BC GETs sent (``requests``), company units asked (``lookups``) and
        every probe per state, hits and hits decided through ``_join``; ``budget_spent``
        when the pick's deadline cut it short (the value it cut decides nothing). Returns
        ``picked``'s arguments, or None."""
        from ontology import identifiers
        from ontology.resolve import only_sources

        from .ontology_tools import default_grammar
        from .ontology_tools_identity import _handle_company, scoped_kind
        from .ontology_tools_results import _column_path

        reverse = self._pick_stats["directions"]["reverse"]
        bc = reverse["bc"]
        reader = getattr(getattr(self.runtime, "live", None), "bc", None)
        grammar = default_grammar()
        direct = [c for c in carriers if c.get("then") is None and c["find_type"] == linked
                  and _system(c["from_binding"]) == "bc"
                  and not scoped_kind(grammar, c["identifier_kind"])]
        if reader is None or not direct:
            return None
        bc["reader"] = True
        ends = {c["to_binding"] for c in direct}
        starts = {c["from_binding"] for c in direct}
        db = sqlite3.connect(self.projection().resolve().as_uri() + "?mode=ro", uri=True)
        try:
            ordered = [oid for oid in _newest(db, linked) if oid.split(":", 2)[1] in ends]
        finally:
            db.close()
        asked: set[tuple[str, str]] = set()
        hits: set[str] = set()
        transport = getattr(self.runtime.live, "bc_transport", None)
        refused = self._refusals(budget)
        # Unsearched units of a find (``ontology_tools._resolution_gaps``): the value may
        # be another company's too. A withheld unit is never named, so it tells nothing.
        unsearched = {"source_truncated", "source_unreadable", "source_unpinned"}

        def spent() -> bool:
            # The deadline passed, or refused a request (a probe it cut decides nothing).
            if budget.passed or self._refusals(budget) > refused:
                reverse["budget_spent"] = True
            return reverse["budget_spent"]

        def sent() -> int:
            return int(getattr(transport, "gets_sent", 0) or 0)

        for linked_id in ordered:
            if bc["orders"] >= PICK_SCAN or spent():
                break
            bc["orders"] += 1
            card = call("plx.object", {"object_id": linked_id})
            if "error" in card:
                bc["orders_unreadable"] += 1
                continue
            binding = linked_id.split(":", 2)[1]
            # Each value the record holds at a carrier's to_field, its kind and the start
            # type's property carrying it: one find each.
            wanted: dict[tuple[str, str, str], None] = {}
            for item in card.get("values") or []:
                cite = item.get("citation") if isinstance(item, Mapping) else None
                text = _plain(item.get("value")) if isinstance(item, Mapping) else None
                if not isinstance(cite, Mapping) or not isinstance(text, (str, int)) \
                        or isinstance(text, bool) or not str(text).strip() \
                        or identifiers.environment_prefix(str(text).strip()):
                    continue
                column = _column_path(str(cite.get("field_path") or ""))
                for carrier in direct:
                    if carrier["to_binding"] == binding and carrier["to_field"] == column:
                        wanted[(str(text).strip()[:200], carrier["identifier_kind"],
                                carrier["property"])] = None
            for text, kind, prop in wanted:
                # BC compares Code fields case-insensitively: one ask per folded value.
                if (text.casefold(), kind, prop) in asked or spent():
                    continue
                asked.add((text.casefold(), kind, prop))
                before = sent()
                with only_sources({"bc"}) as scope:
                    found = call("plx.find", {
                        "query": text, "object_type": object_type, "identifier_kind": kind,
                        "filters": [{"property": prop, "op": "eq", "value": text}]})
                bc["requests"] += sent() - before
                if spent():
                    break
                for state, count in scope.counts.items():
                    # A lookup is a company unit asked of BC: never one withheld before
                    # any request, unpinned or not searchable.
                    if state in ("read", "truncated", "unreadable"):
                        bc["lookups"] += count
                    if count:
                        bc["lookup_states"][state] = bc["lookup_states"].get(state, 0) + count
                if not scope.resolved or "error" in found:
                    continue  # answered by the projection store, or not answered
                live = []
                for candidate in found.get("candidates") or []:
                    obj = candidate.get("object") or {}
                    matched = candidate.get("matched") if isinstance(
                        candidate.get("matched"), Mapping) else {}
                    raw = matched.get("raw") if isinstance(matched.get("raw"), Mapping) else {}
                    origin = raw.get("origin") if isinstance(raw.get("origin"), Mapping) else {}
                    handle = origin.get("citation")
                    if str(obj.get("object_id") or "").split(":", 2)[1:2] != [
                            matched.get("binding")] or matched.get("binding") not in starts \
                            or not isinstance(handle, str) or not handle.startswith("bc://"):
                        continue
                    live.append((handle, matched["binding"], {
                        "object": {"object_id": obj["object_id"], "object_type": object_type},
                        "values": [{"property": matched.get("property"),
                                    "citation": {"l1_handle": handle,
                                                 "field_path": origin.get("field_path")},
                                    "value": raw}]}))
                # BC numbers are unique only within a company: a value records of two
                # companies hold, or a company not searched in full (a read cut short or
                # failed, or a company outside the books, never asked), names no one
                # record. A company withheld from this reader tells it nothing.
                doubt = len({_handle_company(h) for h, _b, _c in live}) > 1 \
                    or (found.get("completeness") or {}).get("complete") is False \
                    or any(isinstance(g, Mapping) and g.get("reason") in unsearched
                           for g in found.get("gaps") or [])
                for handle, start_binding, start in live:
                    if handle in hits:
                        continue
                    hits.add(handle)
                    bc["hits"] += 1
                    mine = [c for c in direct if c["from_binding"] == start_binding]
                    bc["scanned"] += 1
                    if doubt and any(c["bc_company"] for c in mine):
                        reason, root, through = "cross_scope", None, []
                    else:
                        reason, root, _o, through = self._join(call, linked, start, mine, memo)
                    if root is not None:
                        reverse["joined"] = True
                        start_id = start["object"]["object_id"]
                        self._live_cards[start_id] = start
                        return root, start_id, through
                    reverse["reasons"][reason] = reverse["reasons"].get(reason, 0) + 1
        return None

    def _join(self, call: Callable[[str, dict], dict], linked: str, card: Mapping[str, Any],
              carriers: Sequence[Mapping[str, Any]], memo: dict[tuple, Any]
              ) -> tuple[str, Mapping[str, Any] | None, list[tuple[str, str]], list[str]]:
        """(reason, root or None, carrier outcomes, the record a two-hop carrier matched on
        the way to the root) of one candidate: ``plx.find`` for each carrier's
        ``find_type`` once per distinct value the candidate's ``carriers`` carry (the direct
        carrier first). A root is a candidate of a carrier's target binding, matched at its
        declared field, whose match may root (``ontology_tools.ROOT_REASONS``) and whose
        matched text is the value by the owning system's literal comparison
        (``roots_exactly``); through a carrier's ``then`` hop, the one record it reaches
        (``_follow``). A match on a kind unique only
        within a scope (a customer PO) belongs to the carriers of that kind: it roots only
        through the root gate and when its record's scope party is the same record as the
        candidate's (``_scoped``), else ``scope_mismatch`` / ``scope_unread``. A
        ``bc_company`` carrier's match roots nothing while the find shows a BC record of
        another company (or of none the candidate's citations name) holding the number,
        or does not show every match: the match is not shown to be the candidate's
        company's, so it is ``cross_scope``, never corroborated. One distinct
        root over every carrier joins; several are ``ambiguous_root`` (never a winner). A
        target relation the mirror audience does not grant the investigator is never
        searched. The reason follows ``JOIN_REASONS``; each carrier (``carrier_key``) gets
        ``joined`` or its own reason by the same precedence."""
        from ontology import identifiers

        from .ontology_tools import default_grammar
        from .ontology_tools_identity import _handle_company, roots_exactly, scoped_kind
        from .ontology_tools_policy import ROOT_REASONS
        from .ontology_tools_results import _column_path

        values: dict[str, Any] = {}
        companies: set[str | None] = set()
        for item in card.get("values") or []:
            if isinstance(item, Mapping) and item.get("property") not in values:
                values[item.get("property")] = _plain(item.get("value"))
            cite = item.get("citation") if isinstance(item, Mapping) else None
            if isinstance(cite, Mapping) and isinstance(cite.get("l1_handle"), str):
                companies.add(_handle_company(cite["l1_handle"]))
        # The BC company the candidate's numbers belong to: one its citations name, or none.
        company = next(iter(companies)) if len(companies) == 1 else None
        searched: list[tuple[Mapping[str, Any], str]] = []
        for carrier in carriers:
            text = values.get(carrier["property"])
            if isinstance(text, (str, int)) and not isinstance(text, bool) \
                    and str(text).strip():
                searched.append((carrier, str(text).strip()))
        unsearched = [(carrier_key(c), "no_identifier") for c in carriers
                      if all(c is not s for s, _t in searched)]
        if not searched:
            return "no_identifier", None, unsearched, []
        # A carried reference that names a row of a prefix-setting Portal database, which
        # the served replica never holds: not searched, never "no row".
        replica = [(carrier_key(c), "not_in_served_replica") for c, t in searched
                   if identifiers.environment_prefix(t)]
        searched = [(c, t) for c, t in searched if not identifiers.environment_prefix(t)]
        if not searched:
            return "not_in_served_replica", None, replica + unsearched, []
        grammar = default_grammar()
        actor = self.runtime.employee_gate(self.actor)
        target_relations = {(_binding(c["to_binding"]) or {}).get("relation")
                            for c, _t in searched}
        outcomes: list[set[str]] = [set() for _s in searched]
        roots: list[dict[str, Mapping[str, Any]]] = [{} for _s in searched]
        via: dict[str, str] = {}  # a root reached through a matched record -> that record
        for value, find_type in dict.fromkeys((t, c["find_type"]) for c, t in searched):
            group = []
            for index, (carrier, text) in enumerate(searched):
                if text != value or carrier["find_type"] != find_type:
                    continue
                specs = [_binding(b) or {} for b in (carrier["to_binding"],
                                                     (carrier["then"] or {}).get("binding"))
                         if b is not None]
                if any(spec.get("source") == "portal_api"
                       and not self.audience.allows(actor, spec.get("relation"))
                       for spec in specs):
                    outcomes[index].add("{src}_row_unreadable_for_reader")
                else:
                    group.append((index, carrier))
            if not group:
                continue
            found = self._join_find(call, value, find_type, [c for _i, c in group], memo)
            if "error" in found:
                for index, _c in group:
                    outcomes[index].add("find_live_error")
                continue
            scoped = [(i, c) for i, c in group if scoped_kind(grammar, c["identifier_kind"])]
            plain = [(i, c) for i, c in group if (i, c) not in scoped]
            # A BC record of another company holds the value, or the page may leave one out.
            elsewhere = (found.get("completeness") or {}).get("complete") is False
            for candidate in found.get("candidates") or []:
                obj = candidate.get("object") or {}
                parts = str(obj.get("object_id") or "").split(":", 2)
                matched = candidate.get("matched") if isinstance(
                    candidate.get("matched"), Mapping) else {}
                if candidate.get("reason") in ROOT_REASONS and len(parts) == 3 \
                        and (_binding(parts[1]) or {}).get("source") == "bc":
                    origin = (matched.get("raw") or {}).get("origin") if isinstance(
                        matched.get("raw"), Mapping) else None
                    cited = origin.get("citation") if isinstance(origin, Mapping) else None
                    elsewhere = elsewhere or company is None or not isinstance(cited, str) \
                        or _handle_company(cited) != company
                owners = [(i, c) for i, c in scoped
                          if matched.get("identifier_kind") == c["identifier_kind"]
                          and parts[1:2] == [c["to_binding"]]]
                for index, carrier in owners:
                    outcome = self._scoped(call, card, carrier, linked, candidate, value,
                                           grammar, memo)
                    if outcome == "joined":
                        roots[index][obj["object_id"]] = obj
                    elif outcome is not None:
                        outcomes[index].add(outcome)
                if owners:
                    continue
                if not plain:
                    for index, _c in group:
                        outcomes[index].add("identity_lead_only")
                    continue
                field = ((matched.get("raw") or {}).get("origin") or {}).get("field_path") \
                    if isinstance(matched.get("raw"), Mapping) else None
                # Only the declared field of a carrier's target binding may root: a match
                # on any other field is a path the schema does not declare.
                owner = next((i for i, c in plain if parts[1:2] == [c["to_binding"]]
                              and isinstance(field, str) and _column_path(field) == c["to_field"]),
                             None)
                if owner is None or candidate.get("reason") not in ROOT_REASONS \
                        or len(parts) < 3:
                    outcomes[plain[0][0] if owner is None else owner].add("identity_lead_only")
                elif not roots_exactly(candidate, value, grammar):
                    outcomes[owner].add("normalized_only")
                elif searched[owner][0]["then"] is None:
                    roots[owner][obj["object_id"]] = obj
                else:
                    reached, why = self._follow(call, searched[owner][0]["then"],
                                                obj["object_id"], memo)
                    if reached is None:
                        outcomes[owner].add(why)
                    else:
                        roots[owner][reached["object_id"]] = reached
                        via[reached["object_id"]] = obj["object_id"]
            for index, carrier in plain if elsewhere else ():
                if carrier["bc_company"] and roots[index]:
                    roots[index] = {}
                    outcomes[index].add("cross_scope")
            # Only what the find says about the target's own carriers decides: a gap of
            # the asking system's carriers (or a generic no-visible-match) is not a
            # reason the other system's row is missing.
            gaps = [g for g in found.get("gaps") or [] if isinstance(g, Mapping)]
            if any(g.get("reason") in _WITHHELD_FIND_GAPS for g in gaps):
                for index, _c in group:
                    outcomes[index].add("{src}_row_unreadable_for_reader")
            elif any(g.get("reason") in _UNREAD_FIND_GAPS and any(
                    rel and rel in str(g.get("subject")) for rel in target_relations)
                    for g in gaps):
                for index, _c in group:
                    outcomes[index].add("{src}_carrier_unread")
        for index, (carrier, _t) in enumerate(searched):
            if outcomes[index] & {"ambiguous_root", "scope_unread"} \
                    and scoped_kind(grammar, carrier["identifier_kind"]):
                roots[index] = {}  # a scoped match beside an unsettled one roots nothing
        joined = {oid: obj for found_roots in roots for oid, obj in found_roots.items()}
        if self.upstream:
            # The Portal could not be read at capture: its silence shows nothing.
            for index, (carrier, _t) in enumerate(searched):
                if _system(carrier["to_binding"]) == "portal":
                    outcomes[index].add("{src}_carrier_unread")

        def reason_of(seen: set[str], carrier: Mapping[str, Any]) -> str:
            reason = next((r for r in JOIN_REASONS if r in seen), "no_{src}_row_for_{property}")
            return reason.format(src=_system(carrier["to_binding"]), property=carrier["property"])

        per = [(carrier_key(c), "joined" if len(joined) == 1 and roots[i] else
                "ambiguous_root" if roots[i] else reason_of(outcomes[i], c))
               for i, (c, _t) in enumerate(searched)] + replica + unsearched
        if len(joined) == 1:
            [(root_id, root)] = joined.items()
            return "joined", root, per, [via[root_id]] if root_id in via else []
        everything = set().union(*outcomes) | ({"ambiguous_root"} if joined else set()) \
            | ({"not_in_served_replica"} if replica else set())
        return reason_of(everything, searched[0][0]), None, per, []

    def _join_find(self, call: Callable[[str, dict], dict], value: str, find_type: str,
                   group: Sequence[Mapping[str, Any]], memo: dict[tuple, Any]
                   ) -> dict[str, Any]:
        """``plx.find`` for one carried value as the join needs it, once per value, type
        and family set for the whole pick (a value many candidates carry, such as a shared
        customer PO, is resolved, and probed live, once). A match roots only at a carrier's
        target binding, so a find the projection store cannot answer asks live only the
        source families of the ``group``'s targets (``ontology.resolve.only_sources``: the
        Portal for a Portal order). Live 2026-10-17: an unrestricted fallback also probed
        every pinned BC company for the very invoice the pick started from, about 38 s a
        root, and nothing it found could root. Only when that find shows a record of a
        target binding a ``bc_company`` carrier could root on is the find repeated over
        every family, so a BC record of another company holding the number still makes it
        ``cross_scope``, never a silent join."""
        from ontology.resolve import only_sources

        arguments = {"query": value[:200], "object_type": find_type}
        targets = {b for c in group for b in (c["to_binding"], (c["then"] or {}).get("binding"))
                   if b is not None}
        families = frozenset(str((_binding(b) or {}).get("source")) for b in targets)
        key = ("find", value, find_type, families)
        if key not in memo:
            with only_sources(families) as asked:
                memo[key] = (call("plx.find", arguments), asked.resolved > 0)
        found, live = memo[key]
        # Answered by the projection store (nothing asked live): it already shows every
        # family's records holding the value.
        if not live or "error" in found or not any(c["bc_company"] for c in group) or not any(
                str((cand.get("object") or {}).get("object_id") or "").split(":", 2)[1:2]
                == [c["to_binding"]] for c in group for cand in found.get("candidates") or []):
            return found
        if ("find", value, find_type) not in memo:
            memo[("find", value, find_type)] = call("plx.find", arguments)
        return memo[("find", value, find_type)]

    def _follow(self, call: Callable[[str, dict], dict], then: Mapping[str, Any],
                object_id: str, memo: dict[tuple, Any]
                ) -> tuple[Mapping[str, Any] | None, str]:
        """The last hop of a two-hop carrier from the record it matched: the one object of
        ``then["binding"]`` that ``plx.around`` reaches along ``then["link_type"]`` (its
        ``plx.object`` card's object), else why not: ``path_unreached`` (none, several, a
        cut answer or another binding) or ``{src}_row_unreadable_for_reader``."""
        reached = self._party(call, object_id, then["link_type"], memo, then["direction"])
        if reached is None or reached.split(":", 2)[1:2] != [then["binding"]]:
            return None, "path_unreached"
        card = call("plx.object", {"object_id": reached})
        if "error" in card:
            return None, "{src}_row_unreadable_for_reader"
        return card["object"], ""

    def _scoped(self, call: Callable[[str, dict], dict], card: Mapping[str, Any],
                carrier: Mapping[str, Any], linked: str, candidate: Mapping[str, Any],
                value: str, grammar: Any, memo: dict[tuple, Any]) -> str | None:
        """A scoped carrier's match: ``joined`` when the root gate passes it
        (``scoped_root_lead``: its literal text, unique within its record's one scope) and
        the scope party its record links to (``scope_links``, the Portal's customer + PO
        match) is the same record as the candidate's; else ``scope_mismatch`` (both read,
        not one record), ``scope_unread`` (either not shown as one record) or the refused
        match's reason (``_SCOPE_WHY``; None: the owning system never matches it)."""
        from .ontology_tools_identity import scoped_root_lead

        why = scoped_root_lead(candidate, value, grammar)
        if why is not None:
            return _SCOPE_WHY[why] if why in _SCOPE_WHY else "identity_lead_only"
        here_id = str(card["object"]["object_id"])
        links = scope_links(here_id.split(":", 1)[0], carrier, linked)
        if links is None:
            return "scope_unread"
        here = self._party(call, here_id, links[0], memo)
        there = self._party(call, str(candidate["object"]["object_id"]), links[1], memo)
        if here is None or there is None:
            return "scope_unread"
        same = self._same_record(call, here, there, memo)
        return "joined" if same else "scope_unread" if same is None else "scope_mismatch"

    @staticmethod
    def _party(call: Callable[[str, dict], dict], object_id: str, link: str,
               memo: dict[tuple, Any], direction: str = "forward") -> str | None:
        """The one object ``plx.around`` reaches from ``object_id`` along the single-valued
        ``link`` in ``direction`` (asked again at lead tier by the plans' own rule when the
        tool says the link lies below its floor, as a Portal-native row's own foreign key
        does), or None: none, several or a cut answer shows no one party."""
        from .ontology_tools_investigate_plans import LEAD_RETRY, ask_again

        key = ("party", object_id, link, direction)
        if key not in memo:
            _first, found = ask_again(
                lambda args: call("plx.around", args),
                {"from": object_id, "path": [{"link_type": link, "direction": direction}],
                 "limit": 2}, LEAD_RETRY, lambda answer: not answer.get("objects"))
            ids = {str(o.get("object_id")) for o in found.get("objects") or []
                   if isinstance(o, Mapping)} if "error" not in found else set()
            cut = bool((found.get("completeness") or {}).get("truncated"))
            memo[key] = next(iter(ids)) if len(ids) == 1 and not cut else None
        return memo[key]

    @staticmethod
    def _same_record(call: Callable[[str, dict], dict], first: str, second: str,
                     memo: dict[tuple, Any]) -> bool | None:
        """Whether two parties are one record, decided by ``ontology_tools.same_party``
        (the rule plx.find's ``related_same_record`` reads) over the identity links both
        ``plx.object`` cards show: True only at an identity tier; None (scope unread) when
        only a weaker link joins them, a card is unreadable or its identity list was cut
        (the link may be the one left out)."""
        from .ontology_tools import same_party

        if first == second:
            return True
        key = ("same", *sorted((first, second)))
        if key not in memo:
            edges: list[tuple[Any, Any, Any]] = []
            cut = False
            for object_id in (first, second):
                card = call("plx.object", {"object_id": object_id})
                if "error" in card:
                    memo[key] = None
                    return None
                # plx.object says a cut identity list only as a gap (past MAX_IDENTITY).
                cut = cut or any(isinstance(gap, Mapping)
                                 and gap.get("reason") == "identity_truncated"
                                 for gap in card.get("gaps") or ())
                edges += [(edge.get("from"), edge.get("to"), edge.get("tier"))
                          for edge in card.get("identity") or [] if isinstance(edge, Mapping)]
            memo[key] = same_party(first, second, edges, cut=cut)
        return memo[key]

    def _investigate(self, call: Callable[[str, dict], dict], kind: str,
                     identifier: str) -> str | None:
        from operator_api.workspace.object_workspace import plans_for

        from .ontology_tools_policy import _IDENTITY_TIERS

        extra: list[str] = []
        if kind == "pick":
            root, asked, extra = self._pick(call, identifier)
        else:
            root, asked = self._find(call, identifier, KIND_TYPES[kind]), identifier.strip()
        root_id, root_type = root["object_id"], root["object_type"]
        card = call("plx.object", {"object_id": root_id, "response_format": "detailed"})
        if "error" in card:
            raise OpenError(f"object_error:{_code(card)}")
        # The same record as the other systems hold it (verified or reviewed identity
        # links): each card's own cited values, never merged into the root's.
        twins = [edge["to"] if edge["from"] == root_id else edge["from"]
                 for edge in card.get("identity") or []
                 if edge.get("tier") in _IDENTITY_TIERS][:MAX_TWINS]
        cards = [(root_type, card)]
        self.root = _root_record(root_id, root_type)
        # The record a join pick started from is cited beside the root it joined.
        for twin in dict.fromkeys([*twins, *extra]):
            # A record a join pick read live (``_bc_reverse``) is cited from its live card.
            other = self._live_cards.get(twin) if kind == "pick" else None
            other = other or call("plx.object", {"object_id": twin,
                                                 "response_format": "detailed"})
            if "error" not in other:
                cards.append((other["object"]["object_type"], other))
        plans = plans_for(root_type)
        # Every question family from the root and from each verified twin of the root's
        # own type, by each system's own links (an FM assembly order reaches its order line
        # through the FM allocation row, its Portal twin through the MO's line).
        starts = [root_id] + [each["object"]["object_id"] for object_type, each in cards[1:]
                              if object_type == root_type]
        answers = [(plan, call("plx.investigate", {"family": plan["family"], "root": start}))
                   for start in starts for plan in plans]
        titles = [str(plan["title"]) for plan in plans] or [f"{root_type} record"]
        question = f"{'; '.join(titles)}: {root_type} {asked}"[:4000]
        case_id = call("plx.case_open", {"question": question, "roots": [asked[:200]]})["case_id"]
        record = _Recorder(call, case_id, upstream=self.upstream)
        for object_type, each in cards:
            # A join pick's start record is joined by a text match: a lead.
            lead = each["object"]["object_id"] in extra
            # Its missing-in-record leads first: a case full of values still shows them.
            for found in each.get("leads") or []:
                record.lead(found)
            for value in each.get("values") or []:
                record.value(value, object_type, lead=lead)
            for conflict in each.get("conflicts") or []:
                record.conflict(conflict)
            record.tool_gaps(each.get("gaps"), "plx.object", each.get("leads"))
        for plan, answer in answers:
            record.answer(plan, answer)
        # The records the question families reached around the root (their chains), each
        # card's own cited values and conflicts, so the case cites the transaction's rows.
        known = {root_id, *(each["object"]["object_id"] for _type, each in cards)}
        reached = [ref for _plan, answer in answers
                   for ref in (answer.get("chain") or {}).get("objects") or []
                   if isinstance(ref, Mapping) and ref.get("object_id") not in known]
        # A chain record is established when a path of links above lead tier joins it
        # to the root or a same-record twin; one reached only through leads is a lead.
        established = {root_id, *(each["object"]["object_id"] for _type, each in cards
                                  if each["object"]["object_id"] not in extra)}
        edges = [(edge.get("from"), edge.get("to")) for _plan, answer in answers
                 for edge in (answer.get("chain") or {}).get("edges") or []
                 if isinstance(edge, Mapping) and edge.get("tier") not in (None, "lead")]
        grown = True
        while grown:
            grown = False
            for first, second in edges:
                if (first in established) != (second in established):
                    established |= {first, second}
                    grown = True
        # And one plx.around hop along every link the root's cards count (default tier
        # floor: leads are not followed into the case).
        for _type, each in cards:
            for entry in each.get("linked_counts") or []:
                if not entry.get("count"):
                    continue
                around = call("plx.around", {
                    "from": each["object"]["object_id"], "limit": 10,
                    "path": [{"link_type": entry["link_type"],
                              "direction": entry["direction"]}]})
                hop = [ref for ref in around.get("objects") or []
                       if isinstance(ref, Mapping) and ref.get("object_id") not in known]
                if (around.get("completeness") or {}).get("truncated"):
                    # More linked records than one hop reads: named, never silently cut.
                    record.gap("unknown", "around_truncated",
                               f"{entry['link_type']} ({entry['direction']}): linked records "
                               "beyond the first page this case reads",
                               detail="read the rest with plx.around and its next_cursor")
                # The hop's default tier floor admits no lead; a record one hop from a
                # join pick's start record (itself a lead) is only as joined as it is.
                if each["object"]["object_id"] not in extra:
                    established |= {ref.get("object_id") for ref in hop}
                reached += hop
        reached = list({ref["object_id"]: ref for ref in reached}.values())
        if len(reached) > MAX_REACHED:
            record.gap("unknown", "record_cap",
                       "Records the question families reached beyond those one case cites",
                       detail="the case cites the first of them; read the rest with plx.around "
                              "or plx.investigate", own=True)
        for ref in reached[:MAX_REACHED]:
            other = call("plx.object", {"object_id": ref["object_id"]})
            if "error" in other:
                continue
            for found in other.get("leads") or []:
                record.lead(found)
            for value in other.get("values") or []:
                record.value(value, ref.get("object_type"),
                             lead=ref["object_id"] not in established)
            for conflict in other.get("conflicts") or []:
                record.conflict(conflict)
        if self.originals is not None:
            from .sharepoint_originals import attach_original
            attached = attach_original(call, case_id, asked, drive_id=self.originals["drive_id"],
                                       folder=self.originals["folder"],
                                       name_pattern=self.originals["name_pattern"],
                                       expected=self.originals.get("expected"))
            if attached["status"] != "attached":
                record.gap("unknown", f"original_{attached['status']}",
                           "The original document in the configured originals folder", own=True)
        stats = self._pick_stats
        if stats and sum((stats.get("root_reasons") or {}).values()):
            # Newer candidates that joined nothing: their reasons are the typed gap.
            # The counts are the run's (``pick``); the case names the reasons.
            counts = ", ".join(sorted(stats["root_reasons"]))
            record.gap("inaccessible" if self.upstream else "unknown", "pick_unjoined",
                       f"Newer {identifier.partition('>')[0]} records that joined no "
                       f"{identifier.partition('>')[2]} record of the other system",
                       detail=f"reasons: {counts}", own=True)
        if stats and stats.get("lead_tier"):
            # The pick chose this root only through a lead-tier link (a machine-read
            # reference): the case says so, as the run's ``pick`` does, citing the objects
            # the link reached, so a reader they are withheld from sees neither the gap nor
            # their type; uncitable, it is not written (never the uncited form).
            handles = record.object_handles(self._pick_lead) if self._pick_lead else None
            citations = record.scoped(handles) if handles else None
            if citations:
                record.gap("unknown", "pick_rests_on_lead",
                           f"The root was picked because a lead-tier link reaches a "
                           f"{identifier.partition('+')[2]} record",
                           detail="the link is a machine-read reference, not evidence of the "
                                  "connection; the plan's answers label what rests on it",
                           citations=citations, own=True)
            else:
                record.skipped["gaps"] += 1
        if self._unlinked:
            # Rooted without the linked record the pick asked for: an uncited gap every
            # reader of the case sees, so it names neither the linked type nor the source
            # (the run's ``missing`` names the source for the operator).
            record.gap("not_ingested", "pick_linked_not_captured",
                       "The linked record the pick asked for, reached from this root",
                       detail=f"{self._unlinked.partition(':')[0]}: a source of the linked "
                              "type was not captured in full or not captured at all, so the "
                              "root was chosen without one; a record the capture missed is "
                              "no evidence of none", own=True)
        record.finish()
        return case_id


def _last_ingestion_run(state_dir: Path) -> int | None:
    """The latest finished ingestion run id (read-only), or None."""
    import sqlite3

    from .ingestion import STORE_DIR, STORE_FILE

    path = Path(state_dir) / STORE_DIR / STORE_FILE
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT MAX(run_id) FROM run WHERE state = 'finished'").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def structure(view: Mapping[str, Any] | None) -> dict[str, Any]:
    """Counts, booleans and enums of one rendered workspace (no identifier or value)."""
    if view is None:
        return {"status": "not_readable"}
    findings = view.get("findings") or {}
    withheld = {"findings": _count(findings.get("withheld"))}
    for name in ("calculations", "gaps", "proposals", "verifications"):
        withheld[name] = sum(1 for item in view.get(name) or []
                             if isinstance(item, Mapping) and item.get("content") == WITHHELD)
    withheld["total"] = sum(withheld.values())
    gaps_by_state: dict[str, int] = {}
    for gap in view.get("gaps") or []:
        if gap.get("content") != WITHHELD:
            state = str(gap.get("state") or "unknown")
            gaps_by_state[state] = gaps_by_state.get(state, 0) + 1
    leads: dict[str, int] = {}   # the recorded leads this reader reads (never superseded)
    for item in findings.get("unknown") or []:
        kind = _LEAD_KIND.match(str(item.get("statement") or "")) \
            if isinstance(item, Mapping) else None
        if kind:
            leads[kind[1]] = leads.get(kind[1], 0) + 1
    conflicts: dict[str, int] = {}
    for item in view.get("conflicts") or []:
        kind = str(item.get("kind") or "conflict")
        conflicts[kind] = conflicts.get(kind, 0) + 1
    approvals: dict[str, int] = {}
    for proposal in view.get("proposals") or []:
        state = str((proposal.get("approval") or {}).get("state") or "unknown")
        approvals[state] = approvals.get(state, 0) + 1
    originals = view.get("originals") or []
    return {
        "status": "read", "case_status": view.get("status"),
        "chain_ok": bool((view.get("integrity") or {}).get("chain_ok")),
        "evidence_label": (view.get("evidence") or {}).get("label"),
        "evidence_fixture": bool((view.get("evidence") or {}).get("fixture")),
        "read_only": view.get("read_only") is True,
        "business_system_writes": view.get("business_system_writes"),
        "timeline": _count(view.get("timeline")),
        "source_dates": _count(view.get("source_dates")),
        "records": _count(view.get("records")),
        "findings": {bucket: _count(items) for bucket, items in sorted(findings.items())},
        "calculations": _count(view.get("calculations")),
        "gaps": _count(view.get("gaps")), "gaps_by_state": dict(sorted(gaps_by_state.items())),
        "proposals": _count(view.get("proposals")),
        "approval_states": dict(sorted(approvals.items())),
        "withheld": withheld,
        "originals": len(originals),
        "originals_sharepoint": sum(1 for o in originals
                                    if (o.get("source") or {}).get("kind") == "sharepoint-drive"),
        "originals_etag_current": sum(1 for o in originals
                                      if (o.get("source") or {}).get("etag_status") == "current"),
        "original_pages": sum(int(o.get("pages_total") or 0) for o in originals),
        "conflicts": sum(conflicts.values()), "conflicts_by_kind": dict(sorted(conflicts.items())),
        "leads_by_kind": dict(sorted(leads.items())),
        "evaluation": (view.get("evaluation") or {}).get("status"),
        "source_coverage": (view.get("source_coverage") or {}).get("status"),
        "truncated": sorted(view.get("truncated") or {}),
        # Every panel in this one rendered case: a timeline, an original, the gaps and
        # proposals sections (either may be empty) and a known evaluation status.
        "converged": bool(_count(view.get("timeline")) and originals
                          and isinstance(view.get("gaps"), list)
                          and isinstance(view.get("proposals"), list)
                          and (view.get("evaluation") or {}).get("status") is not None),
    }


class _ReaderTools:
    """One in-process tool session per reader over the run's projection, for
    ``relationships`` (opened on first use, closed with the run)."""

    def __init__(self, runtime: Any, audience: Any, opener: "CaseOpener") -> None:
        self.runtime, self.audience, self.opener = runtime, audience, opener
        self.sessions: dict[Any, _Session] = {}

    def __call__(self, reader: Any, view: Mapping[str, Any] | None) -> dict[str, Any]:
        if view is None:
            return {}
        try:
            projection = self.opener.projection()
            if reader not in self.sessions:
                self.sessions[reader] = _Session(self.runtime, reader, self.audience,
                                                 projection, captures=self.opener.state_dir,
                                                 bc_books=self.opener.bc_books)
        except Exception:  # noqa: BLE001 - named in the counts, never a silent zero
            return {"relationship_nodes": 0, "relationship_edges": {},
                    "relationship_truncated": ["projection_unavailable"]}
        return relationships(self.sessions[reader], projection, view)

    def close(self) -> None:
        for session in self.sessions.values():
            session.close()


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         graph_sender: Any = None, originals_options: Mapping[str, Any] | None = None,
         host_options: Mapping[str, Any] | None = None) -> int:
    """The CLI; what the run would write to stderr is counted into the summary's
    ``stderr_causes`` (``_StderrCauses``)."""
    with _StderrCauses({}) as counted:
        return _main(argv, environ=environ, graph_sender=graph_sender,
                     originals_options=originals_options, host_options=host_options,
                     causes=counted)


def _main(argv: Sequence[str] | None, *, environ: Mapping[str, str] | None,
          graph_sender: Any, originals_options: Mapping[str, Any] | None,
          host_options: Mapping[str, Any] | None, causes: _StderrCauses) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.case_workspace_live",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path,
                        help="plx-source-mirror-audience/v1 file (default deny)")
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True,
                        help="investigator principal (repeat); opens the cases")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--open", action="append", default=[], metavar="KIND=IDENTIFIER",
                        help="ao=, sales_order=, customer_po=, petra_po=<identifier>, "
                             "find=<any identifier>, pick=TYPE[+LINKED_TYPE] or "
                             "pick=TYPE>LINKED_TYPE (a join across two systems)")
    parser.add_argument("--case-id", action="append", default=[],
                        help="render an existing case of this state directory")
    parser.add_argument("--reinvestigate", action="store_true")
    parser.add_argument("--reader", action="append", default=[],
                        help="comma-separated principals of one reader (repeat); the "
                             "investigator is always rendered too")
    parser.add_argument("--live-source-audience", type=Path, default=None,
                        help="plx-live-source-audience/v1 file: the GET-only Graph/BC readers "
                             "(originals, citation rechecks, PO mail); credentials from the "
                             "environment; live-unverified")
    parser.add_argument("--sharepoint-original", action="store_true",
                        help="attach each case's original from the --originals-expected "
                             "folder (needs --live-source-audience)")
    parser.add_argument("--originals-expected", type=Path, default=None,
                        help="plx-sharepoint-originals-expected/v1 file naming the originals "
                             "folder: the folder, file-name pattern and drive the operator "
                             "configures for this host")
    parser.add_argument("--sharepoint-drive", action="append", default=[])
    parser.add_argument("--internal-domain", action="append", default=[])
    parser.add_argument("--ingest-sources", type=Path, default=None,
                        help="first run operating.ingestion over this plx-ingest-sources/v1 "
                             "file into --state-dir (GET only, credentials from the "
                             "environment)")
    parser.add_argument("--ingest-live-audience", type=Path, default=None)
    parser.add_argument("--host-config", type=Path, default=None,
                        help="first run one capture cycle of operating.host over this "
                             "plx-host-config/v1 file into --state-dir (Portal and BC, GET "
                             "only, credentials from the environment)")
    parser.add_argument("--ingest-runs", type=int, default=1)
    parser.add_argument("--enable-local-pdf-ocr", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="write each reader's full workspace JSON here (row data)")
    parser.add_argument("--min-rendered", type=int, default=None,
                        help="exit 0 when at least this many requests render (default: all; "
                             "an unlinked pick's case is rendered but never counts)")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        requests = [parse_open(text) for text in args.open]
        readers = [_reader(text, args.tenant, args.clearance) for text in args.reader]
    except OpenError as error:
        parser.error(str(error))
    if not requests and not args.case_id:
        parser.error("give at least one --open or --case-id")
    if len(requests) + len(args.case_id) > MAX_REQUESTS:
        parser.error(f"at most {MAX_REQUESTS} cases per run")
    if not 1 <= args.ingest_runs <= 3:
        parser.error("--ingest-runs must be 1..3")
    if args.host_config is not None and args.ingest_sources is not None:
        parser.error("--host-config and --ingest-sources are two ways to fill the state "
                     "directory: give one")
    if (args.sharepoint_original or args.sharepoint_drive) \
            and args.live_source_audience is None:
        parser.error("--sharepoint-original and --sharepoint-drive need "
                     "--live-source-audience")
    if args.sharepoint_original != (args.originals_expected is not None):
        parser.error("--sharepoint-original and --originals-expected go together")
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
    except (OSError, ValueError) as error:
        parser.error(f"mirror audience is invalid: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    seconds: dict[str, float] = {}
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": MIRROR_EVIDENCE["label"],
                               "mirror": {"workbench_client": False}, "ingestion": None,
                               "seconds": seconds, "stderr_causes": causes.causes}
    if args.ingest_sources is not None:
        started = time.monotonic()
        code, summary["ingestion"] = _ingest(args, env)
        seconds["ingest"] = round(time.monotonic() - started, 1)
        if code != 0:
            causes.flush()
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    if args.host_config is not None:
        started = time.monotonic()
        code, summary["capture"] = _host_cycle(args, env, host_options or {})
        seconds["capture"] = round(time.monotonic() - started, 1)
        if code != 0:
            causes.flush()
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    live = None
    originals = None
    if args.live_source_audience is not None:
        from .case_runtime import build_live_readers
        from .ingestion import IngestError, load_live_audience
        drives = list(args.sharepoint_drive)
        if args.sharepoint_original:
            from .sharepoint_originals import folder_source
            originals = folder_source(args.originals_expected, env)
            drives.append(originals["drive_id"])
        try:
            live_audience = load_live_audience(args.live_source_audience, env)
        except IngestError as error:
            parser.error(f"live source audience is invalid: {error.code}")
        live = build_live_readers(live_audience, env, sharepoint_drives=drives,
                                  graph_sender=graph_sender,
                                  bc_sender=(host_options or {}).get("sender"))
    options = dict(originals_options or {})
    if args.enable_local_pdf_ocr:
        options["local_ocr"] = True
    runtime = offline_runtime(args.state_dir, args.mirror_audience, args.tenant, live=live,
                              originals_options=options or None, environ=env)
    investigator = Actor(args.tenant, frozenset(args.principal), args.clearance)
    action_path = None
    actions_file = args.state_dir / "actions.sqlite"
    cases: list[dict[str, Any]] = []
    views: list[Mapping[str, Any] | None] = []
    mapped: _ReaderTools | None = None
    opener: CaseOpener | None = None
    try:
        if actions_file.exists():
            from .action_path import ActionPath
            from .goal2_mc_approval import load_approver_roster, load_bucket_audiences
            try:
                # The oids the host resolved at its start, through the one reader.
                approvers = load_approver_roster(state_dir=args.state_dir)
            except (ValueError, OSError) as error:
                # As the console and the MCP host: a typed refusal, never a traceback
                # (the runtime still closes).
                parser.error("approver roster is invalid: " + str(error)[:200])
            try:
                audiences = load_bucket_audiences(tenant=args.tenant)
            except (ValueError, OSError) as error:
                parser.error("mc bucket audiences are invalid: " + str(error)[:200])
            action_path = ActionPath(actions_file, runtime.cases, **approvers.path_options(),
                                     mc_follow_up_audience=audiences)
        opener = CaseOpener(runtime, investigator, args.state_dir, audience=audience,
                            originals=originals, internal_domains=args.internal_domain,
                            bc_books=_host_books(args.host_config))
        mapped = _ReaderTools(runtime, audience, opener)
        summary["blocked_upstream"] = upstream_parts(opener.upstream) + [
            part for parts in opener.refused.values() for part in parts]
        started = time.monotonic()
        opened = [opener.open_case(kind, identifier, reinvestigate=args.reinvestigate)
                  for kind, identifier in requests]
        seconds["open"] = round(time.monotonic() - started, 1)
        summary["blocked_upstream"] += list(opener.gap_blocked.values())
        started = time.monotonic()
        opened += [{"request": "case_id", "status": "existing", "case_id": case_id}
                   for case_id in args.case_id]
        for index, item in enumerate(opened):
            entry = {key: item[key] for key in ("request", "status", "audience_narrowed",
                                                "pick") if key in item}
            if item.get("missing"):
                entry["missing"] = item["missing"]
            case_id = item.get("case_id")
            if case_id is None:
                cases.append(entry)
                continue
            full = workspace(runtime, investigator, case_id, action_path=action_path)
            views.append(full)
            rendered = [{"reader": sorted(investigator.principals), "investigator": True,
                         **structure(full), **mapped(investigator, full)}]
            for reader in readers:
                view = workspace(runtime, reader, case_id, action_path=action_path)
                views.append(view)
                rendered.append({"reader": sorted(reader.principals), "investigator": False,
                                 **structure(view), **mapped(reader, view),
                                 **leak_check(runtime, investigator, reader, full, view,
                                              audience)})
                if args.output_dir is not None and view is not None:
                    _write(args.output_dir, index, reader, view)
            if args.output_dir is not None and full is not None:
                _write(args.output_dir, index, investigator, full)
            entry["rendered"] = full is not None
            if full is None:
                entry["missing"] = "case_not_readable_for_investigator"
            entry["readers"] = rendered
            cases.append(entry)
        gap, months = _replica_gap(opener, requests, cases)
        # A case rooted without the linked record its pick asked for (``unlinked``) is
        # rendered and read, never counted as the request met: it stays a failure.
        unlinked = sum(1 for entry in cases
                       if entry.get("rendered") and entry.get("status") == "unlinked")
        rendered_count = sum(1 for entry in cases if entry.get("rendered")) - unlinked
        seconds["render"] = round(time.monotonic() - started, 1)
        summary.update(
            cases_requested=len(cases), cases_rendered=rendered_count,
            cases_unlinked=unlinked,
            failed=sum(1 for entry in cases
                       if (not entry.get("rendered") or entry.get("status") == "unlinked")
                       and entry.get("status") != "blocked"),
            blocked=sum(1 for entry in cases if entry.get("status") == "blocked"),
            failed_unnamed=sum(1 for entry in cases
                               if not entry.get("rendered") and not entry.get("missing")),
            row_leaks=sum(r.get("row_leaks", 0) for e in cases for r in e.get("readers", [])),
            withheld_relation_refs=sum(r.get("withheld_relation_refs", 0)
                                       for e in cases for r in e.get("readers", [])),
            readers_with_withheld_markers=sum(
                1 for e in cases for r in e.get("readers", [])
                if not r["investigator"] and (r.get("withheld") or {}).get("total")),
            # Join picks rooted across two systems, and why the others joined nothing.
            cited_cross_rooted=sum(1 for e in cases if e.get("rendered")
                                   and (e.get("pick") or {}).get("joined")),
            pick_root_reasons=_totals(e.get("pick", {}).get("root_reasons") for e in cases),
            pick_carrier_outcomes={key: _totals(
                e.get("pick", {}).get("carrier_outcomes", {}).get(key) for e in cases)
                for key in sorted({k for e in cases
                                   for k in e.get("pick", {}).get("carrier_outcomes", {})})},
            pick_directions=_direction_totals(e.get("pick") for e in cases),
            pick_date_months=months,
            **({"pick_blocked": gap} if gap else {}),
            **panel_totals(cases), cases=cases)
        if opener.run_projection is not None:   # which projection the cases were read on
            summary["projection"] = opener.run_projection.summary()
        causes.flush()
        text = json.dumps(summary, indent=1, sort_keys=True)
        check = summary_check(runtime, investigator, views, text)
    finally:
        if mapped is not None:
            mapped.close()
        if opener is not None:
            opener.close()
        runtime.close()
    if ingested(args.state_dir):
        from .ingestion import row_data_check
        items = row_data_check(args.state_dir, text)
        check = {"values_checked": check["values_checked"] + items["values_checked"],
                 "leaked": check["leaked"] + items["leaked"]}
    summary["summary_row_data_check"] = check
    causes.flush()
    print(json.dumps(summary, indent=1, sort_keys=True))
    wanted = len(cases) if args.min_rendered is None else args.min_rendered
    # A request blocked by a Portal outage is not owed a render; the run then reports
    # blocked_upstream, which the live verifier records as blocked, never as a pass.
    wanted = min(wanted, len(cases) - summary["blocked"])
    ok = (summary["cases_rendered"] >= wanted and not summary["failed_unnamed"]
          and not summary["row_leaks"] and not summary["withheld_relation_refs"]
          and not check["leaked"])
    return 0 if ok else 1


def _replica_gap(opener: CaseOpener, requests: Sequence[tuple[str, str]],
                 cases: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    """The join picks' month buckets (``{type: {YYYY-MM: held objects by latest event}}``,
    counts only: a time-window mismatch reads apart from an environment one) and the
    replica gap: ``GAP_REASON`` when every join pick that failed ran both directions to
    the end (neither clock cut, the BC reader present, the Portal read, no capture of
    either type unfinished, nothing refused) and joined nothing while every root reason is
    ``no_portal_row_*`` or ``not_in_served_replica`` with reverse hits 0 and
    ``linked_matched`` 0: BC names Portal rows the served replica never holds
    (workbench-api-replica-target). Those cases are set ``blocked`` (not ``failed``); any
    other failure in the run, or any other reason mix, leaves it all failing."""
    months: dict[str, Any] = {}
    gapped: list[dict[str, Any]] = []
    db = None
    for (kind, identifier), entry in zip(requests, cases):
        if kind != "pick" or not opener.projection_path.exists():
            continue
        try:
            object_type, linked, join = parse_pick(identifier)
        except OpenError:
            continue
        if not join:
            continue
        if db is None:
            db = sqlite3.connect(opener.projection_path.resolve().as_uri() + "?mode=ro",
                                 uri=True)
        for each in (object_type, linked):
            months[each] = _months(db, each)
        pick = entry.get("pick") or {}
        reverse = (pick.get("directions") or {}).get("reverse") or {}
        forward = (pick.get("directions") or {}).get("forward") or {}
        reasons = {**(pick.get("root_reasons") or {}), **(reverse.get("reasons") or {})}
        bc = reverse.get("bc") or {}
        # BC must have answered: orders read, every lookup read (none unreadable or cut).
        answered = (bc.get("orders", 0) > 0 and not bc.get("orders_unreadable")
                    and bc.get("lookups", 0) > 0
                    and set(bc.get("lookup_states") or ()) == {"read"})
        if (entry.get("status") == "failed" and entry.get("missing") == "pick_none_found"
                and answered
                and not forward.get("budget_spent") and not reverse.get("budget_spent")
                and reverse.get("tried") and bc.get("reader")
                and not reverse.get("joined") and not reverse.get("linked_matched")
                and not bc.get("hits") and reasons
                and all(_GAP_ROOT_REASONS.fullmatch(reason) for reason in reasons)
                and not _unfinished(db, object_type) and not _unfinished(db, linked)):
            gapped.append(entry)
    if db is not None:
        db.close()
    if not gapped or any(entry.get("status") in ("failed", "unlinked") and not any(
            entry is each for each in gapped) for entry in cases):
        return None, months
    for entry in gapped:
        entry["status"] = "blocked"
    return GAP_REASON, months


__all__ = ["CaseOpener", "JOIN_REASONS", "KIND_TYPES", "MIRROR_EVIDENCE", "MissingData",
           "OPEN_KINDS", "OpenError", "carrier_key", "case_tokens", "cited_rows",
           "host_capture_gaps", "host_refusals", "ingested", "join_carriers", "leak_check", "main",
           "offline_runtime", "parse_open", "parse_pick", "portal_upstream", "RunProjection",
           "run_projection", "projection_file", "scope_links", "structure", "summary_check",
           "upstream_parts", "workspace"]


if __name__ == "__main__":
    raise SystemExit(main())
