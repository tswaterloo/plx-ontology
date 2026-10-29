"""Impact chain: which customer orders a late purchase order puts at risk, by how many days
and for how much, walked only over recorded links.

From each late or slipping purchase order (``plx.process`` ``po_delivery_window``'s own
lateness verdict, never re-decided here) the walk follows the links a source records:

    PO (and its twins) <- PurchaseOrderLine.line_of - PO line - PurchaseOrderLine.for_item ->
    item (and its twins, Product.same_record_as) <- AssemblyOrderComponent.uses_item -
    FileMaker assembly-order component - AssemblyOrderComponent.component_of -> assembly
    order (and its twins: the Portal manufacturing order) - AssemblyOrder.for_order_line, or
    <- AssemblyOrderAllocation.allocates - allocation - AssemblyOrderAllocation.for_order_line
    -> order line - SalesOrderLine.line_of -> customer order (and its twins).

Only links at a firm tier (verified, reviewed, corroborated) count; a lead-tier link
(``stored_by_text_join``, ``lead``) is the typed gap ``link_lead_only`` and a contradicted
one is no link. Nothing is joined by a shared code, date or amount. Where the next link of a
chain is not recorded the chain stops at a typed gap (``BREAKS``) that names the source that
would hold it; ``absent_in_source`` only when the walk was complete and the reader's scope
covers the link types, else ``unknown``.

Per affected customer order (an order cluster: an order and its recorded twins):

* **material date** of a PO: its received date when the lateness verdict measured a full
  receipt, else the later of the date it was measured on (today) and the supplier's revised
  promise. A PO the verdict flags ``at_risk`` (the shared po-status rule,
  ``operating.po_status.lateness_verdict``, applied by ``plx.process``; not decided here)
  is at risk at the supplier's latest promise (revised, else the one promised date). Any
  other verdict puts no order at risk; an ``undetermined`` one, or an at-risk one with no
  promise date, is the gap ``lateness_undetermined``.
* **days at risk** = material date + recorded lead time - the order's required (promised)
  date, the worst over the POs that feed it. No source the Workbench reads records an
  assembly lead time, so it counts 0 with the gap ``assembly_lead_time_not_recorded`` and
  days at risk is a lower bound.
  Required dates that disagree across the order's twins leave the days null with a gap.
  The order is at risk when the days are above 0.
* **value at risk**: the order's cited total (``SalesOrder.total_amount``, a finance-marked
  value) as a Decimal with its ISO 4217 currency, only when the reader was shown it and the
  cited field re-read for the reader states the same number. A reader without money
  visibility gets the days and no value; the gap then never says whether a value exists.
  Twins whose totals or currencies differ give no value. Values are never added across
  currencies, and orders are ranked by days only, so the order never hints at value.

* **margin at stake** (``order_margin``, finance): per order record of the cluster with
  recorded lines (``SalesOrderLine.line_of``), each line's cited sales amount
  (``SalesOrderLine.line_total``, excluding tax: Portal ``total_price``, BC
  ``amountExcludingTax``) minus its ordered quantity times the sold item's unit cost
  (``SalesOrderLine.for_item``, then the item's twins): the landed-cost engine's per-unit
  total when it has one (actual, else its estimate, labelled "margin estimated"), else BC
  ``Item.unitCost`` labelled "recorded unit cost, excludes freight/duty" (``COST_BASES``);
  only a positive cost that states its currency and its per-unit unit. A margin over more
  than one basis names each one (``basis_label``), never mixed silently. The quantity's unit
  must be the cost's unit and the amount's currency the cost's: nothing is converted. A
  record states a margin only when every line does (never a part as the whole); twins that
  state different margins give none. A reader not shown the values gets the chain and days
  with a gap that never says whether a value exists. Each late PO's impact
  (``po_impacts``): the orders at risk it feeds and, when every one has a margin in one
  currency, their sum and basis: ``operating.proactive_engine`` raises it as ``po_impact``
  (no money) and ``po_impact_margin`` (finance-marked citations: finance readers only).

Orders whose every readable status is closed (``CLOSED_ORDER``) and assembly orders whose
every readable status is done or cancelled (``CLOSED_ASSEMBLY``) put nothing at risk and
are counted as excluded.

Two drivers share the walk: ``plan_run`` (the ``plx.investigate`` engine of
``ontology/plans/impact_chain.json``, family F16: the edges the plan's steps reached and the
entries its evidence items read) and ``scan`` (a set-based pass over the projection store
for ``operating.proactive_engine``'s top finding and the live probe, with the lateness
verdict asked of ``plx.process`` per PO). Evidence label of fixture runs:
``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Callable, Container, Iterable, Mapping, Sequence

ENGINE = "impact_chain"
FIRM_TIERS = ("verified", "reviewed", "corroborated")
LEAD_TIERS = ("stored_by_text_join", "lead")
MAX_TWINS = 32
MAX_CHAINS = 400          # chains walked from one purchase order before the walk is cut
MAX_STEP_CHARS = 400
MAX_GAP_SUBJECTS = 5
CLOSED_ORDER = frozenset({"shipped", "invoiced", "closed", "cancelled", "canceled",
                          "completed", "complete", "delivered"})
CLOSED_ASSEMBLY = frozenset({"realized", "completed", "complete", "cancelled", "canceled"})
LEAD_TIME_GAP = "assembly_lead_time_not_recorded"

# Each recorded link the walk crosses, by its role.
PO_TWIN = "PurchaseOrder.same_record_as"
# The Portal entity map (ontology/schema/domains/bc_entity_map.json): a Portal record and the
# BC record the drain posted it as, joined on the BC systemId, so the BC end is one record of
# one company. A twin like same_record_as, crossed at its own (verified) tier.
PO_POSTED = "PurchaseOrder.bc_posted_as"
PO_LINE = "PurchaseOrderLine.line_of"
LINE_ITEM = "PurchaseOrderLine.for_item"
ITEM_TWIN = "Product.same_record_as"
ITEM_POSTED = "Product.bc_posted_as"
PO_TWINS = (PO_TWIN, PO_POSTED)
ITEM_TWINS = (ITEM_TWIN, ITEM_POSTED)
USES_ITEM = "AssemblyOrderComponent.uses_item"
COMPONENT_OF = "AssemblyOrderComponent.component_of"
AO_TWIN = "AssemblyOrder.same_record_as"
AO_LINE = "AssemblyOrder.for_order_line"
ALLOCATES = "AssemblyOrderAllocation.allocates"
ALLOCATION_LINE = "AssemblyOrderAllocation.for_order_line"
ORDER_LINE = "SalesOrderLine.line_of"
ORDER_TWIN = "SalesOrder.same_record_as"
LINK_TYPES = (*PO_TWINS, PO_LINE, LINE_ITEM, *ITEM_TWINS, USES_ITEM, COMPONENT_OF, AO_TWIN,
              AO_LINE, ALLOCATES, ALLOCATION_LINE, ORDER_LINE, ORDER_TWIN)
# Margin at stake: an order's lines (ORDER_LINE reversed), the item each sells and its twins.
LINE_SELLS = "SalesOrderLine.for_item"
MARGIN_LINK_TYPES = (ORDER_LINE, LINE_SELLS, *ITEM_TWINS, ORDER_TWIN)
REVENUE_PROP = "line_total"           # SalesOrderLine, finance, excluding tax
QUANTITY_PROP = "ordered_quantity"    # SalesOrderLine, with its unit
# The unit cost a margin rests on:
# the landed-cost engine's per-unit total of the sold
# item when it has one (actual, else its labelled estimate), else BC Item.unitCost (CAD per
# base unit), which leaves freight and duty out (ERP-D-015). Each basis keeps its label; a
# margin over more than one basis names each one (never mixed silently).
COST_PROPS = ("bc_unit_cost",)
LANDED_ACTUAL, LANDED_ESTIMATE, RECORDED_COST = ("landed_actual", "landed_estimate",
                                                 "recorded_unit_cost")
COST_BASES = {
    LANDED_ACTUAL: "landed cost (landed-cost engine, actual)",
    LANDED_ESTIMATE: "margin estimated (landed-cost engine estimate, not the actual landed "
                     "cost)",
    RECORDED_COST: "recorded unit cost, excludes freight/duty",
}
LANDED_FAMILY = "F7"          # ontology/plans/landed_cost.json
MAX_LANDED_POS = 25           # purchase orders whose landed cost one scan asks for
_LANDED_LINE = re.compile(r"_(estimate_)?line_[0-9]+$")


def basis_label(bases: Iterable[str]) -> str:
    """The wording of the cost bases a margin rests on: one label, or each named."""
    found = [b for b in COST_BASES if b in set(bases)]
    if len(found) == 1:
        return COST_BASES[found[0]]
    return "mixed basis: " + "; ".join(COST_BASES[b] for b in found) if found else "unknown"


MARGIN_WHY = {
    "line_amount_not_shown": "a line's sales amount or quantity is not one this reader was "
                             "shown and could re-read (whether one is recorded is not said)",
    "landed_cost_not_shown": "a sold item's unit cost is not one this reader was shown and "
                             "could re-read (whether one is recorded is not said)",
    "landed_cost_unit_or_currency_not_stated": "the sold item's recorded cost states no "
                                               "currency or no per-unit unit, so it is not "
                                               "used",
    "landed_cost_not_positive": "the sold item's recorded unit cost is zero or less (an "
                                "unpriced item), never a zero cost",
    "landed_cost_twins_disagree": "the sold item's records state different unit costs",
    "landed_costs_disagree": "the landed-cost engine gives the sold item different per-unit "
                             "landed costs (several purchase orders); none is chosen and no "
                             "recorded unit cost stands in for it",
    "cost_unit_differs_from_quantity_unit": "the line's quantity unit is not the cost's "
                                            "unit; nothing is converted",
    "margin_currency_differs": "the sales amount and the cost are in different currencies; "
                               "nothing is converted",
    "line_values_disagree": "a line's records state different amounts or quantities",
    "line_item_not_recorded": "a line names no item over a recorded link "
                              "(SalesOrderLine.for_item)",
    "line_items_disagree": "a line links to more than one item",
    "order_lines_not_recorded": "no line of the order is recorded (SalesOrderLine.line_of)",
    "order_margins_disagree": "the order's records in different systems give different "
                              "margins",
}
MARGIN_BASIS = ("margin at stake = sales amount excluding tax - ordered quantity x unit cost, "
                "per line, only where both are cited in one currency and the quantity's unit "
                "is the cost's; the unit cost is the landed-cost engine's per-unit total when "
                "it has one (actual, else a labelled estimate), else the BC recorded unit cost, "
                "which excludes freight/duty (ERP-D-015)")

# A chain that stops: reason -> (link types that would carry it, the source that would).
BREAKS: dict[str, tuple[tuple[str, ...], str]] = {
    "po_line_not_recorded": (
        (PO_LINE, *PO_TWINS), "the purchase order's line rows (Portal app_purchase_order_line, "
        "Business Central purchase order lines)"),
    "line_item_not_recorded": (
        (LINE_ITEM,), "the item reference on the purchase order line (Portal product_id, "
        "BC line itemId)"),
    "item_not_used_by_assembly_order": (
        (USES_ITEM, *ITEM_TWINS), "FileMaker assembly order component rows "
        "(fm_assembly_order_item); Business Central assembly consumption is not ingested"),
    "component_without_assembly_order": (
        (COMPONENT_OF,), "the assembly order id on the FileMaker component row"),
    "assembly_order_not_linked_to_order_line": (
        (AO_LINE, ALLOCATES, ALLOCATION_LINE, AO_TWIN), "the Portal manufacturing order's "
        "sales order line or FileMaker order-entry assembly allocations "
        "(fm_order_entry_item_assembly)"),
    "allocation_without_order_line": (
        (ALLOCATION_LINE,), "the order-entry line id on the FileMaker assembly allocation"),
    "order_line_not_linked_to_order": (
        (ORDER_LINE,), "the order id on the sales order line row"),
    "lateness_undetermined": (
        (), "plx.process po_delivery_window (its own gap says which date is unproven)"),
    "link_lead_only": (
        (), "a firm-tier record of the link (only a lead-tier link was found)"),
}
# The scan considers a PO whose walk reaches a consuming assembly component: its chains
# either reach a customer order or stop at one of these (a PO whose items no assembly order
# consumes is counted apart, not as a broken chain: most stock buys feed no order).
REACHES_ASSEMBLY = frozenset({"component_without_assembly_order",
                              "assembly_order_not_linked_to_order_line",
                              "allocation_without_order_line",
                              "order_line_not_linked_to_order", "link_lead_only"})
# Where a missing link sits: before a consuming assembly component (the PO feeds no
# assembly order on record), past it (a chain toward a customer order stops), or at the
# lateness verdict. ``no_late_purchase_order`` is not a missing link: the links reach an
# assembly order, but no PO on them is late or slipping, so no chain is walked.
BEFORE_ASSEMBLY = frozenset({"po_line_not_recorded", "line_item_not_recorded",
                             "item_not_used_by_assembly_order"})
NO_LATE_PO = "no_late_purchase_order"
MISSING: dict[str, tuple[tuple[str, ...], str]] = {
    **BREAKS, NO_LATE_PO: ((), "plx.process po_delivery_window (no PO whose links reach an "
                               "assembly order is late or slipping)")}
MAX_MISSING = 5
DAYS_AT_RISK_BASIS = ("days at risk is a lower bound: no assembly lead time is recorded, "
                      "so 0 days are counted from component arrival to shipment")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LATE_KEYS = ("po", "state", "governing", "measured", "revised", "received", "at_risk",
              "promised")
ARG_KEYS = {"lateness", "required", "order_status", "value", "currency", "assembly_status"}
# Optional: all three or none (no margin is computed without them).
MARGIN_ARG_KEYS = {"line_amount": ("SalesOrderLine",), "line_quantity": ("SalesOrderLine",),
                   "item_cost": ("Product",)}


def _type_of(object_id: str) -> str:
    return str(object_id).split(":", 1)[0]


def _day(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None or isinstance(value, float):
        return None   # a float is not a cited money text
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return number if number.is_finite() else None


def _dec_str(value: Decimal) -> str:
    return format(value, "f")


def _digest(entry: dict[str, Any]) -> dict[str, Any]:
    entry["digest"] = hashlib.sha256(json.dumps(
        {k: entry.get(k) for k in ("name", "value", "inputs", "steps", "basis")},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return entry


# --------------------------------------------------------------------------- the walk

@dataclass(frozen=True)
class Edge:
    link_id: str
    link_type: str
    frm: str
    to: str
    tier: str
    markings: str = "[]"

    def cited(self) -> str:
        return (f"{self.link_type} {self.frm} -> {self.to} (tier {self.tier}, "
                f"link {self.link_id})")


@dataclass
class Late:
    """One purchase order's lateness verdict as ``plx.process`` stated it."""
    po: str
    state: str | None
    governing: date | None = None
    measured: date | None = None
    revised: date | None = None
    received: date | None = None
    at_risk: bool | None = None      # the shared po-status rule's flag, as plx.process stated it
    promised: date | None = None
    inputs: list[str] = field(default_factory=list)   # answer entries (plan) it rests on
    cites: list[Any] = field(default_factory=list)    # cited values (scan) it rests on

    def material(self) -> tuple[date | None, str]:
        """(the date the material can be in, how it was read); None: not at risk."""
        if self.state == "late":
            if self.received is not None:
                return self.received, f"received {self.received.isoformat()}"
            if self.measured is None:
                return None, "late without a measured date"
            when = max(d for d in (self.measured, self.revised) if d is not None)
            how = (f"revised promise {self.revised.isoformat()}" if when == self.revised
                   and self.revised != self.measured else
                   f"not received as of {self.measured.isoformat()}")
            return when, how
        if self.state == "not_due" and self.at_risk is True:
            when = self.revised or self.promised
            if when is None:
                return None, "at risk without a promise date"
            return when, f"supplier promise {when.isoformat()} after the requested-by date"
        return None, str(self.state)


