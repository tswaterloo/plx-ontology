"""The case workspace over the live source mirror: open a real case, render it per reader.

The ingestion runner (``python3 -m operating.ingestion run``) fills a state directory:
Portal relations into the case runtime's source mirror, mail/SharePoint/BC listings into
its own store; a persistent host (``operating.host``) may hold BC, mail and SharePoint
captures there too. This module never fills or refreshes them. It opens a case
**offline** over those captures (``CaseRuntime(client=None)``: no Workbench API client
exists here, so no Portal request can be made) through the generic path every MCP client
uses, then builds the console's workspace (``operator_ui.case_workspace``: timeline,
originals, gaps, conflicts and actions together) for each reader, every
part re-resolved for that reader. A reader the mirror audience does not grant a relation
sees withheld markers, never the rows.

**Opening a case (the general path, no per-kind code).** The captures the state
directory holds (the Portal mirror, and BC, mail and SharePoint captures when present)
are projected once per run into ``case-workspace-live/ontology.sqlite`` (the one
projector, ``ontology.project.build_store``; a BC-only directory answers BC-only). In one
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
failure: what the source refused is no evidence of none.

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
written. The projection the cases are opened over is one file per tenant
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
from a ``verify/live_verify.py`` result (spec ``verify/live-checks/case.workspace_live.json``).

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

SCHEMA = "plx-case-workspace-live/v1"
# KIND -> the ontology object type plx.find looks for (None: any type).
KIND_TYPES = {"ao": "AssemblyOrder", "sales_order": "SalesOrder",
              "customer_po": "CustomerPurchaseOrder", "petra_po": "PurchaseOrder",
              "find": None}
OPEN_KINDS = (*KIND_TYPES, "pick")
INDEX_DIR = "case-workspace-live"
INDEX_FILE = "opened.json"
MIRROR_EVIDENCE = {
    "label": "source-mirror captures (ingestion runner)",
    "fixture": False,
    "note": ("Rows come from the captures the ingestion runner (operating.ingestion) "
             "wrote into this state directory. This workspace never calls the Portal API "
             "or refreshes them: captures are paged and non-atomic, never prove a "
             "deletion and may be older than the source (each cited row shows its "
             "relation's last refresh). Whether the run that wrote them read live sources "
             "is recorded by that run, not here."),
}
WITHHELD = "withheld_citation_unavailable"
MIN_LEAK_CHARS = 8  # any text this long (operating.ingestion.row_data_check's threshold)
MIN_DIGIT_CHARS = 4  # an identifier or number with a digit: PO numbers, quantities, dates
MAX_REQUESTS = 20
PICK_SCAN = 50  # candidates a pick confirms through the tools at most
# The ``+LINKED`` pick causes that are a capture's state, never the records': the case is
# rooted without the linked record (status ``unlinked``) and the cause stays reported.
UNLINKED_CAUSES = ("pick_source_not_captured:", "pick_source_not_configured:")
# Wall clock one join pick may spend, both directions together (its plx.find calls may
# each probe BC live across companies and entity sets: the live check of 73775fc89 spent
# 37 minutes in the forward direction alone). Every live request inside it is refused
# before I/O once it has passed, and one sent waits at most the time left
# (``live_source_guard.RequestDeadline``); a candidate a refusal cut is not decided.
PICK_BUDGET_SECONDS = 420.0
# Share of it the forward direction may spend: the rest is kept for the reverse one. The
# live check of 4130ffdf7 spent all 420 s on 11 forward roots (each naming no Portal
# row), so the reverse direction, the one that can join when the capture holds few
# invoices, never ran.
FORWARD_SHARE = 0.5
# Wall clock of the live requests of one case open (its pick included): beside a host
# cycle of at most 1500 s (portal 900 + bc 600; about 180 s live) the live check's two
# opens still end inside its 2400 s and print their counts.
OPEN_LIVE_SECONDS = 600.0
MAX_FINDINGS = 150  # cited values recorded per case; the rest are counted in one gap
MAX_GAPS = 60
OWN_GAPS = 7  # of MAX_GAPS, kept for the case's own gaps (``_Recorder.gap(own=True)``)
MAX_TWINS = 4  # same-record twins whose cards a case records
MAX_REACHED = 30  # records the question families reached whose cards a case records
MAP_OBJECTS = 60  # case objects whose links one reader's relationship counts read
MAP_PAGES = 4  # plx.around pages read per case object, link type and direction
MAP_PAGE = 50  # objects per plx.around page (the contract's maximum)
LOWEST_TIER = "contradicted"  # the relationship counts read every tier
CARD_IDENTITY, CARD_LINK_TYPES = 50, 100  # what one plx.object card lists at most
WHO = ["the owner of the source record"]
BLOCKED_UPSTREAM = "blocked_upstream:portal"
_PRINCIPAL = re.compile(r"[A-Za-z0-9_.:@-]{1,120}")
_TYPE = re.compile(r"[A-Z][A-Za-z0-9]{1,63}")
_CLAIM_FIELD = re.compile(r"^(?:row|key|rows\.(?:0|[1-9][0-9]*))(?:\.[A-Za-z_][A-Za-z0-9_]*){1,3}$")
# A document type's machine-read field as its pinned bytes' ``documentFields`` row holds it.
_DOCUMENT_FIELD = re.compile(r"^row\.fields\.[A-Za-z_][A-Za-z0-9_]*\.value$")
# An ontology object id (``Type:binding:{source key}``) as the tools write it into text.
_OBJECT_ID = re.compile(r"[A-Z][A-Za-z0-9]{1,63}:[a-z0-9_]+\.[a-z0-9_.]+:\{[^{}]{0,400}\}")
MAX_CITATIONS = 20  # what one case finding or gap cites at most (operating.cases)
# The plx.object lead kinds a case records (operating/attested_conflicts.py), each as an
# unknown fact finding whose statement starts with LEAD_STATEMENT (structure() counts them).
LEAD_KINDS = ("missing_in_record",)
LEAD_STATEMENT = "Lead ({kind}): "
LEAD_GAPS = ("leads_truncated",)  # plx.object gaps derived from its leads (ontology_tools)
_LEAD_KIND = re.compile(r"^Lead \(([a-z_]{1,40})\): ")
MAX_LEAD_PAGES = 20  # captured pages a lead's stated value is looked for on
MAX_LEAD_PAGE_READS = 40  # plx.read page reads all of one case's leads share
# Why a join pick's candidate joined no record of the other system: one reason each, by
# this precedence over its searched values (``{src}``: the linked binding's system,
# ``{property}``: the first searched property, the direct carrier).
# ``scope_unread``: a scoped carrier's match whose scope party (the root's or the
# candidate's) the tools did not show as one record; ``scope_mismatch``: both read, not
# one record (``CaseOpener._scoped``). ``path_unreached``: a two-hop carrier matched a
# record from which its last hop reaches no one record (``CaseOpener._follow``).
JOIN_REASONS = ("root_not_readable", "no_identifier", "not_in_served_replica",
                "{src}_row_unreadable_for_reader", "find_live_error", "ambiguous_root",
                "scope_unread", "scope_mismatch", "cross_scope", "{src}_carrier_unread",
                "identity_lead_only", "normalized_only", "path_unreached",
                "no_{src}_row_for_{property}")
# A scoped match the root gate refuses (``ontology_tools.scoped_root_lead``'s why) as a
# join reason; None: the owning system never matches that record (a cancelled order).
# Any other why is a lead (``identity_lead_only``).
_SCOPE_WHY = {"identity_scope_ambiguous": "ambiguous_root", "no_scope_value": "scope_unread",
              "scope_value_withheld": "scope_unread", "no_scope_field": "scope_unread",
              "normalized_only": "normalized_only", "scope_status_excluded": None}
# find gaps that say a carrier was not read in full or now: absence is not shown.
_UNREAD_FIND_GAPS = frozenset({"stale_source", "source_unavailable", "freshness_unknown",
                               "no_visible_match", "scan_cap", "projection_not_built",
                               "source_not_configured", "schema_drift"})
_WITHHELD_FIND_GAPS = frozenset({"marking_withheld", "undecided_by_withheld_value"})


class OpenError(ValueError):
    """A request that cannot be opened (``code`` names it; never source text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MissingData(LookupError):
    """The root of a request is not held (``code``: the tool's state, never source text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _schema() -> Any:
    from .ontology_tools import default_schema

    schema = default_schema()
    if schema is None:
        raise OpenError("ontology_schema_unavailable")
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


def _system(binding_id: Any) -> str:
    """The system of a binding id (``portal.app_sales_order`` -> ``portal``)."""
    return str(binding_id).split(".", 1)[0]


def _hops(schema: Any, start: str, *, single: bool) -> list[dict[str, Any]]:
    """One declared link hop from ``start`` per link binding pair, in schema order:
    ``{link_type, direction, to_type, from_binding, field, to_binding, to_field,
    identifier_kind, where}``. A forward hop follows a link as declared; with ``single``
    only hops that reach one record (forward N:1/1:1, or reverse 1:1 such as
    ``same_record_as``), else forward ones only (the direct carriers)."""
    out: list[dict[str, Any]] = []
    for name, link in schema.link_types.items():
        for direction in ("forward", "reverse"):
            ends = (link.get("from"), link.get("to"))
            here_type, there_type = ends if direction == "forward" else ends[::-1]
            if here_type != start or (direction == "reverse" and not single) or (
                    single and link.get("cardinality") not in
                    (("N:1", "1:1") if direction == "forward" else ("1:1",))):
                continue
            for binding in link.get("bindings") or ():
                sides = (binding.get("from_binding"), binding.get("to_binding"))
                here, there = sides if direction == "forward" else sides[::-1]
                if not isinstance(here, str) or not isinstance(there, str):
                    continue
                for pair in binding.get("pairs") or ():
                    fields = (pair.get("from"), pair.get("to"))
                    field, to_field = fields if direction == "forward" else fields[::-1]
                    out.append({"link_type": name, "direction": direction,
                                "to_type": there_type, "from_binding": here, "field": field,
                                "to_binding": there, "to_field": to_field,
                                "identifier_kind": pair.get("identifier_kind"),
                                "where": bool(binding.get("where"))})
    return out


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
    from .ontology_tools import default_grammar, scoped_kind

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


def carrier_key(carrier: Mapping[str, Any]) -> str:
    """A join carrier's name in ``carrier_outcomes``, schema names only and short enough
    for a live record's plain keys: its target binding and field
    (``portal.app_sales_order.po_reference``, a two-hop path's cross-system one:
    ``portal.app_shipment.shipment_code``); a two-hop path whose field an earlier carrier
    already names adds its carried property (``join_carriers``)."""
    return carrier.get("key") or f"{carrier['to_binding']}.{carrier['to_field']}"


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


def ingested(state_dir: str | Path) -> bool:
    """True when the ingestion runner has written this state directory."""
    from .ingestion import STORE_DIR, STORE_FILE

    return (Path(state_dir) / STORE_DIR / STORE_FILE).is_file()


def _read_only(path: Path, query: str) -> list[tuple] | None:
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return None


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


_PART_TEXT = re.compile(r"[A-Za-z0-9_.:-]{1,79}")
_PART_KEYS = ("part", "source", "error_code", "cause", "permission")


def host_refusals(state_dir: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Per projection source (``host.PROJECTED_SOURCE``) of each source other than the
    Portal (``portal_upstream``) the host configures, the ``blocked_upstream`` parts its
    latest read was refused or not answered on, read-only from the host ledger
    (``host.blocked_parts``); none when no host ran. Each part is rebuilt from its names,
    status and codes only; a row that does not read keeps its source blocked
    (``host_ledger_unreadable``), never cleared."""
    from .host import _UPSTREAM, HOST_DIR, LEDGER_FILE, PROJECTED_SOURCE

    host = Path(state_dir) / HOST_DIR / LEDGER_FILE
    rows = (_read_only(host, (
        "SELECT b.source, b.parts FROM source_blocked b JOIN source_state s ON s.source = "
        "b.source WHERE s.configured = 1 AND b.source != 'portal' ORDER BY b.source"))
            or []) if host.is_file() else []
    refused: dict[str, list[dict[str, Any]]] = {}
    for source, text in rows:
        if source not in PROJECTED_SOURCE:
            continue
        try:
            listed = json.loads(text)
        except (TypeError, ValueError):
            listed = None
        parts = []
        for part in listed if isinstance(listed, list) else [None]:
            entry: dict[str, Any] = {key: part[key] for key in _PART_KEYS
                                     if isinstance(part, Mapping) and isinstance(
                                         part.get(key), str)
                                     and _PART_TEXT.fullmatch(part[key])}
            status = part.get("http_status") if isinstance(part, Mapping) else None
            if isinstance(status, int) and not isinstance(status, bool):
                entry["http_status"] = status
            if "part" not in entry or "error_code" not in entry:
                entry = {"part": source, "error_code": "host_ledger_unreadable"}
            parts.append({"source": _UPSTREAM[source], **entry})
        refused[PROJECTED_SOURCE[source]] = parts
    return refused


def upstream_parts(reasons: Sequence[str] | None) -> list[dict[str, str]]:
    """``portal_upstream`` reasons as the live checks' ``blocked_upstream`` parts
    (``verify/live_verify.py``: the check is then blocked, never a pass)."""
    parts = {("host:portal" if reason.startswith("host_error:") else
              "ingestion:portal_relation", reason.split(":", 1)[1][:79])
             for reason in reasons or ()}
    return [{"part": part, "source": "portal-workbench-api", "error_code": code}
            for part, code in sorted(parts)]


def projection_file(state_dir: str | Path, tenant: str) -> Path:
    """Where the case workspace keeps ``tenant``'s projection of the state directory: its
    content (reviewed links, confirmations) is the tenant's, so each tenant has its own
    file, named by a digest of the tenant (never a path the tenant's text could shape)."""
    from hashlib import sha256

    digest = sha256(tenant.encode("utf-8")).hexdigest()[:16]
    return Path(state_dir) / INDEX_DIR / f"ontology-{digest}.sqlite"


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


def project_state(state_dir: str | Path, path: str | Path, *, tenant: str,
                  internal_domains: Sequence[str] = ()) -> dict[str, Any]:
    """Project every capture the state directory holds (the Portal mirror; BC, mail and
    SharePoint captures) into ``path``, offline: no source system is contacted. The new
    store replaces the old one by rename, so a reader never opens a half-built file.

    The build runs the host's ``post_project`` steps (``operating.build_steps``) over the
    state directory's review and confirmation stores for ``tenant``: a link a person
    accepted is ``reviewed`` and one they rejected ``contradicted``
    (``ontology.reviews.link_step``), so a live Portal<->BC customer ``same_record_as``
    link, a lead until reviewed, joins a case pick's scope only once a person accepted it,
    and a confirmed document kind reaches its SharePoint file as the host projects it. The
    stores are read if present, never created or written: the ``confirmations`` step runs
    over a copy made beside ``path`` (``post_project(scratch=)``: the document kinds the
    review store holds are imported into the copy, as the host's next build imports them).
    A review store that is there but unreadable fails the build
    (``ReviewStoreUnreadable``), never a build that would serve a rejected link as a lead."""
    from contextlib import ExitStack

    from ontology import project
    from ontology.schema import load_schema

    from . import build_steps
    from .case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    from .host import state_captures

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.build")
    temp.unlink(missing_ok=True)
    with ExitStack() as stack:
        sources = state_captures(Path(state_dir), stack, internal_domains=internal_domains)
        mirror = Path(state_dir) / CASE_RUNTIME_DIR / MIRROR_FILE
        if mirror.is_file():
            captures = stack.enter_context(project.MirrorCaptures(mirror))
            sources[captures.source] = captures
        steps = build_steps.post_project(state_dir, tenant, scratch=path.parent)
        report = project.build_store(temp, load_schema(), sources, post_project=steps)
    os.replace(temp, path)
    return report


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9#._/-]*")


def _value_texts(value: Any) -> list[str]:
    """Comparable texts of one row value (an integral number also as an integer)."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, float) and value.is_integer():
        return [str(int(value)), str(value)]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+\.0+", text):
            return [text, text.split(".", 1)[0]]
        return [text] if text else []
    return []


def _row_texts(node: Any, out: set[str]) -> None:
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        else:
            out.update(_value_texts(item))


def _plain(value: Any) -> Any:
    """A tool value without its untrusted-text envelope (the source text itself)."""
    if isinstance(value, Mapping) and isinstance(value.get("untrusted_text"), str):
        return value["untrusted_text"]
    return value


def _mirror_relation(handle: Any) -> tuple[str, dict[str, Any]] | None:
    from integrations.source_mirror import parse_citation

    try:
        relation, _canonical, key, _capture = parse_citation(handle)
    except ValueError:
        return None
    return relation, key


def cited_rows(result: Any) -> list[tuple[str, set[str]]]:
    """(relation, value texts) for every mirror-cited value in a tool result: a value
    with its ``citation``, or source text whose envelope names its ``origin`` row. The
    row's key values count as its texts too (an object id carries them)."""
    out: list[tuple[str, set[str]]] = []
    stack = [result]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            cite = item.get("citation")
            handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
            value = item.get("value")
            if handle is None and isinstance(item.get("untrusted_text"), str):
                handle = (item.get("origin") or {}).get("citation")
                value = item["untrusted_text"]
            found = _mirror_relation(handle) if isinstance(handle, str) else None
            if found is not None:
                texts: set[str] = set()
                _row_texts([_plain(value), found[1]], texts)
                out.append((found[0], texts))
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def case_tokens(texts: Sequence[Any]) -> list[str]:
    """Identifier-shaped tokens (a digit, 3+ characters) of the case's own words."""
    out: list[str] = []
    for text in texts:
        for token in _TOKEN.findall(str(text or "")):
            token = token.rstrip("._/-")
            if len(token) >= 3 and any(ch.isdigit() for ch in token) and token not in out:
                out.append(token)
    return out


class _StampedCases:
    """A case store view whose ``open`` records ``kind`` as the question's author.

    The in-process MCP server stamps every ``plx.case_open`` question ``agent`` (an
    agent supplied it). When a person's session opens the case (``SessionIdentity``
    with ``author_kind == "human"``, the console ``--open``), the question is theirs:
    this view replaces the stamp with the session's kind, and ``CaseStore.open``
    still refuses ``human`` for an actor holding an agent principal."""

    def __init__(self, cases: Any, kind: str) -> None:
        self._cases, self._kind = cases, kind

    def open(self, *args: Any, **kwargs: Any) -> str:
        kwargs["objective_author_kind"] = self._kind
        return self._cases.open(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cases, name)


class _StampedRuntime:
    def __init__(self, runtime: Any, kind: str) -> None:
        self._runtime = runtime
        self.cases = _StampedCases(runtime.cases, kind)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)


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
                 marking_grants: Mapping[str, Any] | None = None) -> None:
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
    from .ontology_tools_investigate import _EVIDENCE_TIERS
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
            statement = f"{title}: {entry.get('name')} = {text} ({entry.get('kind')}{caveat})."
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
        self.tool_gaps(answer.get("gaps"), "plx.investigate")
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


