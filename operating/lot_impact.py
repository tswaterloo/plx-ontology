"""Lot impact (recall): on a quality hold or recall of a supplier lot or a production batch,
the purchase order and supplier a lot was received on and the customer orders, their
shipments and the customers the lot or batch touches, walked only over recorded firm-tier
links.

From each lot (a ``Lot`` record: one supplier lot as one receipt line received it):

    supply  Lot <- ReceiptLine.received_lot - receipt line - ReceiptLine.line_of -> receipt
            (and Lot.received_in) - Receipt.receives -> purchase order, or receipt line -
            ReceiptLine.receives_line -> PO line - PurchaseOrderLine.line_of -> purchase
            order; PurchaseOrder.ordered_from / Receipt.supplied_by -> supplier.
    demand  Lot <- AssemblyOrderComponent.consumed_lot - FileMaker assembly-order component
            - AssemblyOrderComponent.component_of -> assembly order (and its twins) -
            AssemblyOrder.for_order_line, or <- AssemblyOrderAllocation.allocates -
            allocation - AssemblyOrderAllocation.for_order_line -> order line -
            SalesOrderLine.line_of -> customer order (and its copies within two
            SalesOrder.same_record_as links: FileMaker - Portal - Sage / BC) <-
            Shipment.ships, on every copy.
    batch   Batch (and its recorded same-batch records, Batch.same_record_as) <-
            AssemblyOrder.fills_from - assembly order (then as above), <-
            BatchAllocation.allocates - allocation - BatchAllocation.for_order_line -> order
            line, and Batch.for_sales_order -> the one order its OE list names (the order as
            a whole: no touched lines, so no quantity); <- BatchSchedule.for_batch - QC
            calibration row - BatchSchedule.for_bulk_product -> the bulk product it names.
    orders  per order cluster: shipments by Shipment.ships, by a Sage shipment line
            (ShipmentLine.of_sales_order - ShipmentLine.line_of) or a Sage invoice line
            (SalesInvoiceLine.of_sales_order - SalesInvoiceLine.of_shipment); customers by
            SalesOrder.ordered_by and Shipment.shipped_to, one customer per cluster of
            firm Customer.same_record_as links (a text-joined record stays apart).

``AFTER_BUILD`` link types are declared by the schema the next build serves: the plan's
steps for them are ``if_declared`` (not run, and named in a ``link_not_served`` note, until
the served schema declares them), and a gap resting on one is never ``absent_in_source``
before then (the reader's scope cannot cover an undeclared link).

Only links at a firm tier (verified, reviewed, corroborated) are crossed. No source records
which lot a component consumed (the Portal's app_manufacturing_order_line.lot_id is not
served, upstream ask Q3): ``consumed_lot`` is a lead-tier text match on the lot number, firm
only once a review accepts it. A component matched by lot number only is a **candidate**
(its own answer entry, ``lead_basis``), never walked to an order. Where the next link is not
recorded the walk stops at a typed gap (``BREAKS``); ``absent_in_source`` only when the run
was complete and the reader's scope covers the link types, else ``unknown``.

The ``consumed_lot`` link is marked ``quality_controlled``: a reader without that marking
sees no consumption through any primitive, and the stored plan (``requires_markings``) is
refused to them outright.

Per affected customer order (an order cluster: an order and its recorded twins) the
**quantity touched** is the ordered quantity of the order lines the lot's assembly orders
are made or allocated for, summed as Decimal over the distinct lines of one system (twin
lines in two systems are not added together: they must agree, else no value), with the
unit the source states (FileMaker order lines serve none). The quantity of the lot itself in
each order is not recorded. Shipments are listed where found on the records read; where
none is, the order says so and a ``shipment_not_found`` gap (never ``absent_in_source``)
names it. That a shipment carried this lot is not recorded either.

Two drivers share the walk: ``plan_run`` (the ``plx.investigate`` engine of
``ontology/plans/lot_impact.json``, family F17) and ``scan`` (a set-based pass over the
projection store for the live check, counts only). Evidence label of fixture runs:
``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
from typing import Any, Callable, Container, Iterable, Mapping, Sequence

from operating.impact_chain import (FIRM_TIERS, LEAD_TIERS, MAX_GAP_SUBJECTS, MAX_STEP_CHARS,
                                    Edge, Graph, _dec_str, _digest, _number, _type_of)

ENGINE = "lot_impact"
MAX_ORDERS = 100        # order entries one answer carries (the answer holds 200 entries)
MAX_CANDIDATES = 20
MAX_TWINS_WALKED = 32
ARG_KEYS = {"lot", "ordered"}

RECEIVED_LOT = "ReceiptLine.received_lot"
LOT_RECEIPT = "Lot.received_in"
LINE_RECEIPT = "ReceiptLine.line_of"
RECEIPT_PO = "Receipt.receives"
LINE_PO_LINE = "ReceiptLine.receives_line"
PO_LINE = "PurchaseOrderLine.line_of"
PO_SUPPLIER = "PurchaseOrder.ordered_from"
RECEIPT_SUPPLIER = "Receipt.supplied_by"
CONSUMED = "AssemblyOrderComponent.consumed_lot"
COMPONENT_OF = "AssemblyOrderComponent.component_of"
AO_TWIN = "AssemblyOrder.same_record_as"
AO_LINE = "AssemblyOrder.for_order_line"
ALLOCATES = "AssemblyOrderAllocation.allocates"
ALLOCATION_LINE = "AssemblyOrderAllocation.for_order_line"
ORDER_LINE = "SalesOrderLine.line_of"
ORDER_TWIN = "SalesOrder.same_record_as"
SHIPS = "Shipment.ships"
BATCH_TWIN = "Batch.same_record_as"
FILLS_FROM = "AssemblyOrder.fills_from"
BATCH_ALLOCATES = "BatchAllocation.allocates"
BATCH_ALLOCATION_LINE = "BatchAllocation.for_order_line"
SCHEDULE_BATCH = "BatchSchedule.for_batch"
SHIPMENT_LINE_OF = "ShipmentLine.line_of"
ORDERED_BY = "SalesOrder.ordered_by"
SHIPPED_TO = "Shipment.shipped_to"
CUSTOMER_TWIN = "Customer.same_record_as"
# Declared by the schema the next build serves: the plan's steps for them
# are ``if_declared``, and the walk uses their links wherever the store holds them.
BATCH_ORDER = "Batch.for_sales_order"
SCHEDULE_PRODUCT = "BatchSchedule.for_bulk_product"
SHIPMENT_LINE_ORDER = "ShipmentLine.of_sales_order"
INVOICE_LINE_ORDER = "SalesInvoiceLine.of_sales_order"
INVOICE_LINE_SHIPMENT = "SalesInvoiceLine.of_shipment"
AFTER_BUILD = (BATCH_ORDER, SCHEDULE_PRODUCT, SHIPMENT_LINE_ORDER, INVOICE_LINE_ORDER,
               INVOICE_LINE_SHIPMENT)
LINK_TYPES = (RECEIVED_LOT, LOT_RECEIPT, LINE_RECEIPT, RECEIPT_PO, LINE_PO_LINE, PO_LINE,
              PO_SUPPLIER, RECEIPT_SUPPLIER, CONSUMED, COMPONENT_OF, AO_TWIN, AO_LINE,
              ALLOCATES, ALLOCATION_LINE, ORDER_LINE, ORDER_TWIN, SHIPS, BATCH_TWIN,
              FILLS_FROM, BATCH_ALLOCATES, BATCH_ALLOCATION_LINE, SCHEDULE_BATCH,
              SHIPMENT_LINE_OF, ORDERED_BY, SHIPPED_TO, CUSTOMER_TWIN, *AFTER_BUILD)

# A walk that stops: reason -> (link types that would carry it, the source that would).
BREAKS: dict[str, tuple[tuple[str, ...], str]] = {
    "receipt_not_recorded": (
        (RECEIVED_LOT, LOT_RECEIPT), "the receipt line carrying the lot (a FileMaker raw "
        "material or packaging receipt, or a Portal receipt line)"),
    "purchase_order_not_linked": (
        (RECEIPT_PO, LINE_PO_LINE, PO_LINE), "the purchase order on the receipt or the PO "
        "line on the receipt line"),
    "supplier_not_linked": (
        (PO_SUPPLIER, RECEIPT_SUPPLIER), "the supplier on the purchase order or receipt"),
    "lot_consumption_not_recorded": (
        (CONSUMED,), "which assembly-order component consumed the lot: no source the "
        "Workbench reads records it yet (the Portal does not share the lot on its "
        "manufacturing order lines); a review accepting a lot-number match records it"),
    "lot_consumption_lead_only": (
        (), "a firm record of the consumption (only lot-number matches were found: "
        "candidates, see the candidate entries)"),
    "batch_not_linked_to_order": (
        (FILLS_FROM, BATCH_ALLOCATES, BATCH_ORDER), "the assembly orders filled from the "
        "batch (the batch named on a FileMaker assembly order), its FileMaker order-entry "
        "batch allocations, or the one order its order-entry list names"),
    "component_without_assembly_order": (
        (COMPONENT_OF,), "the assembly order named on the FileMaker component"),
    "assembly_order_not_linked_to_order_line": (
        (AO_LINE, ALLOCATES, ALLOCATION_LINE, AO_TWIN), "the Portal manufacturing order's "
        "customer order line or the FileMaker order-entry assembly allocation"),
    "allocation_without_order_line": (
        (ALLOCATION_LINE, BATCH_ALLOCATION_LINE), "the order-entry line named on the "
        "FileMaker assembly or batch allocation"),
    "order_line_not_linked_to_order": (
        (ORDER_LINE,), "the order named on the customer order line"),
    "customer_not_linked": (
        (ORDERED_BY, SHIPPED_TO), "the customer on the order (or a copy of it) or on its "
        "shipments"),
    "link_lead_only": (
        (), "a firm-tier record of the link (only a lead-tier link was found)"),
    # Never ``absent_in_source``: only the order and its copies within two same_record_as
    # links are read, so no shipment found there does not prove none is recorded.
    "shipment_not_found": (
        (), "a shipment (Portal, Sage or Business Central) linked to the order or to a "
        "copy of it in another system, directly or by a Sage shipment or invoice line of "
        "the order; none was found on the records read, and copies further away are not "
        "read"),
}
STAGE = {reason: ("supply" if reason in ("receipt_not_recorded", "purchase_order_not_linked",
                                         "supplier_not_linked") else "demand")
         for reason in BREAKS}


@dataclass(frozen=True)
class Break:
    reason: str
    subject: str
    lot: str


@dataclass
class Hit:
    """One customer order cluster the lot reaches."""
    key: str
    members: list[str]
    lines: dict[str, list[Edge]] = field(default_factory=dict)   # order line -> path
    direct: list[Edge] = field(default_factory=list)   # reached without an order line
    records: list[str] = field(default_factory=list)
    shipments: dict[str, list[Edge]] = field(default_factory=dict)   # shipment -> path
    customers: dict[str, tuple[str, Edge]] = field(default_factory=dict)  # -> (role, edge)


@dataclass
class Walk:
    lot: str
    supply: list[tuple[str, list[Edge], list[str]]]   # (purchase order, path, records)
    suppliers: dict[str, Edge]
    orders: dict[str, Hit]
    candidates: list[tuple[str, Edge]]
    breaks: list[Break]
    cut: bool = False
    batches: list[str] = field(default_factory=list)   # the batch cluster walked
    products: dict[str, list[Edge]] = field(default_factory=dict)   # QC schedule's bulk


def _edges(raw: Iterable[Mapping[str, Any]]) -> list[Edge]:
    return [Edge(str(e.get("link_id")), str(e.get("link_type")), str(e.get("from")),
                 str(e.get("to")), str(e.get("tier"))) for e in raw if isinstance(e, Mapping)]


def _shipments(graph: Graph, member: str) -> list[tuple[str, list[Edge]]]:
    """The shipments recorded for one order record: by the shipment header, by a Sage
    shipment line of the order, or by a Sage invoice line of the order that names it."""
    found = [(ship, [e]) for ship, e in graph.rev(SHIPS, member)]
    for line, e_l in graph.rev(SHIPMENT_LINE_ORDER, member):
        found += [(ship, [e_l, e]) for ship, e in graph.fwd(SHIPMENT_LINE_OF, line)]
    for line, e_l in graph.rev(INVOICE_LINE_ORDER, member):
        found += [(ship, [e_l, e]) for ship, e in graph.fwd(INVOICE_LINE_SHIPMENT, line)]
    return found


def walk(lot: str, graph: Graph, leads: Mapping[str, list[Edge]]) -> Walk:
    """Both walks from ``lot`` (a supplier ``Lot``, or a production ``Batch`` and its
    recorded same-batch records) over ``graph``'s firm links; ``leads``: the lead-tier
    ``consumed_lot`` edges into each lot (candidates, never crossed)."""
    breaks: dict[tuple[str, str], Break] = {}
    cut = False

    def stop(reason: str, subject: str, link_type: str | None = None,
             forward: bool = True) -> None:
        if link_type is not None and graph.lead(link_type, subject, forward):
            reason = "link_lead_only"
        breaks.setdefault((reason, subject), Break(reason, subject, lot))

    supply: dict[str, tuple[str, list[Edge], list[str]]] = {}
    suppliers: dict[str, Edge] = {}
    candidates: list[tuple[str, Edge]] = []
    # (order line, path, records) and (order, path, records) the demand side reaches
    order_lines: list[tuple[str, list[Edge], list[str]]] = []
    direct: list[tuple[str, list[Edge], list[str]]] = []
    seen_aos: set[str] = set()
    batches: list[str] = []
    products: dict[str, list[Edge]] = {}

    def assembly(ao: str, base: list[Edge], brecords: list[str]) -> None:
        """The order lines one assembly order (and its twins) is made or allocated for."""
        nonlocal cut
        if ao in seen_aos:
            return
        twins, capped = graph.twins(AO_TWIN, ao)
        cut = cut or capped
        seen_aos.update(twins)
        found = len(order_lines)
        allocated = False
        for a, apath in sorted(twins.items()):
            arec = [*brecords, *([a] if a != ao else [])]
            for sol, e in graph.fwd(AO_LINE, a):
                order_lines.append((sol, [*base, *apath, e], [*arec, sol]))
            for alloc, e_alloc in graph.rev(ALLOCATES, a):
                allocated = True
                lines = graph.fwd(ALLOCATION_LINE, alloc)
                if not lines:
                    stop("allocation_without_order_line", alloc, ALLOCATION_LINE)
                for sol, e in lines:
                    order_lines.append((sol, [*base, *apath, e_alloc, e],
                                        [*arec, alloc, sol]))
        if len(order_lines) == found and not allocated:   # else the allocation's break
            stop("assembly_order_not_linked_to_order_line", ao, AO_LINE)

    if _type_of(lot) == "Batch":
        cluster, capped = graph.twins(BATCH_TWIN, lot)
        cut = cut or capped
        batches = sorted(cluster)
        for b, bpath in sorted(cluster.items()):
            brec = [b] if b != lot else []
            for ao, e in graph.rev(FILLS_FROM, b):
                assembly(ao, [*bpath, e], [*brec, ao])
            for alloc, e_alloc in graph.rev(BATCH_ALLOCATES, b):
                lines = graph.fwd(BATCH_ALLOCATION_LINE, alloc)
                if not lines:
                    stop("allocation_without_order_line", alloc, BATCH_ALLOCATION_LINE)
                for sol, e in lines:
                    order_lines.append((sol, [*bpath, e_alloc, e], [*brec, alloc, sol]))
            for so, e in graph.fwd(BATCH_ORDER, b):
                direct.append((so, [*bpath, e], [*brec, so]))
            for sched, e_s in graph.rev(SCHEDULE_BATCH, b):
                for product, e in graph.fwd(SCHEDULE_PRODUCT, sched):
                    products.setdefault(product, [*bpath, e_s, e])
        if not order_lines and not direct and not seen_aos and not breaks:
            stop("batch_not_linked_to_order", lot, FILLS_FROM, False)
    else:
        # supply
        lines = graph.rev(RECEIVED_LOT, lot)
        receipts = [(r, [e], []) for r, e in graph.fwd(LOT_RECEIPT, lot)]
        receipts += [(r, [e_l, e], [line]) for line, e_l in lines
                     for r, e in graph.fwd(LINE_RECEIPT, line)]
        if not lines and not receipts:
            stop("receipt_not_recorded", lot, RECEIVED_LOT, False)
        for r, path, recs in receipts:
            for po, e in graph.fwd(RECEIPT_PO, r):
                supply.setdefault(po, (po, [*path, e], [*recs, r]))
        for line, e_l in lines:
            for pol, e_pl in graph.fwd(LINE_PO_LINE, line):
                for po, e in graph.fwd(PO_LINE, pol):
                    supply.setdefault(po, (po, [e_l, e_pl, e], [line, pol]))
        if (lines or receipts) and not supply:
            stop("purchase_order_not_linked", (receipts[0][0] if receipts else lines[0][0]),
                 RECEIPT_PO)
        for po in supply:
            for sup, e in graph.fwd(PO_SUPPLIER, po):
                suppliers.setdefault(sup, e)
        for r, _path, _recs in receipts:
            for sup, e in graph.fwd(RECEIPT_SUPPLIER, r):
                suppliers.setdefault(sup, e)
        if (lines or receipts) and not suppliers:
            stop("supplier_not_linked", next(iter(supply), receipts[0][0] if receipts
                                             else lines[0][0]), PO_SUPPLIER)
        # demand
        comps = graph.rev(CONSUMED, lot)
        firm = {c for c, _e in comps}
        candidates = [(e.frm, e) for e in leads.get(lot, ()) if e.frm not in firm]
        if not comps:
            stop("lot_consumption_lead_only" if candidates
                 else "lot_consumption_not_recorded", lot)
        for comp, e_c in comps:
            aos = graph.fwd(COMPONENT_OF, comp)
            if not aos:
                stop("component_without_assembly_order", comp, COMPONENT_OF)
            for ao, e_ao in aos:
                assembly(ao, [e_c, e_ao], [comp, ao])

    orders: dict[str, Hit] = {}

    def hit_of(so: str) -> tuple[Hit | None, list[Edge]]:
        """The order cluster of ``so`` and the twin links crossed to reach its members."""
        nonlocal cut
        members, capped = graph.twins(ORDER_TWIN, so)
        cut = cut or capped
        key = sorted(members)[0]
        hit = orders.get(key)
        if hit is None:
            if len(orders) >= MAX_ORDERS:
                cut = True
                return None, []
            hit = orders[key] = Hit(key, sorted(members))
            for m in hit.members:
                for ship, path in _shipments(graph, m):
                    hit.shipments.setdefault(ship, path)
                for customer, e in graph.fwd(ORDERED_BY, m):
                    hit.customers.setdefault(customer, ("ordered", e))
            for ship in sorted(hit.shipments):
                for customer, e in graph.fwd(SHIPPED_TO, ship):
                    if hit.customers.get(customer, ("",))[0] != "shipped":
                        hit.customers[customer] = ("shipped", e)
            if not hit.shipments:
                stop("shipment_not_found", key)
            if not hit.customers:
                stop("customer_not_linked", key)
        return hit, list(dict.fromkeys(e for m in sorted(members) for e in members[m]))

    for sol, spath, srecords in order_lines:
        found = graph.fwd(ORDER_LINE, sol)
        if not found:
            stop("order_line_not_linked_to_order", sol, ORDER_LINE)
        for so, e_so in found:
            hit, twin_edges = hit_of(so)
            if hit is None:
                continue
            hit.lines.setdefault(sol, [*spath, e_so, *twin_edges])
            hit.records = list(dict.fromkeys([*hit.records, lot, *srecords, so]))
    for so, path, records in direct:
        hit, twin_edges = hit_of(so)
        if hit is None:
            continue
        if not hit.direct:
            hit.direct = [*path, *twin_edges]
        hit.records = list(dict.fromkeys([*hit.records, lot, *records]))
    return Walk(lot, sorted(supply.values()), suppliers, orders, candidates,
                list(breaks.values()), cut, batches, products)


def break_gap(found: Sequence[Break], reason: str, *, complete: bool,
              covers: Callable[[str], bool]) -> dict[str, Any]:
    types, source = BREAKS.get(reason, ((), "unknown"))
    known = complete and bool(types) and all(covers(t) for t in types)
    subjects = sorted({b.subject for b in found})
    more = len(subjects) - MAX_GAP_SUBJECTS
    detail = (f"{reason}: {len(found)} walk(s) from the lot stop here; the source that "
              f"would hold the link: {source}"
              + ("; the walk read every such link this reader may see" if known else
                 "; the walk was cut or the reader's scope may hide one, so this is not "
                 "proof that none is recorded")
              + "; no link is inferred from a shared code, name or date")
    return {"state": "absent_in_source" if known else "unknown", "reason": reason,
            "subject": (", ".join(subjects[:MAX_GAP_SUBJECTS])
                        + (f" and {more} more" if more > 0 else ""))[:700],
            "detail": detail[:600]}


def _quantity(hit: Hit, ordered: Mapping[str, Sequence[Mapping[str, Any]]]
              ) -> tuple[Decimal | None, str | None, list[str], str | None]:
    """(quantity, unit, the entries it rests on, why none) of one order's touched lines:
    a Decimal sum per system binding; bindings that disagree, a line without a stated
    quantity or units that differ give none."""
    from ontology.units import unit_token
    sums: dict[str, Decimal] = {}
    units: set[str | None] = set()
    used: list[str] = []
    if not hit.lines:   # the batch names the order as a whole, not its lines
        return None, None, [], "touched_order_lines_not_recorded"
    for sol in sorted(hit.lines):
        entries = [e for e in ordered.get(sol, ()) if e.get("value") is not None]
        amounts = {_number(e.get("value")) for e in entries}
        if not entries or None in amounts or len(amounts) != 1:
            return None, None, [], "order_quantity_not_stated"
        binding = sol.split(":", 2)[1]
        sums[binding] = sums.get(binding, Decimal(0)) + amounts.pop()
        units.update(unit_token(e.get("unit")) for e in entries)
        used.extend(e["name"] for e in entries)
    if len(units) > 1:
        return None, None, [], "order_quantity_units_differ"
    if len(set(sums.values())) != 1:
        return None, None, [], "order_line_records_disagree"
    return next(iter(sums.values())), units.pop(), used, None


# --------------------------------------------------------------------------- the plan engine

def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with ``engine_args``: ``lot`` names a Lot property item and ``ordered`` a
    SalesOrderLine one (quantity role ordered), each listed before the compute item."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    out = [f"unknown engine argument {k}" for k in sorted(set(args) - ARG_KEYS)]
    out += [f"missing engine argument {k}" for k in sorted(ARG_KEYS - set(args))]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    for key, type_id in (("lot", "Lot"), ("ordered", "SalesOrderLine")):
        item = items.get(args.get(key))
        if item is None or item["kind"] != "property" or item.get("object_type") != type_id:
            out.append(f"{key} must name a {type_id} property item listed before it")
    if (items.get(args.get("ordered")) or {}).get("quantity_role") not in (None, "ordered"):
        out.append("ordered must name the ordered quantity")
    links = getattr(schema, "link_types", None) or {}
    if links:   # the after-build ones are walked where the served schema declares them
        out += [f"undeclared link type {t}" for t in LINK_TYPES
                if t not in links and t not in AFTER_BUILD]
    return out


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Any, actor: Any, complete: bool,
             covers: Callable[[str], bool], roots: Sequence[str] = ()) -> dict[str, Any]:
    """The supply chain, the customer orders, shipments and customers the plan's lots and
    batches touch (module doc)."""
    edge_list = _edges(edges)
    graph = Graph(edge_list, LINK_TYPES)
    leads = _leads(edge_list)
    lots = sorted({str(e["object_id"]) for e in evidence.get(args["lot"], ())
                   if e.get("kind") == "property" and _type_of(e.get("object_id")) == "Lot"})
    # a batch root with its recorded same-batch records is walked once
    walked: set[str] = set()
    for batch in sorted(r for r in roots if _type_of(r) == "Batch"):
        if batch not in walked:
            walked.update(graph.twins(BATCH_TWIN, batch)[0])
            lots.append(batch)
    ordered: dict[str, list[Mapping[str, Any]]] = {}
    for entry in evidence.get(args["ordered"], ()):
        if entry.get("kind") == "property":
            ordered.setdefault(str(entry.get("object_id")), []).append(entry)
    gaps: list[dict[str, Any]] = []
    reasons: list[str] = []
    if not lots:
        gaps.append({"state": "unknown", "reason": "no_lot_reached", "subject": name,
                     "detail": "no_lot_reached: the plan read no lot this reader can see"})
        return {"entries": [], "gaps": gaps, "reasons": ["no_lot_reached"]}
    walks = [walk(lot, graph, leads) for lot in lots]
    cut = any(w.cut for w in walks)
    grouped: dict[str, list[Break]] = {}
    for w in walks:
        for brk in w.breaks:
            grouped.setdefault(brk.reason, []).append(brk)
    for reason in sorted(grouped):
        gaps.append(break_gap(grouped[reason], reason, complete=complete and not cut,
                              covers=covers))
    if cut:
        gaps.append({"state": "unknown", "reason": "lot_walk_capped", "subject": name,
                     "detail": f"lot_walk_capped: more than {MAX_ORDERS} orders or "
                               f"{MAX_TWINS_WALKED} twins from one record; the rest were "
                               "not walked"})
        reasons.append("lot_walk_capped")
    entries: list[dict[str, Any]] = []
    for n, w in enumerate(walks, start=1):
        if not w.supply and not w.suppliers:
            continue
        steps = [f"lot {w.lot} was received on purchase order {po}" for po, _p, _r in w.supply]
        steps += [f"crossed {e.cited()}" for _po, path, _r in w.supply for e in path]
        steps += [f"supplier {s}: crossed {e.cited()}" for s, e in sorted(w.suppliers.items())]
        basis = list(dict.fromkeys([w.lot, *(r for _po, _p, recs in w.supply for r in recs),
                                    *(po for po, _p, _r in w.supply), *sorted(w.suppliers)]))
        entries.append(_digest({
            "name": f"{name}_supply_{n}"[:64], "kind": "computed", "value": len(w.supply),
            "inputs": [], "steps": [s[:MAX_STEP_CHARS] for s in steps][:40],
            "basis": basis[:100], "check": "not_applicable"}))
    for n, w in enumerate(walks, start=1):
        if not w.batches:
            continue
        keys = sorted(w.orders)
        steps = [f"batch {w.lot}"
                 + (f" (with its same-batch records {', '.join(b for b in w.batches if b != w.lot)})"
                    if len(w.batches) > 1 else "")
                 + f" reaches {len(keys)} customer order(s) over recorded firm-tier links"]
        steps += [f"its QC calibration schedule names bulk product {p}: crossed "
                  + "; ".join(e.cited() for e in path) for p, path in sorted(w.products.items())]
        entries.append(_digest({
            "name": f"{name}_batch_{n}"[:64], "kind": "computed", "value": len(keys),
            "inputs": [], "steps": [s[:MAX_STEP_CHARS] for s in steps][:40],
            "basis": list(dict.fromkeys([*w.batches, *sorted(w.products), *keys]))[:100],
            "check": "not_applicable"}))
    hits: dict[str, tuple[Hit, list[str]]] = {}
    for w in walks:
        for key, hit in w.orders.items():
            if key in hits:
                old = hits[key][0]
                for sol, path in hit.lines.items():
                    old.lines.setdefault(sol, path)
                old.direct = old.direct or hit.direct
                old.records = list(dict.fromkeys([*old.records, *hit.records]))
                hits[key][1].append(w.lot)
            else:
                hits[key] = (hit, [w.lot])
    order_names: list[str] = []
    unstated = 0
    for n, (key, (hit, from_lots)) in enumerate(sorted(hits.items()), start=1):
        qty, unit, used, why = _quantity(hit, ordered)
        steps = [f"lot {', '.join(from_lots)} reaches customer order {key}"]
        for sol in sorted(hit.lines):
            steps += [f"crossed {e.cited()}" for e in hit.lines[sol]]
        if hit.direct:
            steps.append("the batch names the order as a whole (its OE list), not its lines")
            steps += [f"crossed {e.cited()}" for e in hit.direct]
        if hit.shipments:
            steps += [f"shipment {s} recorded for the order: crossed "
                      + "; ".join(e.cited() for e in path)
                      for s, path in sorted(hit.shipments.items())]
            steps.append("which lot a shipment carried is not recorded")
        else:   # the shipment_not_found gap names the order
            steps.append("no shipment found on the records read")
        steps += [f"customer {c} ({'shipped to' if role == 'shipped' else 'ordered by'}): "
                  f"crossed {e.cited()}" for c, (role, e) in sorted(hit.customers.items())]
        entry: dict[str, Any] = {"name": f"{name}_{n}"[:64], "kind": "computed",
                                 "value": None, "inputs": used[:50]}
        if qty is None:
            unstated += 1
            steps.append(f"quantity touched not stated: {why}")
            gaps.append({"state": "unknown", "reason": why, "subject": key,
                         "detail": f"{why}: the ordered quantity of the order lines the lot "
                                   "reaches is not stated by one cited value per line"})
        else:
            entry.update(value=_dec_str(qty), quantity_role="ordered")
            if unit:
                entry["unit"] = unit
            steps.append(f"quantity touched: ordered quantity {_dec_str(qty)}"
                         + (f" {unit}" if unit else " (the source states no unit)")
                         + f" over {len(hit.lines)} order line(s) the lot's assembly orders "
                         "or the batch are made or allocated for; the lot's own quantity in "
                         "the order is not recorded"
                         + ("; the batch also names the whole order, whose other lines are "
                            "not counted" if hit.direct else ""))
        basis = list(dict.fromkeys([*hit.records, *hit.members, *sorted(hit.shipments),
                                    *sorted(hit.customers)]))
        entry.update(steps=[s[:MAX_STEP_CHARS] for s in steps][:40], basis=basis[:100],
                     check="not_applicable")
        entries.append(_digest(entry))
        order_names.append(entry["name"])
    if unstated:
        reasons.append("order_quantity_not_stated")
    k = 0
    for w in walks:
        for comp, e in w.candidates:
            k += 1
            if k > MAX_CANDIDATES:
                break
            entries.append(_digest({
                "name": f"{name}_candidate_{k}"[:64], "kind": "computed", "value": comp,
                "inputs": [], "lead_basis": True,
                "steps": [f"candidate only: {e.cited()} is a lead-tier match by lot number, "
                          "not a recorded consumption; it is not walked to any order"[
                              :MAX_STEP_CHARS]],
                "basis": [w.lot, comp], "check": "not_applicable"}))
    if k > MAX_CANDIDATES:
        gaps.append({"state": "unknown", "reason": "candidates_capped", "subject": name,
                     "detail": f"candidates_capped: {k - MAX_CANDIDATES} more lot-number "
                               "candidates were not listed"})
    reached = sum(1 for w in walks if w.orders)
    # Lot records sharing a lot number may be different suppliers' lots: their orders are
    # counted per supplier, never added together. A record whose supplier is not linked
    # cannot be told apart, so it is its own group too.
    groups: dict[tuple[str, ...], list[Walk]] = {}
    for w in walks:
        groups.setdefault((f"batch:{w.lot}",) if w.batches else
                          tuple(sorted(w.suppliers)) or (f"unlinked:{w.lot}",), []).append(w)
    split = len(groups) > 1
    head_steps = [f"{len(hits)} customer order(s) touched by {reached} of {len(walks)} "
                  "lot record(s), over recorded firm-tier links only"]
    if split:
        reasons.append("lot_number_ambiguous")
        gaps.append({"state": "unknown", "reason": "lot_number_ambiguous", "subject": name,
                     "detail": f"lot_number_ambiguous: the {len(walks)} lot records read "
                               f"fall into {len(groups)} supplier groups (or carry no linked "
                               "supplier), so they may be different lots sharing a number; "
                               "orders are counted per supplier group, never together"})
        head_steps = [f"the lot records read belong to {len(groups)} supplier groups: "
                      "no single count; see the per-supplier entries"]
        for g, (sups, ws) in enumerate(sorted(groups.items()), start=1):
            keys = sorted({key for w in ws for key in w.orders})
            linked = [] if sups[0].startswith(("unlinked:", "batch:")) else list(sups)
            entries.append(_digest({
                "name": f"{name}_by_supplier_{g}"[:64], "kind": "computed",
                "value": len(keys), "inputs": [],
                "steps": [s[:MAX_STEP_CHARS] for s in (
                    f"{len(keys)} customer order(s) touched by the lot record(s) "
                    f"{', '.join(w.lot for w in ws)} "
                    + (f"of supplier(s) {', '.join(linked)}" if linked
                       else "(a production batch)" if sups[0].startswith("batch:")
                       else "with no linked supplier"),
                    "counted apart from the other supplier groups' lots of this number")],
                "basis": list(dict.fromkeys([
                    *(w.lot for w in ws), *linked, *keys,
                    *(m for w in ws for _k, hit in sorted(w.orders.items())
                      for m in hit.members)]))[:100],
                "check": "not_applicable"}))
    # One customer is one cluster of its recorded same-customer records across systems.
    clusters: dict[str, dict[str, Any]] = {}
    for key, (hit, _lots) in sorted(hits.items()):
        for customer, (role, _e) in sorted(hit.customers.items()):
            members, capped = graph.twins(CUSTOMER_TWIN, customer)
            cut = cut or capped
            ckey = sorted(members)[0]
            c = clusters.setdefault(ckey, {"members": set(), "shipped": set(),
                                           "ordered": set()})
            c["members"].update(members)
            c["shipped" if role == "shipped" else "ordered"].add(key)
    customer_steps = []
    for ckey, c in sorted(clusters.items()):
        shipped = sorted(c["shipped"])
        only = sorted(c["ordered"] - c["shipped"])
        customer_steps.append(
            f"customer {ckey}"
            + (f" (same customer: {', '.join(sorted(c['members'] - {ckey}))})"
               if len(c["members"]) > 1 else "")
            + (f": shipped on order(s) {', '.join(shipped)}" if shipped else "")
            + (f"{';' if shipped else ':'} ordered order(s) {', '.join(only)} with no shipment "
               "to it found" if only else ""))
    entries.append(_digest({
        "name": f"{name}_customers"[:64], "kind": "computed",
        "value": None if split else len(clusters), "inputs": [],
        "steps": [s[:MAX_STEP_CHARS] for s in (
            [f"{len(clusters)} customer(s) on the touched orders, one per customer across "
             "systems (recorded same-customer links); shipped to = a recorded shipment of "
             "the order names them"] + customer_steps)][:40],
        "basis": sorted({m for c in clusters.values() for m in c["members"]})[:100],
        "check": "not_applicable"}))
    entries.append(_digest({
        "name": name, "kind": "computed", "value": None if split else len(hits),
        "inputs": order_names[:50],
        "steps": head_steps
        + [f"{min(k, MAX_CANDIDATES)} candidate consumption(s) by lot number only, "
           "not walked"]
        + ([] if complete and not cut else
           ["the run was cut, so other orders may be touched"]),
        "basis": sorted(hits)[:100], "check": "not_applicable"}))
    return {"entries": entries, "gaps": gaps[:50], "reasons": reasons}


def _leads(edges: Iterable[Edge]) -> dict[str, list[Edge]]:
    out: dict[str, list[Edge]] = {}
    for e in sorted(edges, key=lambda e: (e.to, e.frm, e.link_id)):
        if e.link_type == CONSUMED and e.tier in LEAD_TIERS:
            if all(o.link_id != e.link_id for o in out.get(e.to, ())):
                out.setdefault(e.to, []).append(e)
    return out


PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run}}


# --------------------------------------------------------------------------- store scan

def scan(conn: Any, *, visible: Container[str] | None = None,
         held: Iterable[str] = frozenset(), bound: int | None = None) -> dict[str, Any]:
    """Counts only, over the projection store (``conn``: its SQLite connection): the lots
    investigated (the first ``bound`` in id order), those reaching at least one customer
    order, those with a lot-number candidate, the orders reached and the lots stopped at
    each gap type. Links and lots are those ``visible`` (object ids the reader may read;
    None: all) whose markings the reader ``held``."""
    held = frozenset(held)
    marks = ", ".join("?" for _ in LINK_TYPES)
    edges = []
    for link_id, link_type, frm, to, tier, markings in conn.execute(
            "SELECT link_id, link_type, from_object, to_object, tier, markings FROM links "
            f"WHERE superseded_at IS NULL AND link_type IN ({marks})", LINK_TYPES):
        try:
            need = set(json.loads(markings or "[]"))
        except ValueError:
            continue          # unreadable markings: fully marked
        if not need <= held or tier not in FIRM_TIERS + LEAD_TIERS:
            continue
        if visible is not None and (frm not in visible or to not in visible):
            continue
        edges.append(Edge(str(link_id), link_type, frm, to, tier))
    graph, leads = Graph(edges, LINK_TYPES), _leads(edges)
    lots = [oid for (oid,) in conn.execute(
        "SELECT object_id FROM objects WHERE object_type = 'Lot' ORDER BY object_id")
        if visible is None or oid in visible]
    truncated = bound is not None and len(lots) > bound
    lots = lots[:bound] if bound is not None else lots
    stopped: dict[str, int] = {}
    orders: set[str] = set()
    with_order = with_candidates = 0
    for lot in lots:
        w = walk(lot, graph, leads)
        with_order += bool(w.orders)
        with_candidates += bool(w.candidates)
        orders.update(w.orders)
        for reason in sorted({b.reason for b in w.breaks}):
            stopped[reason] = stopped.get(reason, 0) + 1
    return {"lots_investigated": len(lots), "lots_with_order": with_order,
            "lots_with_candidates": with_candidates, "orders_reached": len(orders),
            "stopped_by_gap": dict(sorted(stopped.items())), "truncated": truncated}