@dataclass
class Chain:
    po: str
    order: str                 # the order cluster's key (its least object id)
    edges: list[Edge]
    records: list[str]


@dataclass
class Break:
    reason: str
    subject: str
    po: str
    lead_only: bool = False


@dataclass
class Order:
    key: str
    members: list[str]
    chains: list[Chain]
    required: date | None = None
    required_from: list[str] = field(default_factory=list)
    days: int | None = None
    worst: str | None = None
    material: date | None = None
    material_how: str = ""
    why_null: str | None = None

    @property
    def at_risk(self) -> bool:
        return self.days is not None and self.days > 0


@dataclass
class Assessment:
    considered: list[str]              # late or slipping POs walked
    orders: list[Order]                # affected orders, worst first
    chains: int
    breaks: list[Break]
    excluded: dict[str, int]
    undetermined: list[str]
    cut: bool

    @property
    def at_risk(self) -> list[Order]:
        return [o for o in self.orders if o.at_risk]

    @property
    def broken_share(self) -> str | None:
        """Share of chain ends that stop at a missing link, as a percentage text."""
        total = self.chains + len(self.breaks)
        if not total:
            return None
        return str((Decimal(len(self.breaks)) * 100 / Decimal(total)).quantize(Decimal("0.1")))


class Graph:
    """The recorded links the walk may cross; lead-tier links kept apart (never crossed)."""

    def __init__(self, edges: Iterable[Edge], link_types: Container[str] = LINK_TYPES) -> None:
        self.out: dict[tuple[str, str], list[Edge]] = {}
        self.inn: dict[tuple[str, str], list[Edge]] = {}
        self.lead_out: set[tuple[str, str]] = set()
        self.lead_in: set[tuple[str, str]] = set()
        self.lead_ends: dict[tuple[str, str], set[str]] = {}   # (type, end) -> other ends
        seen: set[str] = set()
        for edge in edges:
            if edge.link_type not in link_types or edge.link_id in seen:
                continue
            seen.add(edge.link_id)
            if edge.tier in FIRM_TIERS:
                self.out.setdefault((edge.link_type, edge.frm), []).append(edge)
                self.inn.setdefault((edge.link_type, edge.to), []).append(edge)
            elif edge.tier in LEAD_TIERS:
                self.lead_out.add((edge.link_type, edge.frm))
                self.lead_in.add((edge.link_type, edge.to))
                self.lead_ends.setdefault((edge.link_type, edge.frm), set()).add(edge.to)
                self.lead_ends.setdefault((edge.link_type, edge.to), set()).add(edge.frm)
        for table in (self.out, self.inn):
            for key in table:
                table[key].sort(key=lambda e: (e.frm, e.to, e.link_id))

    def fwd(self, link_type: str, oid: str) -> list[tuple[str, Edge]]:
        return [(e.to, e) for e in self.out.get((link_type, oid), ())]

    def rev(self, link_type: str, oid: str) -> list[tuple[str, Edge]]:
        return [(e.frm, e) for e in self.inn.get((link_type, oid), ())]

    def lead(self, link_type: str, oid: str, forward: bool) -> bool:
        return (link_type, oid) in (self.lead_out if forward else self.lead_in)

    def twins(self, link_type: str | Sequence[str], oid: str
              ) -> tuple[dict[str, list[Edge]], bool]:
        """``oid`` and its recorded twins (both directions, two links deep, over any of the
        twin link types given) with the edges crossed to reach each; True when the cap cut
        the cluster."""
        kinds = (link_type,) if isinstance(link_type, str) else tuple(link_type)
        via: dict[str, list[Edge]] = {oid: []}
        frontier, capped = [oid], False
        for _ in range(2):
            reached = []
            for start in frontier:
                for other, edge in [pair for kind in kinds
                                    for pair in self.fwd(kind, start) + self.rev(kind, start)]:
                    if other in via:
                        continue
                    if len(via) >= MAX_TWINS:
                        capped = True
                        continue
                    via[other] = [*via[start], edge]
                    reached.append(other)
            frontier = reached
        return via, capped

    def cluster(self, link_type: str | Sequence[str], oid: str) -> list[str]:
        return sorted(self.twins(link_type, oid)[0])