def _rooted(candidate: Mapping[str, Any], identifier: str) -> Mapping[str, Any]:
    """The object a lone plx.find candidate roots. A candidate matched on a kind unique
    only within a scope (a customer PO) roots only when it passes the one root gate
    (``ontology_tools.roots_exactly``, read through ``scoped_root_lead``: a root reason or
    a linked record, its one scope and the identifier's literal text); else the request is
    refused ``root_lead_only:<why>``, never opened on a lead. Other kinds root as plx.find
    answers them."""
    from .ontology_tools import default_grammar, scoped_root_lead

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

    from .ontology_tools import _column_path, default_grammar, scoped_kind

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


def _freshness(db: sqlite3.Connection) -> dict[str, Any]:
    """The projection's ``store_meta.source_freshness`` (source -> relation -> facts); {}
    when absent or unreadable."""
    row = db.execute("SELECT value FROM store_meta WHERE key = 'source_freshness'").fetchone()
    try:
        freshness = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        freshness = {}
    return freshness if isinstance(freshness, dict) else {}


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
    from .ontology_tools_investigate import LEAD_RETRY, ask_again

    first, found = ask_again(lambda args: call("plx.around", args),
                             {"from": object_id, "path": path, "limit": 1}, LEAD_RETRY,
                             lambda answer: not answer.get("objects"))
    if not found.get("objects"):
        return None, []
    reached = [str(o["object_id"]) for o in found["objects"]
               if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)]
    return ("evidence" if found is first else "lead"), reached


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
        self._projected = False
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
        """The projection of this state directory's captures, built once per opener."""
        if not self._projected:
            project_state(self.state_dir, self.projection_path, tenant=self.actor.tenant,
                          internal_domains=self.internal_domains)
            self._projected = True
        return self.projection_path

    def open_case(self, kind: str, identifier: str, *, reinvestigate: bool = False
                  ) -> dict[str, Any]:
        """``{"request", "status": opened|reused|unlinked|blocked|failed, "case_id"?,
        "missing"?, "pick"?}`` (``pick``: a join pick's counts and reason codes, also when it
        failed; ``unlinked``: a ``+LINKED`` pick rooted its case without the linked record
        under a capture-state cause, ``missing``).
        ``self.root`` then names the opened case's root (object id and type, and its
        mirror row: relation and key) for an in-process caller; never printed."""
        result = self._open(kind, identifier, reinvestigate)
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

        from .ontology_tools import _column_path, _handle_company, default_grammar, scoped_kind

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

        from .ontology_tools import (ROOT_REASONS, _column_path, _handle_company,
                                     default_grammar, roots_exactly, scoped_kind)

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
        from .ontology_tools import scoped_root_lead

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
        from .ontology_tools_investigate import LEAD_RETRY, ask_again

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
        from operator_ui.object_workspace import plans_for

        from .ontology_tools import _IDENTITY_TIERS

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
        # Every question family from the root and from each verified twin of the same
        # type: each system's own links (an FM assembly order reaches its order line
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
    from operator_ui.case_workspace import CaseWorkspace

    view = CaseWorkspace(runtime, reader, evidence=evidence or MIRROR_EVIDENCE,
                         action_path=action_path, ingestion=ingestion)
    try:
        return view.case(case_id)
    except KeyError:
        return None


