"""Constants, errors and join-path helpers of the live case workspace
(``operating.case_workspace_live``, which re-exports them)."""

from __future__ import annotations

import contextvars
import re
from typing import Any, Mapping


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
# The ``+LINKED`` pick causes that are a capture's state, never the records': the case is
# rooted without the linked record (status ``unlinked``) and the cause stays reported.
UNLINKED_CAUSES = ("pick_source_not_captured:", "pick_source_not_configured:")
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
OWN_GAPS = 7  # of MAX_GAPS, kept for the case's own gaps (``_Recorder.gap(own=True)``)
MAX_TWINS = 4  # same-record twins whose cards a case records
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


# The schema one case open reads (``CaseOpener.open_case``): loaded at its first use and
# kept to its end, so its bindings, carriers and link index are compiled once per open,
# not once per candidate (46 loads, 74 s of one 60-invoice pick under load, 2026-11-14).
# Outside an open every call loads the schema as it is now.
_OPEN_SCHEMA: contextvars.ContextVar[list[Any] | None] = contextvars.ContextVar(
    "case_open_schema", default=None)


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


def carrier_key(carrier: Mapping[str, Any]) -> str:
    """A join carrier's name in ``carrier_outcomes``, schema names only and short enough
    for a live record's plain keys: its target binding and field
    (``portal.app_sales_order.po_reference``, a two-hop path's cross-system one:
    ``portal.app_shipment.shipment_code``); a two-hop path whose field an earlier carrier
    already names adds its carried property (``join_carriers``)."""
    return carrier.get("key") or f"{carrier['to_binding']}.{carrier['to_field']}"