def walk(po: str, graph: Graph) -> tuple[list[Chain], list[Break], bool]:
    """Every chain from ``po`` to a customer order over recorded links (one per order
    cluster, the first path in id order), and a typed break where a chain stops."""
    chains: dict[str, Chain] = {}
    breaks: dict[tuple[str, str], Break] = {}
    cut = False

    def stop(reason: str, subject: str, link_type: str, forward: bool,
             lead_twin: bool = False) -> None:
        lead = lead_twin or graph.lead(link_type, subject, forward)
        key = ("link_lead_only" if lead else reason, subject)
        breaks.setdefault(key, Break(key[0], subject, po, lead))

    pos, capped = graph.twins(PO_TWINS, po)
    cut = cut or capped
    lines = [(pol, [*path, e], [*([p] if p != po else []), pol])
             for p, path in sorted(pos.items()) for pol, e in graph.rev(PO_LINE, p)]
    if not lines:
        stop("po_line_not_recorded", po, PO_LINE, False)
    seen_items: set[str] = set()
    seen_aos: set[str] = set()
    for pol, path, records in lines:
        items = graph.fwd(LINE_ITEM, pol)
        if not items:
            stop("line_item_not_recorded", pol, LINE_ITEM, True)
        for item, e_item in items:
            if item in seen_items:
                continue
            twins, capped = graph.twins(ITEM_TWINS, item)
            cut = cut or capped
            seen_items.update(twins)
            uses = [(comp, [*path, e_item, *tpath, e], [*records, item,
                                                         *([t] if t != item else []), comp])
                    for t, tpath in sorted(twins.items()) for comp, e in graph.rev(USES_ITEM, t)]
            if not uses:
                # An item with a lead-tier twin outside its firm cluster (a BC item reached
                # by its number alone) stops at that lead, never as "not used".
                stop("item_not_used_by_assembly_order", item, USES_ITEM, False,
                     lead_twin=any(other not in twins for t in twins for kind in ITEM_TWINS
                                   for other in graph.lead_ends.get((kind, t), ())))
            for comp, cpath, crecords in uses:
                aos = graph.fwd(COMPONENT_OF, comp)
                if not aos:
                    stop("component_without_assembly_order", comp, COMPONENT_OF, True)
                for ao, e_ao in aos:
                    if ao in seen_aos:
                        continue
                    ao_twins, capped = graph.twins(AO_TWIN, ao)
                    cut = cut or capped
                    seen_aos.update(ao_twins)
                    base = [*cpath, e_ao]
                    brecords = [*crecords, ao]
                    order_lines: list[tuple[str, list[Edge], list[str]]] = []
                    allocated = False
                    for a, apath in sorted(ao_twins.items()):
                        arec = [*brecords, *([a] if a != ao else [])]
                        for sol, e in graph.fwd(AO_LINE, a):
                            order_lines.append((sol, [*base, *apath, e], [*arec, sol]))
                        for alloc, e_alloc in graph.rev(ALLOCATES, a):
                            allocated = True
                            found = graph.fwd(ALLOCATION_LINE, alloc)
                            if not found:
                                stop("allocation_without_order_line", alloc, ALLOCATION_LINE,
                                     True)
                            for sol, e in found:
                                order_lines.append((sol, [*base, *apath, e_alloc, e],
                                                    [*arec, alloc, sol]))
                    if not order_lines and not allocated:   # else the allocation's break
                        stop("assembly_order_not_linked_to_order_line", ao, AO_LINE, True)
                    for sol, spath, srecords in order_lines:
                        orders = graph.fwd(ORDER_LINE, sol)
                        if not orders:
                            stop("order_line_not_linked_to_order", sol, ORDER_LINE, True)
                        for so, e_so in orders:
                            key = graph.cluster(ORDER_TWIN, so)[0]
                            if key in chains:
                                continue
                            if len(chains) >= MAX_CHAINS:
                                cut = True
                                continue
                            chains[key] = Chain(po, key, [*spath, e_so],
                                                list(dict.fromkeys([po, *srecords, so])))
    return list(chains.values()), list(breaks.values()), cut


def _text(value: Any) -> str | None:
    """A status as text: a plain string or the tools' ``{"untrusted_text": ...}`` wrapper
    (data, compared only against a closed set, never followed)."""
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _closed(statuses: Iterable[Any], closed: frozenset[str]) -> bool:
    said = [t.lower() for s in statuses if (t := _text(s)) is not None]
    return bool(said) and all(s in closed for s in said)


def assess(late: Mapping[str, Late], graph: Graph, *,
           required: Mapping[str, Sequence[tuple[date, str]]],
           order_status: Mapping[str, Sequence[str]],
           assembly_status: Mapping[str, Sequence[str]]) -> Assessment:
    """Walk every late or slipping PO and put days at risk on each order it feeds.

    ``required``: order object id -> [(required date, its reference)]; ``order_status``
    and ``assembly_status``: object id -> readable status texts."""
    excluded = {"po_not_late": 0, "order_closed": 0, "assembly_order_closed": 0}
    considered: list[str] = []
    undetermined: list[str] = []
    breaks: list[Break] = []
    orders: dict[str, Order] = {}
    chains = 0
    cut = False
    for po in sorted(late):
        verdict = late[po]
        material, how = verdict.material()
        if verdict.state == "undetermined" or verdict.state not in (
                "late", "on_time", "not_due", "not_applicable") \
                or ((verdict.state == "late" or verdict.at_risk is True)
                    and material is None):   # late or at risk, but undated
            undetermined.append(po)
            breaks.append(Break("lateness_undetermined", po, po))
            continue
        if material is None:
            excluded["po_not_late"] += 1
            continue
        considered.append(po)
        found, stops, capped = walk(po, graph)
        cut = cut or capped
        breaks.extend(stops)
        for chain in found:
            closed_ao = next((r for r in chain.records if _type_of(r) == "AssemblyOrder"
                              and _closed([s for t in graph.cluster(AO_TWIN, r)
                                           for s in assembly_status.get(t, ())],
                                          CLOSED_ASSEMBLY)), None)
            if closed_ao is not None:
                excluded["assembly_order_closed"] += 1
                continue
            members = graph.cluster(ORDER_TWIN, chain.order)
            if _closed([s for m in members for s in order_status.get(m, ())], CLOSED_ORDER):
                excluded["order_closed"] += 1
                continue
            chains += 1
            order = orders.setdefault(chain.order, Order(chain.order, members, []))
            order.chains.append(chain)
    for order in orders.values():
        dates = {d: ref for m in order.members for d, ref in
                 sorted(required.get(m, ()), key=lambda pair: pair[1])}
        order.required_from = [ref for m in order.members for _d, ref in required.get(m, ())]
        if not dates:
            order.why_null = "order_required_date_not_recorded"
            continue
        if len(dates) > 1:
            order.why_null = "order_required_dates_disagree"
            continue
        (order.required,) = dates
        for chain in order.chains:
            material, how = late[chain.po].material()
            days = (material - order.required).days   # lead time: none recorded (0)
            if order.days is None or days > order.days:
                order.days, order.worst = days, chain.po
                order.material, order.material_how = material, how
    ranked = sorted(orders.values(), key=lambda o: (
        o.days is None, -(o.days if o.days is not None else 0), o.key))
    return Assessment(considered, ranked, chains, breaks, excluded, undetermined, cut)


def break_gap(reasons: Mapping[str, list[Break]], reason: str, *, complete: bool,
              covers: Callable[[str], bool]) -> dict[str, Any]:
    """One gap per break reason (the run's gap list is capped): the count, the first
    subjects and the source that would hold the missing link."""
    found = reasons[reason]
    types, source = BREAKS.get(reason, ((), "unknown"))
    known = complete and bool(types) and all(covers(t) for t in types) \
        and reason not in ("lateness_undetermined", "link_lead_only")
    subjects = ", ".join(sorted({b.subject for b in found})[:MAX_GAP_SUBJECTS])
    more = len({b.subject for b in found}) - MAX_GAP_SUBJECTS
    detail = (f"{reason}: {len(found)} chain(s) stop here; the source that would hold the "
              f"link: {source}"
              + ("; the walk read every such link this reader may see" if known else
                 "; the walk was cut or the reader's scope may hide one, so this is not "
                 "proof that none is recorded")
              + "; no link is inferred from a shared code, date or amount")
    return {"state": "absent_in_source" if known else "unknown", "reason": reason,
            "subject": (subjects + (f" and {more} more" if more > 0 else ""))[:700],
            "detail": detail[:600]}