def _count(items: Any) -> int:
    return len(items) if isinstance(items, list) else 0


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
    reader sees with an end on one of them; identity edges included) and
    ``relationship_truncated`` (the sorted parts the bounded read could not finish, so a
    count there is a floor, never complete)."""
    if view is None:
        return {}
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
                for edge in around.get("edges") or []:
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


# ------------------------------------------------------------------ CLI


def _reader(text: str, tenant: str, clearance: str):
    from ontology.core import Actor

    principals = [part.strip() for part in str(text).split(",") if part.strip()]
    if not principals or any(not _PRINCIPAL.fullmatch(p) or "*" in p for p in principals):
        raise OpenError("reader_principals_invalid")
    return Actor(tenant, frozenset(principals), clearance)


def _ingest(args: argparse.Namespace, environ: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    """Run ``operating.ingestion run`` into the state directory; counts-only result."""
    from . import ingestion

    argv = ["run", "--state-dir", str(args.state_dir), "--sources", str(args.ingest_sources),
            "--mirror-audience", str(args.mirror_audience), "--runs", str(args.ingest_runs)]
    if args.ingest_live_audience is not None:
        argv += ["--live-audience", str(args.ingest_live_audience)]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ingestion.main(argv, environ=environ)
    try:
        report = json.loads(out.getvalue() or "{}")
    except ValueError:
        report = {}
    if code != 0:
        try:
            error = json.loads(err.getvalue() or "{}").get("error")
        except ValueError:
            error = None
        return code, {"exit_code": code, "error": error if isinstance(error, str) else
                      "ingestion_failed"}
    status = report.get("status") or {}
    return 0, {"exit_code": 0, "runs": [run.get("summary") for run in report.get("runs") or []],
               "cursor_regressions": report.get("cursor_regressions"),
               "row_data_check": report.get("row_data_check"),
               "status_summary": status.get("summary"),
               # Per relation: rows read and why a unit is incomplete (cap reasons).
               "units": [{key: unit.get(key) for key in ("unit_id", "kind", "state",
                                                          "reasons", "counts")}
                         for unit in status.get("units") or []]}


def _host_cycle(args: argparse.Namespace, environ: Mapping[str, str],
                options: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """One capture cycle of the host into the state directory; counts-only result."""
    from .host import Host, HostConfig, HostError

    try:
        report = Host(args.state_dir, HostConfig.from_file(args.host_config),
                      environ=environ, **options).cycle()
    except HostError as error:
        return 1, {"exit_code": 1, "error": error.code}
    return 0, {"exit_code": 0, "state": report.get("state"),
               "sources": {name: {key: outcome.get(key) for key in (
                   "state", "complete", "counts", "reasons") if key in outcome}
                   for name, outcome in (report.get("sources") or {}).items()},
               "projection": {**{key: (report.get("projection") or {}).get(key)
                                 for key in ("state", "objects")},
                              "reference_alias": _reference_alias(report.get("projection")),
                              "reviews": _review_step(report.get("projection"))}}


def _reference_alias(projection: Mapping[str, Any] | None) -> dict[str, Any]:
    """The supplier-number alias join of the build: its typed outcome
    (``ontology.mentions.reference_alias_outcome``: ``alias_joined``, a pending lead, or
    why no candidate was found) and counts only, from the mention step's counts."""
    from ontology.mentions import reference_alias_outcome

    counts = ((projection or {}).get("steps") or {}).get("mentions") or {}
    return {"outcome": reference_alias_outcome(counts),
            **{key: counts.get(key) for key in (
                "reviewed_alias_links", "reviewed_alias_links_open", "alias_numbers",
                "alias_leads", "alias_no_candidate")}}


def _review_step(projection: Mapping[str, Any] | None) -> dict[str, int]:
    """The people's decisions the build projected (``ontology.reviews.link_step``'s
    counts: links, allocations and why a decision was not projected), counts only."""
    counts = ((projection or {}).get("steps") or {}).get("reviews") or {}
    return {key: value for key, value in counts.items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)}


def _host_books(path: Path | None) -> dict[str, dict[str, str]] | None:
    """The BC company books the host config's capture pinned (``bc.books``), so a live
    lookup pins the companies the capture read; None: the default map."""
    if path is None:
        return None
    from integrations.bc_read import load_company_books

    from .host import HostConfig

    books = (HostConfig.from_file(path).sources.get("bc") or {}).get("books")
    return None if books is None else load_company_books(books)


STDERR_CAUSES_MAX = 12
_CAUSE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,23}(?:\.[A-Za-z_][A-Za-z0-9_]{0,23})?")


class _StderrCauses(contextlib.AbstractContextManager):
    """Counts, by typed cause, the noise the run would otherwise write to stderr: pypdf's
    logging records on a malformed mailed PDF, which the reading keeps off stderr where
    pypdf reads (``document_understanding.pypdf_record_listener``), and ``warnings``
    messages, which it keeps off stderr: ``causes`` (``{"records": n, "by_cause":
    {"log:<logger, two name parts>:<LEVEL>" | "warning:<Category>": n}}``), never a
    message, which may carry a document's text. At most ``STDERR_CAUSES_MAX`` causes are
    named; the rest count as ``other``. Any other logging record, a traceback logged with
    ``exc_info`` included, reaches stderr unchanged (``verify/live_verify.py`` reads its
    exception class), as does an exception escaping the run. Warnings are caught
    process-wide (``warnings.catch_warnings``: not thread-safe), so it wraps the CLI only."""

    def __init__(self, causes: dict[str, Any]):
        from integrations import document_understanding

        self.causes = causes
        causes.update(records=0, by_cause={})
        self._lock = threading.Lock()
        self._pypdf = document_understanding.pypdf_record_listener(lambda record: self._count(
            f"log:{_cause_name('.'.join(record.name.split('.')[:2]))}:"
            f"{_cause_name(record.levelname)}"))
        self._warnings = warnings.catch_warnings(record=True)

    def _count(self, cause: str) -> None:
        with self._lock:   # pypdf reads in the capture's worker threads too
            by_cause = self.causes["by_cause"]
            if cause not in by_cause and len(by_cause) >= STDERR_CAUSES_MAX:
                cause = "other"
            by_cause[cause] = by_cause.get(cause, 0) + 1
            self.causes["records"] += 1

    def __enter__(self) -> "_StderrCauses":
        self._pypdf.__enter__()
        self._caught = self._warnings.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.flush()
        self._warnings.__exit__(*exc)
        self._pypdf.__exit__(*exc)

    def flush(self) -> None:
        """Count the warnings caught so far (a summary printed mid-run shows them)."""
        while self._caught:
            self._count(f"warning:{_cause_name(self._caught.pop(0).category.__name__)}")