def lead_time_gap(subject: str) -> dict[str, Any]:
    return {"state": "not_ingested", "reason": LEAD_TIME_GAP, "subject": subject[:700],
            "detail": (f"{LEAD_TIME_GAP}: no source the Workbench reads records the time "
                       "from component arrival to shipment, so it counts 0 days: days at "
                       "risk is a lower bound")[:600]}


# --------------------------------------------------------------------------- the plan engine

def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with ``engine_args``: ``lateness`` names the per-PO select items
    (``po``: the select of the verdict's root, then ``state``, ``governing``, ``measured``,
    ``revised``, ``received``, ``at_risk``, ``promised``), ``required``, ``order_status``, ``value`` and ``currency``
    name SalesOrder property items and ``assembly_status`` an AssemblyOrder one, each
    listed before the compute item."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    out = [f"unknown engine argument {k}"
           for k in sorted(set(args) - ARG_KEYS - set(MARGIN_ARG_KEYS))]
    out += [f"missing engine argument {k}" for k in sorted(ARG_KEYS - set(args))]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    margin = set(args) & set(MARGIN_ARG_KEYS)
    if margin and margin != set(MARGIN_ARG_KEYS):
        out.append("line_amount, line_quantity and item_cost come together or not at all")
    for key in sorted(margin):
        item = items.get(args.get(key))
        if item is None or item["kind"] != "property" or \
                item.get("object_type") not in MARGIN_ARG_KEYS[key]:
            out.append(f"{key} must name a {MARGIN_ARG_KEYS[key][0]} property item listed "
                       "before it")
    late = args.get("lateness")
    if not isinstance(late, Mapping) or set(late) != set(_LATE_KEYS):
        out.append("lateness must name " + ", ".join(_LATE_KEYS))
    else:
        for key in _LATE_KEYS:
            item = items.get(late[key])
            if item is None or item["kind"] != "select" or item.get("per") != "root":
                out.append(f"lateness.{key} must name a select item per root listed before it")
    for key, type_id in (("required", "SalesOrder"), ("order_status", "SalesOrder"),
                         ("value", "SalesOrder"), ("currency", "SalesOrder"),
                         ("assembly_status", "AssemblyOrder")):
        item = items.get(args.get(key))
        if item is None or item["kind"] != "property" or item.get("object_type") != type_id:
            out.append(f"{key} must name a {type_id} property item listed before it")
    links = getattr(schema, "link_types", None) or {}
    if links:
        out += [f"undeclared link type {t}"
                for t in dict.fromkeys(LINK_TYPES + MARGIN_LINK_TYPES) if t not in links]
    return out


def _suffix(name: str, prefix: str) -> str | None:
    rest = name[len(prefix) + 1:] if name.startswith(prefix + "_") else None
    return rest if rest is not None and rest.isdigit() else None


def _plan_late(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]]
               ) -> dict[str, Late]:
    names = args["lateness"]
    by_n: dict[str, dict[str, Mapping[str, Any]]] = {}
    for key in _LATE_KEYS:
        for entry in evidence.get(names[key], ()):
            n = _suffix(str(entry.get("name")), names[key])
            if n is not None:
                by_n.setdefault(n, {})[key] = entry
    out: dict[str, Late] = {}
    for n, got in by_n.items():
        po = (got.get("po") or {}).get("value")
        if not isinstance(po, str) or _type_of(po) != "PurchaseOrder" or po in out:
            continue

        def value(key: str) -> Any:
            return (got.get(key) or {}).get("value")
        out[po] = Late(po, value("state") if isinstance(value("state"), str) else None,
                       _day(value("governing")), _day(value("measured")), _day(value("revised")),
                       _day(value("received")),
                       at_risk=value("at_risk") if isinstance(value("at_risk"), bool) else None,
                       promised=_day(value("promised")),
                       inputs=[got[k]["name"] for k in _LATE_KEYS if k in got])
    return out


class _Reread:
    """A cited value re-read for the actor: used only when the source says the same."""

    def __init__(self, resolver: Any, actor: Any) -> None:
        self.resolver, self.actor, self.views = resolver, actor, {}

    def same(self, entry: Mapping[str, Any]) -> bool:
        from operating.cases import _field
        citation = entry.get("citation") or {}
        handle = citation.get("l1_handle")
        if handle not in self.views:
            try:
                view = self.resolver(handle, self.actor) if self.resolver else None
            except Exception:  # noqa: BLE001 - any resolver failure fails closed
                view = None
            self.views[handle] = view if isinstance(view, Mapping) else None
        view = self.views[handle]
        if view is None:
            return False
        path = str(citation.get("field_path"))
        for candidate in (path, "record." + path.split(".", 1)[-1]):
            try:
                raw = _field(view, candidate)
            except Exception:  # noqa: BLE001 - a path the view does not carry
                continue
            return _number(raw) is not None and _number(raw) == _number(entry.get("value"))
        return False


def order_value(members: Sequence[str], amounts: Mapping[str, Sequence[Mapping[str, Any]]],
                currencies: Mapping[str, Sequence[Mapping[str, Any]]],
                same: Callable[[Mapping[str, Any]], bool]
                ) -> tuple[Decimal | None, str | None, list[Mapping[str, Any]], str | None]:
    """(amount, currency, the entries it rests on, why none) of one order cluster."""
    from ontology.units import ISO_4217
    found: list[tuple[Decimal, str | None, Mapping[str, Any]]] = []
    for member in members:
        for entry in amounts.get(member, ()):
            amount = _number(entry.get("value"))
            if amount is None or not same(entry):
                return None, None, [], "order_value_not_shown"
            currency = entry.get("currency")
            if not currency:
                said = {str(c.get("value")).strip().upper() for c in currencies.get(member, ())
                        if isinstance(c.get("value"), str) and c["value"].strip()}
                currency = said.pop() if len(said) == 1 else None
            found.append((amount, currency, entry))
    if not found:
        return None, None, [], "order_value_not_shown"
    if len({a for a, _c, _e in found}) > 1 or len({c for _a, c, _e in found}) > 1:
        return None, None, [], "order_values_disagree"
    amount, currency, _entry = found[0]
    if currency not in ISO_4217:
        return None, None, [], "order_currency_not_stated"
    return amount, currency, [e for _a, _c, e in found], None


# --------------------------------------------------------------------------- margin at stake

@dataclass(frozen=True)
class Figure:
    """One cited value a margin rests on: its number, ISO 4217 currency (money) or unit,
    and the reference that cites it (an answer entry name or an l1 handle)."""
    amount: Decimal
    currency: str | None
    unit: str | None
    ref: str
    object_id: str = ""
    markings: str = "[]"
    # a landed-cost engine figure: the l1 handles of the cited values it was computed from
    refs: tuple[str, ...] = ()


@dataclass
class Margin:
    """The margin at stake on one order cluster (None amount: not stated, ``why`` says
    why), the per-line arithmetic and the figures it rests on."""
    amount: Decimal | None
    currency: str | None
    why: str | None
    steps: list[str] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    lines: int = 0
    bases: tuple[str, ...] = ()   # the cost bases its lines rest on (``COST_BASES``)

    @property
    def basis(self) -> str:
        return basis_label(self.bases)


def figure(raw: Mapping[str, Any], ref: str, *, money: bool) -> Figure | None:
    """A value as a ``Figure``: a Decimal from its text (a float is not cited money), a
    money value only with an ISO 4217 currency; None otherwise."""
    from ontology.units import ISO_4217
    amount = _number(raw.get("value"))
    if amount is None:
        return None
    currency = raw.get("currency")
    currency = currency.strip().upper() if isinstance(currency, str) and currency.strip() \
        else None
    if money and currency not in ISO_4217:
        return None
    unit = raw.get("unit") if isinstance(raw.get("unit"), str) and raw["unit"].strip() \
        else None
    return Figure(amount, currency, unit, ref, str(raw.get("object_id") or ""),
                  str(raw.get("markings") or "[]"))


def _one(found: Sequence[Figure]) -> Figure | None | bool:
    """The one figure the records state (False: they disagree; None: none)."""
    if not found:
        return None
    from ontology.units import unit_token
    said = {(f.amount, f.currency, unit_token(f.unit) or f.unit) for f in found}
    return found[0] if len(said) == 1 else False


def _cost_of(item: str, graph: Graph, costs: Mapping[str, Mapping[str, Sequence[Figure]]],
             seen_any: Container[str]) -> tuple[Figure | None, str | None, str | None]:
    """(unit cost, its basis, why none) of one sold item, over the item and its recorded
    twins: the landed-cost engine's per-unit total (``LANDED_ACTUAL``, else
    ``LANDED_ESTIMATE``) when it gives one, else the first ``COST_PROPS`` property
    (``RECORDED_COST``); only a positive cost per a unit in an ISO currency. Figures of one
    basis that disagree give none, and never fall back to another basis."""
    members = graph.cluster(ITEM_TWINS, item)
    for prop in (LANDED_ACTUAL, LANDED_ESTIMATE, *COST_PROPS):
        found = [f for m in members for f in costs.get(m, {}).get(prop, ())]
        usable = [f for f in found if f.unit is not None and f.currency is not None]
        one = _one(usable)
        basis = prop if prop in COST_BASES else RECORDED_COST
        if one is False:
            return None, basis, ("landed_costs_disagree" if basis != RECORDED_COST
                                 else "landed_cost_twins_disagree")
        if one is not None:
            if one.amount <= 0:
                return None, basis, "landed_cost_not_positive"   # an unpriced item
            return one, basis, None
    if any(m in seen_any for m in members):
        return None, None, "landed_cost_unit_or_currency_not_stated"
    return None, None, "landed_cost_not_shown"