def _cause_name(text: str) -> str:
    return text if _CAUSE_NAME.fullmatch(text) else "other"


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
        causes.flush()
        text = json.dumps(summary, indent=1, sort_keys=True)
        check = summary_check(runtime, investigator, views, text)
    finally:
        if mapped is not None:
            mapped.close()
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
    # blocked_upstream, which the live checks record as blocked, never as a pass.
    wanted = min(wanted, len(cases) - summary["blocked"])
    ok = (summary["cases_rendered"] >= wanted and not summary["failed_unnamed"]
          and not summary["row_leaks"] and not summary["withheld_relation_refs"]
          and not check["leaked"])
    return 0 if ok else 1


GAP_REASON = "not_in_served_replica"  # the check's blocked string (source_gap)
_GAP_ROOT_REASONS = re.compile(r"no_portal_row_for_\w+|not_in_served_replica")


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


def _months(db: sqlite3.Connection, object_type: str) -> dict[str, int]:
    """Held ``object_type`` objects by month (``YYYY-MM``) of their latest event, the
    pick's own ordering; ``undated`` for none."""
    from ontology.store import removed_at_sql

    out: dict[str, int] = {}
    for (when,) in db.execute(
            "SELECT MAX(COALESCE(e.time, e.window_end)) FROM objects o "
            "LEFT JOIN event_objects eo ON eo.object_id = o.object_id "
            "LEFT JOIN events e ON e.event_id = eo.event_id "
            f"WHERE o.object_type = ? AND NOT {removed_at_sql('o.object_id')} "
            "GROUP BY o.object_id", (object_type,)):
        key = str(when)[:7] if re.fullmatch(r"\d{4}-\d{2}.*", str(when)) else "undated"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _totals(counts: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for each in counts:
        for key, value in (each or {}).items():
            out[key] = out.get(key, 0) + int(value)
    return dict(sorted(out.items()))


def _direction_totals(picks: Any) -> dict[str, dict[str, Any]]:
    """The join picks' per-direction counts summed over the run (``_join_pick``
    ``directions``): numbers added, ``joined`` / ``tried`` / ``budget_spent`` /
    ``bc.reader`` as how many picks, reasons and lookup states per key (``reverse.bc``:
    the BC point lookups of the reverse direction). Counts only; a key the totals do not
    name is dropped, never echoed."""
    bc = {"reader": 0, "orders": 0, "orders_unreadable": 0, "requests": 0, "lookups": 0,
          "lookup_states": {}, "hits": 0, "scanned": 0}
    out: dict[str, dict[str, Any]] = {"forward": {"joined": 0, "scanned": 0,
                                                  "budget_spent": 0},
                                      "reverse": {"joined": 0, "linked_held": 0,
                                                  "linked_matched": 0, "reasons": {},
                                                  "scanned": 0, "start_held": 0,
                                                  "tried": 0, "budget_spent": 0,
                                                  "bc": bc}}

    def add(total: dict[str, Any], counts: Mapping[str, Any]) -> None:
        for key, value in counts.items():
            if key not in total:
                continue
            if isinstance(total[key], dict) and isinstance(value, Mapping):
                if key in ("reasons", "lookup_states"):
                    total[key] = _totals([total[key], value])
                else:
                    add(total[key], value)
            elif isinstance(value, (int, bool)) and not isinstance(total[key], dict):
                total[key] += int(value)

    for pick in picks:
        for name, counts in ((pick or {}).get("directions") or {}).items():
            if name in out and isinstance(counts, Mapping):
                add(out[name], counts)
    return out


def _write(directory: Path, index: int, reader: Any, view: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    name = "_".join(sorted(p.replace(":", "-") for p in reader.principals))[:120]
    (directory / f"case-{index}-{name}.json").write_text(
        json.dumps(view, indent=1, sort_keys=True, default=str), "utf-8")


__all__ = ["CaseOpener", "JOIN_REASONS", "KIND_TYPES", "MIRROR_EVIDENCE", "MissingData",
           "OPEN_KINDS", "OpenError", "carrier_key", "case_tokens", "checkable", "cited_rows",
           "host_refusals", "ingested", "join_carriers", "leak_check", "main",
           "offline_runtime", "parse_open", "parse_pick", "portal_upstream", "project_state",
           "projection_file", "scope_links", "structure", "summary_check", "upstream_parts", "workspace"]


if __name__ == "__main__":
    raise SystemExit(main())