def order_margin(members: Sequence[str], graph: Graph,
                 lines: Mapping[str, Mapping[str, Sequence[Figure]]],
                 costs: Mapping[str, Mapping[str, Sequence[Figure]]],
                 seen_cost: Container[str] = ()) -> Margin:
    """Margin at stake of one order cluster: sales price minus landed cost, only where both
    are known (module doc). Per order record of the cluster with recorded lines: each
    line's cited amount (``SalesOrderLine.line_total``, excluding tax) minus its ordered
    quantity times its item's cited unit cost (``COST_PROPS``, over the item's twins); the
    quantity's unit must be the cost's unit and the amount's currency the cost's (nothing is
    converted). A record states a margin only when every one of its lines does; twins that
    state different margins give none. ``lines``: line id -> property -> figures (only the
    values this reader was shown); ``costs``: item id -> property -> figures."""
    from ontology.units import unit_token
    found: list[Margin] = []
    why: str | None = None
    for member in members:
        member_lines = sorted({sol for sol, _e in graph.rev(ORDER_LINE, member)})
        if not member_lines:
            continue
        total, currency, steps, figures, broke = Decimal(0), None, [], [], None
        bases: set[str] = set()
        for sol in member_lines:
            values = lines.get(sol, {})
            revenue = _one(values.get(REVENUE_PROP, ()))
            quantity = _one(values.get(QUANTITY_PROP, ()))
            items = sorted({i for i, _e in graph.fwd(LINE_SELLS, sol)})
            if revenue is False or quantity is False:
                broke = "line_values_disagree"
            elif revenue is None or quantity is None:
                broke = "line_amount_not_shown"
            elif len(items) != 1:
                broke = ("line_item_not_recorded" if not items else "line_items_disagree")
            else:
                cost, basis, cost_why = _cost_of(items[0], graph, costs, seen_cost)
                if cost is None:
                    broke = cost_why
                elif (unit_token(quantity.unit) or quantity.unit) is None or \
                        (unit_token(quantity.unit) or quantity.unit) != \
                        (unit_token(cost.unit) or cost.unit):
                    broke = "cost_unit_differs_from_quantity_unit"
                elif revenue.currency != cost.currency or (
                        currency is not None and revenue.currency != currency):
                    broke = "margin_currency_differs"
                else:
                    currency = revenue.currency
                    line = revenue.amount - quantity.amount * cost.amount
                    total += line
                    figures += [revenue, quantity, cost]
                    bases.add(basis)
                    steps.append(
                        f"{sol}: {_dec_str(revenue.amount)} {currency} sales amount - "
                        f"{_dec_str(quantity.amount)} {quantity.unit} x {_dec_str(cost.amount)} "
                        f"{cost.currency}/{cost.unit} {COST_BASES[basis]} (of {items[0]}) = "
                        f"{_dec_str(line)} {currency}")
            if broke:
                break
        if broke:
            why = why or broke
            continue
        found.append(Margin(total, currency, None, steps, figures, len(member_lines),
                            tuple(sorted(bases))))
    if not found:
        return Margin(None, None, why or "order_lines_not_recorded")
    if len({(m.amount, m.currency, m.bases) for m in found}) > 1:
        return Margin(None, None, "order_margins_disagree")
    return found[0]


CHAIN_STEP = "chain from {po} to {order}"   # first step of each order's entry (plan_run)


def ranked_orders(answer: Sequence[Mapping[str, Any]], name: str = "impact"
                  ) -> list[tuple[str, str]]:
    """(late PO, customer order) of each order entry of a ``plan_run`` answer, in its rank
    (worst first: ``<name>_1``, ``<name>_2``, ...), read from the entry's own first step
    (``CHAIN_STEP``). An entry whose first step is not that wording is skipped, never
    guessed."""
    found: list[tuple[int, str, str]] = []
    pattern = re.compile(rf"^{re.escape(name)}_([0-9]+)$")
    head = re.compile(r"^chain from (\S+) to (\S+)$")
    for entry in answer:
        if not isinstance(entry, Mapping) or entry.get("kind") != "computed":
            continue
        rank = pattern.match(str(entry.get("name")))
        steps = entry.get("steps") or ()
        said = head.match(steps[0]) if rank and steps and isinstance(steps[0], str) else None
        if said:
            found.append((int(rank.group(1)), said.group(1), said.group(2)))
    return [(po, order) for _n, po, order in sorted(found)]


def posted_company(edge: Edge, objects: Mapping[str, Mapping[str, Any]] | None) -> str:
    """The company an entity-map link (``PO_POSTED``, ``ITEM_POSTED``) names, as its step
    reads it: the BC end's capture lineage (``bc_<book>``) from the reader's own object
    reference; '' for any other link. The link joins on the BC systemId, so the BC end is one
    record of one company; when the reader was not shown that reference the step says so."""
    if edge.link_type not in (PO_POSTED, ITEM_POSTED):
        return ""
    lineage = ((objects or {}).get(edge.to) or {}).get("lineage")
    if isinstance(lineage, str) and lineage.strip():
        return (f"; Portal entity map, joined on the BC systemId: the BC record of company "
                f"{lineage.strip()}")
    return ("; Portal entity map, joined on the BC systemId: one BC record of one company "
            "(the company is not shown to this reader)")


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Any, actor: Any, complete: bool,
             covers: Callable[[str], bool],
             objects: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The customer orders the plan's late POs put at risk (module doc). ``objects``: the
    run's object references (each with its ``lineage``), so a step crossing the Portal
    entity map names the BC company of its BC end (``posted_company``)."""
    graph = Graph((Edge(str(e.get("link_id")), str(e.get("link_type")), str(e.get("from")),
                        str(e.get("to")), str(e.get("tier"))) for e in edges
                   if isinstance(e, Mapping)), LINK_TYPES + (LINE_SELLS,))
    late = _plan_late(args, evidence)

    def by_object(item: str) -> dict[str, list[Mapping[str, Any]]]:
        out: dict[str, list[Mapping[str, Any]]] = {}
        for entry in evidence.get(args[item], ()):
            if entry.get("kind") == "property" and entry.get("value") is not None:
                out.setdefault(str(entry.get("object_id")), []).append(entry)
        return out
    required_entries = by_object("required")
    required = {oid: [(d, e["name"]) for e in got if (d := _day(e.get("value"))) is not None]
                for oid, got in required_entries.items()}
    statuses = {oid: [e.get("value") for e in got]
                for oid, got in by_object("order_status").items()}
    ao_statuses = {oid: [e.get("value") for e in got]
                   for oid, got in by_object("assembly_status").items()}
    found = assess(late, graph, required=required, order_status=statuses,
                   assembly_status=ao_statuses)
    gaps: list[dict[str, Any]] = []
    reasons: list[str] = []
    grouped: dict[str, list[Break]] = {}
    for brk in found.breaks:
        grouped.setdefault(brk.reason, []).append(brk)
    for reason in sorted(grouped):
        gaps.append(break_gap(grouped, reason, complete=complete and not found.cut,
                              covers=covers))
    if found.cut:
        gaps.append({"state": "unknown", "reason": "impact_walk_capped", "subject": name,
                     "detail": f"impact_walk_capped: more than {MAX_CHAINS} chains or "
                               f"{MAX_TWINS} twins from one record; the rest were not walked"})
        reasons.append("impact_walk_capped")
    if not late:
        gaps.append({"state": "unknown", "reason": "no_lateness_verdict", "subject": name,
                     "detail": "no_lateness_verdict: plx.process gave no lateness verdict this "
                               "reader can see for the purchase orders reached"})
        reasons.append("no_lateness_verdict")
    if found.undetermined:
        reasons.append("lateness_undetermined")
    reread = _Reread(resolver, actor)
    amounts, currencies = by_object("value"), by_object("currency")
    entries: list[dict[str, Any]] = []
    shown = 0
    for n, order in enumerate(found.orders, start=1):
        chain = next((c for c in order.chains if c.po == order.worst), order.chains[0])
        verdict = late[chain.po]
        steps = [CHAIN_STEP.format(po=chain.po, order=order.key)]
        steps += [f"crossed {e.cited()}{posted_company(e, objects)}" for e in chain.edges]
        inputs = list(verdict.inputs) + [ref for ref in order.required_from][:20]
        if order.days is None:
            steps.append(f"days at risk not stated: {order.why_null}")
            gaps.append({"state": "unknown", "reason": order.why_null, "subject": order.key,
                         "detail": f"{order.why_null}: the order's required date is "
                                   + ("not recorded or not shown to this reader"
                                      if order.why_null.endswith("not_recorded")
                                      else "stated differently by its records")})
        else:
            steps.append(f"material date {order.material.isoformat()} ({order.material_how}) "
                         f"+ 0 days recorded lead time - required "
                         f"{order.required.isoformat()} = {order.days} days (days at "
                         "risk is a lower bound)")
            steps.append("at risk" if order.at_risk else "not at risk: ready on or before "
                         "the required date")
        if len(order.chains) > 1:
            steps.append(f"fed by {len(order.chains)} late PO chain(s): "
                         + ", ".join(sorted({c.po for c in order.chains}))[:300])
        basis = list(dict.fromkeys([*chain.records, *order.members]))[:100]
        entries.append(_digest({
            "name": f"{name}_{n}"[:64], "kind": "computed", "value": order.days,
            "inputs": inputs[:50], "steps": [s[:MAX_STEP_CHARS] for s in steps][:40],
            "basis": basis, "check": "not_applicable"}))
        if order.at_risk:
            amount, currency, used, why = order_value(order.members, amounts, currencies,
                                                      reread.same)
            if amount is not None:
                shown += 1
                entries.append(_digest({
                    "name": f"{name}_value_{n}"[:64], "kind": "computed",
                    "value": _dec_str(amount), "currency": currency,
                    "inputs": [e["name"] for e in used][:50],
                    "steps": [f"value at risk of {order.key}: its cited order total "
                              f"{_dec_str(amount)} {currency}, as the source states it (tax "
                              "basis per source)"],
                    "basis": order.members[:100], "check": "not_applicable"}))
            elif why == "order_values_disagree":
                gaps.append({"state": "unknown", "reason": why, "subject": order.key,
                             "detail": f"{why}: the order's twins state different totals or "
                                       "currencies, so no value at risk is stated"})
    risky = found.at_risk
    if risky and set(MARGIN_ARG_KEYS) <= set(args):
        entries += _plan_margins(args, evidence, graph, risky, reread, name, gaps,
                                 {o.key: n for n, o in enumerate(found.orders, start=1)})
    if risky:
        gaps.append(lead_time_gap(name))
    unshown = len(risky) - shown
    if unshown:
        gaps.append({"state": "unknown", "reason": "value_at_risk_not_stated", "subject": name,
                     "detail": f"value_at_risk_not_stated: {unshown} order(s) at risk have no "
                               "value this reader was shown and could re-read"})
    lates = sorted({o.worst for o in risky if o.worst})
    entries.append(_digest({
        "name": name, "kind": "computed", "value": len(risky),
        "inputs": [e["name"] for e in entries][:50],
        "steps": [f"{len(risky)} customer orders at risk from {len(lates)} late POs, worst "
                  "first",
                  f"{len(found.considered)} late or slipping PO(s) walked, {found.chains} "
                  f"chain(s) found, {len(found.breaks)} stop at a missing link",
                  DAYS_AT_RISK_BASIS]
        + ([] if complete else ["the run was cut, so other orders may be at risk"]),
        "basis": [o.key for o in risky][:100], "check": "not_applicable"}))
    return {"entries": entries, "gaps": gaps[:50], "reasons": reasons}


def _margin_figures(entries: Sequence[Mapping[str, Any]], same: Callable[[Mapping[str, Any]], bool],
                    money: bool) -> tuple[dict[str, dict[str, list[Figure]]], set[str]]:
    """object id -> property -> the figures of these answer entries the source still
    states for the reader (re-read), and the objects any entry was shown for."""
    out: dict[str, dict[str, list[Figure]]] = {}
    seen: set[str] = set()
    for entry in entries:
        if entry.get("kind") != "property" or entry.get("value") is None:
            continue
        oid = str(entry.get("object_id"))
        seen.add(oid)
        if not same(entry):
            continue
        got = figure(entry, str(entry.get("name")), money=money)
        if got is not None:
            out.setdefault(oid, {}).setdefault(str(entry.get("property")), []).append(got)
    return out, seen


def _plan_margins(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                  graph: Graph, risky: Sequence[Order], reread: "_Reread", name: str,
                  gaps: list[dict[str, Any]], index: Mapping[str, int]) -> list[dict[str, Any]]:
    """The margin entry of each order at risk whose margin this reader was shown (finance
    values, re-read), a total when one currency, and one gap for the rest: specific only
    where the reader was shown the values it rests on, else it never says whether one
    exists."""
    amounts, _ = _margin_figures(evidence.get(args["line_amount"], ()), reread.same, True)
    quantities, _ = _margin_figures(evidence.get(args["line_quantity"], ()), reread.same, False)
    lines: dict[str, dict[str, list[Figure]]] = {}
    for table in (amounts, quantities):
        for oid, props in table.items():
            for prop, found in props.items():
                lines.setdefault(oid, {}).setdefault(prop, []).extend(found)
    costs, seen_cost = _margin_figures(evidence.get(args["item_cost"], ()), reread.same, True)
    entries: list[dict[str, Any]] = []
    totals: dict[str, Decimal] = {}
    bases: set[str] = set()
    why: dict[str, list[str]] = {}
    for order in risky:
        margin = order_margin(order.members, graph, lines, costs, seen_cost)
        if margin.amount is None:
            why.setdefault(margin.why or "margin_not_stated", []).append(order.key)
            continue
        totals[margin.currency] = totals.get(margin.currency, Decimal(0)) + margin.amount
        bases.update(margin.bases)
        entries.append(_digest({
            "name": f"{name}_margin_{index[order.key]}"[:64], "kind": "computed",
            "value": _dec_str(margin.amount), "currency": margin.currency,
            "inputs": list(dict.fromkeys(f.ref for f in margin.figures))[:50],
            "steps": [s[:MAX_STEP_CHARS] for s in [
                f"margin at stake on {order.key} over its {margin.lines} line(s)",
                *margin.steps, f"= {_dec_str(margin.amount)} {margin.currency}",
                f"cost basis: {margin.basis}", MARGIN_BASIS]][:40],
            "basis": order.members[:100], "check": "not_applicable"}))
    if len(totals) == 1 and entries:
        (currency, total), = totals.items()
        entries.append(_digest({
            "name": f"{name}_margin"[:64], "kind": "computed", "value": _dec_str(total),
            "currency": currency, "inputs": [e["name"] for e in entries][:50],
            "steps": [x[:MAX_STEP_CHARS] for x in (
                f"margin at stake on {len(entries)} of {len(risky)} order(s) at risk: "
                f"{_dec_str(total)} {currency}", f"cost basis: {basis_label(bases)}",
                MARGIN_BASIS)],
            "basis": [o.key for o in risky][:100], "check": "not_applicable"}))
    elif len(totals) > 1:
        gaps.append({"state": "unknown", "reason": "margin_currencies_differ", "subject": name,
                     "detail": "margin_currencies_differ: the orders' margins are in "
                               f"{len(totals)} currencies; no total is stated (nothing is "
                               "converted)"})
    for reason in sorted(why):
        subjects = sorted(why[reason])
        gaps.append({"state": "unknown", "reason": reason,
                     "subject": (", ".join(subjects[:MAX_GAP_SUBJECTS])
                                 + (f" and {len(subjects) - MAX_GAP_SUBJECTS} more"
                                    if len(subjects) > MAX_GAP_SUBJECTS else ""))[:700],
                     "detail": (f"{reason}: no margin at stake is stated for "
                                f"{len(subjects)} order(s) at risk; "
                                + MARGIN_WHY.get(reason, "see the reason"))[:600]})
    return entries


PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run}}


# --------------------------------------------------------------------------- store scan

def _store_edges(st: Any) -> list[Edge]:
    return [Edge(str(row.get("link_id") or f"{row['link_type']}|{row['from_object']}|"
                                           f"{row['to_object']}"),
                 row["link_type"], row["from_object"], row["to_object"], row["tier"],
                 row.get("markings") or "[]")
            for row in st.links(LINK_TYPES + (LINE_SELLS,), FIRM_TIERS + LEAD_TIERS)]


def _verdict(po: str, answer: Any) -> Late:
    """The lateness entry of ``po`` in a ``plx.process`` answer; undetermined otherwise."""
    entries = answer.get("lateness") if isinstance(answer, Mapping) else None
    entry = next((e for e in entries or () if isinstance(e, Mapping)
                  and e.get("root") == po), None)
    if entry is None or "error" in (answer or {}):
        return Late(po, "undetermined")
    cites = [v for v in entry.get("values") or ()
             if isinstance(v, Mapping) and isinstance(v.get("citation"), Mapping)]
    state = entry.get("state") if isinstance(entry.get("state"), str) else None
    return Late(po, state, _day(entry.get("governing_date")), _day(entry.get("measured_on")),
                _day(entry.get("revised_date")), _day(entry.get("received_date")),
                at_risk=entry["at_risk"] if isinstance(entry.get("at_risk"), bool) else None,
                promised=_day(entry.get("promised_date")), cites=cites)


def scan(st: Any, call: Callable[[str, Mapping[str, Any]], Any], *, bound: int = 500,
         value: Callable[[str], Any] | None = None, samples: int = 3,
         visible: Container[str] | None = None,
         margin_values: Callable[[Sequence[str]], Mapping[str, Sequence[Mapping[str, Any]]]]
         | None = None,
         landed_values: Callable[[Sequence[str], "Graph"], Mapping[str, Sequence[Mapping[str, Any]]]]
         | None = None) -> dict[str, Any]:
    """Every late PO's impact over the projection store (``st``: a
    ``proactive_engine._Store``): links set-based, the lateness verdict asked of
    ``plx.process`` (``call``) only for POs whose recorded links reach a consuming assembly
    component (a chain to a customer order, or a typed break past it), at most ``bound`` of
    them. ``value(order_id)``: the reader's ``plx.object`` answer of an
    order, for the value at risk (None: no values are read). ``visible``: the object ids
    a restricted reader may read (None: the whole store, the engine's own run); a link with
    an end outside it is not crossed (the chain stops at a typed break) and a date or
    status of an object outside it is not read. ``margin_values(object_ids)``: object id ->
    the values of those order lines and items this reader may read (``store_margin_values``
    for the engine, ``object_margin_values`` for a reader's ``plx.object``; None: no margin
    is computed); per late PO, ``po_impacts`` lists the orders at risk it feeds and, where
    known, their margin at stake (``order_margin``). ``landed_values(item_ids, graph)``:
    item id -> the landed-cost engine's per-unit totals of those sold items
    (``investigate_landed_values``; None: only the recorded unit cost is used)."""
    graph = Graph((e for e in _store_edges(st)
                   if visible is None or (e.frm in visible and e.to in visible)),
                  LINK_TYPES + (LINE_SELLS,))
    pos = sorted({e.to for es in graph.inn.values() for e in es if e.link_type == PO_LINE})
    roots: list[str] = []
    seen: set[str] = set()
    unconsumed = 0
    stopped: dict[str, set[str]] = {}
    for po in pos:
        cluster = graph.cluster(PO_TWINS, po)
        if seen.intersection(cluster):
            continue
        seen.update(cluster)
        root = cluster[0] if _type_of(cluster[0]) == "PurchaseOrder" else po
        chains, breaks, _cut = walk(root, graph)
        if chains or any(b.reason in REACHES_ASSEMBLY for b in breaks):
            roots.append(root)
        else:
            unconsumed += 1
            for brk in breaks:
                stopped.setdefault(brk.reason, set()).add(root)
    late: dict[str, Late] = {}
    for po in roots[:bound]:
        try:
            late[po] = _verdict(po, call("plx.process", {"from": po,
                                                         "process": "po_delivery_window"}))
        except Exception:  # noqa: BLE001 - one unread verdict is undetermined, never late
            late[po] = Late(po, "undetermined")
    sales = {oid: entry for oid, entry in st.values(
        ("SalesOrder",), ("required_date", "status", "fm_status", "bc_status")).items()
        if visible is None or oid in visible}
    aos = {oid: entry for oid, entry in st.values(("AssemblyOrder",), ("status",)).items()
           if visible is None or oid in visible}
    required = {oid: [(d, row["l1_handle"]) for row in entry["props"].get("required_date", ())
                      if (d := _day(row.get("value"))) is not None]
                for oid, entry in sales.items()}
    statuses = {oid: [row.get("value") for p in ("status", "fm_status", "bc_status")
                      for row in entry["props"].get(p, ())] for oid, entry in sales.items()}
    ao_statuses = {oid: [row.get("value") for row in entry["props"].get("status", ())]
                   for oid, entry in aos.items()}
    found = assess(late, graph, required=required, order_status=statuses,
                   assembly_status=ao_statuses)
    by_reason: dict[str, int] = {}
    for brk in found.breaks:
        by_reason[brk.reason] = by_reason.get(brk.reason, 0) + 1
        stopped.setdefault(brk.reason, set()).add(brk.po)
    if not pos:
        stopped["po_line_not_recorded"] = set()
    elif roots and not found.considered and not found.undetermined:
        stopped[NO_LATE_PO] = set(roots)
    totals: dict[str, Decimal] = {}
    with_value = 0
    if value is not None:
        for order in found.at_risk:
            amounts: dict[str, list[Mapping[str, Any]]] = {}
            currencies: dict[str, list[Mapping[str, Any]]] = {}
            for member in order.members:
                answer = value(member)
                for v in (answer or {}).get("values") or () if isinstance(answer, Mapping) \
                        else ():
                    if not isinstance(v, Mapping) or v.get("value") is None:
                        continue
                    named = {"total_amount": amounts, "currency": currencies}.get(
                        v.get("property"))
                    if named is not None:
                        named.setdefault(member, []).append(v)
            amount, currency, _used, _why = order_value(order.members, amounts, currencies,
                                                        lambda _e: True)
            if amount is not None:
                with_value += 1
                totals[currency] = totals.get(currency, Decimal(0)) + amount
    margins: dict[str, Margin] = {}
    margin_why: dict[str, int] = {}
    if margin_values is not None and found.at_risk:
        margins = scan_margins(found.at_risk, graph, margin_values, landed_values)
        for margin in margins.values():
            if margin.amount is None:
                margin_why[margin.why or "margin_not_stated"] = \
                    margin_why.get(margin.why or "margin_not_stated", 0) + 1
    impacts = po_impacts(found, margins)
    margin_totals: dict[str, Decimal] = {}
    for margin in margins.values():
        if margin.amount is not None:
            margin_totals[margin.currency] = margin_totals.get(margin.currency, Decimal(0)) \
                + margin.amount
    return {"assessment": found, "graph": graph, "late": late,
            "margins": margins, "po_impacts": impacts,
            "orders_with_margin": sum(1 for m in margins.values() if m.amount is not None),
            "margin_at_stake": {c: _dec_str(v) for c, v in sorted(margin_totals.items())},
            "margin_not_stated_by_reason": dict(sorted(margin_why.items())),
            "late_pos_with_full_chain": sum(1 for i in impacts if i["chain"] == "full"),
            "late_pos_with_partial_chain": sum(1 for i in impacts if i["chain"] == "partial"),
            "late_pos_considered": len(found.considered),
            "late_pos_with_orders_at_risk": len({o.worst for o in found.at_risk}),
            "pos_checked": len(late), "pos_not_checked": max(0, len(roots) - bound),
            "pos_not_reaching_assembly": unconsumed,
            "lateness_undetermined": len(found.undetermined),
            "chains_found": found.chains, "chains_broken": len(found.breaks),
            "broken_share_percent": found.broken_share,
            "broken_by_reason": dict(sorted(by_reason.items())),
            "top_missing_links": [r for r, _n in sorted(by_reason.items(),
                                                         key=lambda p: (-p[1], p[0]))][:3],
            "orders_affected": len(found.orders), "orders_at_risk": len(found.at_risk),
            "orders_excluded": dict(found.excluded),
            "orders_with_value": with_value,
            "value_at_risk": {c: _dec_str(v) for c, v in sorted(totals.items())},
            "missing_links": missing_links(stopped),
            "days_at_risk_basis": DAYS_AT_RISK_BASIS,
            "samples": [sample(o) for o in found.at_risk[:samples]],
            "truncated": found.cut or len(roots) > bound}


def scan_margins(orders: Sequence[Order], graph: Graph,
                 margin_values: Callable[[Sequence[str]], Mapping[str, Sequence[Mapping[str, Any]]]],
                 landed_values: Callable[[Sequence[str], Graph],
                                         Mapping[str, Sequence[Mapping[str, Any]]]] | None = None
                 ) -> dict[str, Margin]:
    """The margin at stake of each order (``order_margin``) over the values
    ``margin_values`` gives for its lines and their items (and the items' twins), and the
    landed-cost engine's per-unit totals ``landed_values`` gives for those items: each
    ``{value, unit "CUR/unit", basis actual|estimate, refs}``; one without a per-unit unit,
    an ISO currency or a cited input is not used."""
    lines = sorted({sol for o in orders for m in o.members for sol, _e in graph.rev(ORDER_LINE, m)})
    items = sorted({t for sol in lines for i, _e in graph.fwd(LINE_SELLS, sol)
                    for t in graph.cluster(ITEM_TWINS, i)})
    read = margin_values([*lines, *items]) if lines else {}
    line_figs: dict[str, dict[str, list[Figure]]] = {}
    cost_figs: dict[str, dict[str, list[Figure]]] = {}
    seen_cost: set[str] = set()
    for oid in [*lines, *items]:
        for raw in read.get(oid, ()) if isinstance(read, Mapping) else ():
            if not isinstance(raw, Mapping) or raw.get("value") is None:
                continue
            prop = raw.get("property")
            is_line = oid in lines and prop in (REVENUE_PROP, QUANTITY_PROP)
            if not is_line and not (oid in items and prop in COST_PROPS):
                continue
            if not is_line:
                seen_cost.add(oid)
            got = figure({**raw, "object_id": oid}, str(raw.get("ref") or raw.get("l1_handle")),
                         money=prop != QUANTITY_PROP)
            if got is not None:
                (line_figs if is_line else cost_figs).setdefault(oid, {}).setdefault(
                    prop, []).append(got)
    landed = landed_values(items, graph) if landed_values is not None and items else {}
    for oid in items:
        for raw in landed.get(oid, ()) if isinstance(landed, Mapping) else ():
            got = landed_figure(raw, oid)
            if got is not None:
                prop = LANDED_ACTUAL if raw.get("basis") == "actual" else LANDED_ESTIMATE
                cost_figs.setdefault(oid, {}).setdefault(prop, []).append(got)
                seen_cost.add(oid)
    return {o.key: order_margin(o.members, graph, line_figs, cost_figs, seen_cost)
            for o in orders}


def landed_figure(raw: Any, oid: str) -> Figure | None:
    """A landed-cost engine per-unit total as a ``Figure`` (unit ``CUR/unit`` split into
    currency and unit), or None: not a mapping, basis not actual or estimate, no positive
    Decimal, no ISO currency or per-unit unit, or no cited input (``refs``)."""
    from ontology.units import ISO_4217
    if not isinstance(raw, Mapping) or raw.get("basis") not in ("actual", "estimate"):
        return None
    unit = raw.get("unit")
    if not isinstance(unit, str) or unit.count("/") != 1:
        return None
    currency, per = (part.strip() for part in unit.split("/"))
    refs = tuple(r for r in raw.get("refs") or () if isinstance(r, str) and r)
    amount = _number(raw.get("value"))
    if amount is None or currency.upper() not in ISO_4217 or not per or not refs:
        return None
    return Figure(amount, currency.upper(), per, str(raw.get("ref") or refs[0]), oid,
                  '["finance"]', refs)


def landed_from_answer(answer: Any, lines: Container[str]) -> dict[str, list[dict[str, Any]]]:
    """PO line id -> the per-unit landed costs a ``landed_cost`` plan answer
    (``plx.investigate`` family ``LANDED_FAMILY``) states for those lines: each line entry
    (``<name>_line_<n>``: ``actual`` unless a step calls it an estimate; ``<name>_estimate_
    line_<n>``: ``estimate``) with a value, a ``CUR/unit`` unit, exactly one of ``lines`` in
    its basis, and the l1 handles of the cited inputs it rests on (``refs``)."""
    entries = [e for e in (answer.get("answer") or ()) if isinstance(e, Mapping)] \
        if isinstance(answer, Mapping) and "error" not in answer else []
    by_name = {str(e.get("name")): e for e in entries}
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        match = _LANDED_LINE.search(str(entry.get("name") or ""))
        if not match or entry.get("kind") != "computed" or entry.get("value") is None:
            continue
        own = [b for b in entry.get("basis") or () if b in lines]
        if len(own) != 1:
            continue
        refs = []
        for name in entry.get("inputs") or ():
            cited = by_name.get(name, {}).get("citation")
            if isinstance(cited, Mapping) and isinstance(cited.get("l1_handle"), str):
                refs.append(cited["l1_handle"])
        estimate = bool(match.group(1)) or any(
            "estimate" in str(step).lower() for step in entry.get("steps") or ())
        out.setdefault(own[0], []).append({
            "value": entry["value"], "unit": entry.get("unit"), "ref": entry.get("name"),
            "refs": list(dict.fromkeys(refs)), "basis": "estimate" if estimate else "actual"})
    return out


def investigate_landed_values(call: Callable[[str, Mapping[str, Any]], Any], *,
                              bound: int = MAX_LANDED_POS
                              ) -> Callable[[Sequence[str], Graph],
                                            dict[str, list[dict[str, Any]]]]:
    """``landed_values`` through ``plx.investigate`` (``call``: the reader's tool caller):
    for the purchase order lines that buy one of the sold items (``PurchaseOrderLine.
    for_item``), the landed-cost plan of their purchase orders (at most ``bound``, in id
    order), item id -> each line's per-unit landed cost (``landed_from_answer``). A failed or
    refused answer gives none, so the recorded unit cost is used and labelled as such."""
    def read(item_ids: Sequence[str], graph: Graph) -> dict[str, list[dict[str, Any]]]:
        bought: dict[str, str] = {}
        for item in item_ids:
            for pol, _e in graph.rev(LINE_ITEM, item):
                bought.setdefault(pol, item)
        pos: dict[str, set[str]] = {}
        for pol in bought:
            for po, _e in graph.fwd(PO_LINE, pol):
                pos.setdefault(po, set()).add(pol)
        out: dict[str, list[dict[str, Any]]] = {}
        for po in sorted(pos)[:bound]:
            try:
                answer = call("plx.investigate", {"family": LANDED_FAMILY, "root": po})
            except Exception:  # noqa: BLE001 - a budget or tool failure states nothing
                continue
            for pol, found in landed_from_answer(answer, pos[po]).items():
                out.setdefault(bought[pol], []).extend(found)
        return out
    return read


def po_impacts(found: Assessment, margins: Mapping[str, Margin]) -> list[dict[str, Any]]:
    """Per late or slipping PO walked: the orders at risk it feeds (``orders``), whether its
    walk reached a customer order with no link missing (``chain``: full), reached one with
    another chain stopping (partial) or reached none (none), the breaks by reason and,
    where every such order's margin is stated in one currency, the margin at stake."""
    broke: dict[str, dict[str, int]] = {}
    for brk in found.breaks:
        broke.setdefault(brk.po, {})
        broke[brk.po][brk.reason] = broke[brk.po].get(brk.reason, 0) + 1
    out = []
    for po in found.considered:
        orders = [o for o in found.at_risk if any(c.po == po for c in o.chains)]
        affected = [o for o in found.orders if any(c.po == po for c in o.chains)]
        stated = [margins[o.key] for o in orders
                  if o.key in margins and margins[o.key].amount is not None]
        bases = {b for m in stated for b in m.bases}
        currencies = {m.currency for m in stated}
        total = (sum((m.amount for m in stated), Decimal(0))
                 if len(currencies) == 1 else None)
        out.append({"po": po, "orders": [o.key for o in orders],
                    "chain": ("none" if not affected else
                              "partial" if broke.get(po) else "full"),
                    "breaks": dict(sorted(broke.get(po, {}).items())),
                    "orders_with_margin": len(stated),
                    "margin": ({"amount": _dec_str(total), "currency": currencies.pop(),
                                "basis": basis_label(bases)}
                               if total is not None else None)})
    return out


def store_margin_values(st: Any, visible: Container[str] | None = None
                        ) -> Callable[[Sequence[str]], dict[str, list[dict[str, Any]]]]:
    """``margin_values`` over the projection store (the engine's own read): each value with
    its unit, currency, l1 handle (``ref``) and markings."""
    def read(object_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        ids = [o for o in object_ids if visible is None or o in visible]
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(ids), 500):
            rows = st.conn.execute(
                "SELECT v.object_id, v.property, v.value, v.unit, v.currency, v.l1_handle, "
                "v.markings FROM object_values v WHERE v.superseded_at IS NULL "
                "AND v.object_id IN (SELECT value FROM json_each(?)) "
                "AND v.property IN (SELECT value FROM json_each(?)) "
                "ORDER BY v.object_id, v.property, v.l1_handle",
                (json.dumps(ids[i:i + 500]),
                 json.dumps([REVENUE_PROP, QUANTITY_PROP, *COST_PROPS])))
            for row in rows:
                out.setdefault(row[0], []).append(
                    {"property": row[1], "value": row[2], "unit": row[3], "currency": row[4],
                     "ref": row[5], "l1_handle": row[5], "markings": row[6]})
        return out
    return read


def object_margin_values(value: Callable[[str], Any]
                         ) -> Callable[[Sequence[str]], dict[str, list[dict[str, Any]]]]:
    """``margin_values`` over a reader's ``plx.object`` answers (``value(object_id)``): only
    the values that reader is shown, each with its citation handle."""
    def read(object_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for oid in object_ids:
            answer = value(oid)
            for v in (answer.get("values") or ()) if isinstance(answer, Mapping) else ():
                if not isinstance(v, Mapping):
                    continue
                handle = (v.get("citation") or {}).get("l1_handle") \
                    if isinstance(v.get("citation"), Mapping) else None
                out.setdefault(oid, []).append({**v, "ref": handle, "l1_handle": handle})
        return out
    return read


def missing_links(stopped: Mapping[str, Container[str]]) -> list[dict[str, Any]]:
    """The links that stop most POs, most first (at most ``MAX_MISSING``), typed: the
    reason, the POs it stops (``pos``: distinct purchase orders; 0 when no PO has a recorded
    line at all), where it sits, the link types that would carry it and the source that
    would hold it. An unknown reason is kept by name with no source (fail closed: never
    dropped)."""
    out = []
    for reason, members in stopped.items():
        types, source = MISSING.get(reason, ((), "unknown"))
        stage = ("before_assembly" if reason in BEFORE_ASSEMBLY else
                 "lateness" if reason in ("lateness_undetermined", NO_LATE_PO) else
                 "past_assembly" if reason in MISSING else "unknown")
        out.append({"link": reason, "pos": len(members), "stage": stage,
                    "link_types": list(types), "source": source})
    out.sort(key=lambda e: (-e["pos"], e["link"]))
    return out[:MAX_MISSING]


def sample(order: Order) -> dict[str, Any]:
    """One chain as cited links (object ids, link types and link ids; never source text)."""
    chain = next((c for c in order.chains if c.po == order.worst), order.chains[0])
    return {"po": chain.po, "order": order.key, "days_at_risk": order.days,
            "links": [{"link_type": e.link_type, "from": e.frm, "to": e.to, "tier": e.tier,
                       "link_id": e.link_id} for e in chain.edges]}


def headline(orders_at_risk: int, late_pos: int) -> str:
    return f"{orders_at_risk} customer orders at risk from {late_pos} late POs, worst first"


__all__ = ["ENGINE", "PLAN_ENGINES", "BREAKS", "MISSING", "DAYS_AT_RISK_BASIS", "MARGIN_BASIS",
           "Edge", "Figure", "Graph", "Late", "Margin", "assess", "walk", "plan_check",
           "posted_company", "ranked_orders", "CHAIN_STEP", "PO_TWINS", "ITEM_TWINS",
           "plan_run", "scan", "scan_margins", "po_impacts", "store_margin_values",
           "object_margin_values", "missing_links", "headline", "order_value", "order_margin",
           "COST_BASES", "basis_label", "landed_figure", "landed_from_answer",
           "investigate_landed_values"]
