"""Typed shipments view (PLAN-P2-A5 / PLAN-A18): every customer shipment once (Portal,
Sage OE and Business Central copies joined by settled identity links), with its ship date,
carrier/tracking, linked sales order, governing required date (by the one sales-order
rule, rejecting entry-default own-day dates), and on-time / days-late verdict.

Registered by ``operating.pg.business_views`` after its own helpers (import
``business_views``, never this module first). Same rules as there: one row per real thing,
plain columns, currency beside every amount, door v2 permissions and lineage on every row."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Iterator

from operating.pg.business_views import (
    CHAIN_COL, ORDER_LINK, ORDER_RECORD_SYSTEM, PILOT_DOC, Reader, Row, Thing, _rep_handle,
    _text, add_chain, customer_chains, customer_link, customer_things, fill, handle_col,
    money, pick, pilot_binding, to_date, to_decimal, view)

SHIPMENT_PROPS = (
    "shipment_number", "sage_shipment_number", "bc_number",
    "ship_date", "delivery_date", "status", "carrier", "tracking_number",
    "freight_cost", "total_units", "customer_po",
)
SHIPMENT_ORDER_LINK = "Shipment.ships"
SHIPMENT_CUSTOMER_LINK = "Shipment.shipped_to"


@view("shipments", "Every customer shipment once (Sage OE, Portal and Business Central "
      "copies joined by settled identity links), with its ship date, linked sales order, "
      "the order's governing required date (excluding entry defaults equal to order_date), "
      "and whether it shipped on time (ship_date <= required_date). Business Central-only "
      "shipments carry pilot = true.",
      [("shipment_number", "text", "shipment number (Portal shipment_number, else Sage "
        "sage_shipment_number, else BC bc_number)"),
       ("ship_date", "date", "the date the shipment went out (BC posted shipment postingDate)"),
       ("delivery_date", "date", "delivery date, when stated"),
       ("status", "text", "shipment status, when stated"),
       ("carrier", "text", "carrier name, when stated"),
       ("tracking_number", "text", "carrier tracking number, when stated"),
       ("freight_cost", "money", "freight cost in its currency, when stated"),
       ("total_units", "numeric", "total units shipped, when stated"),
       ("customer_po", "text", "customer PO number, when stated"),
       ("order_number", "text", "linked sales order number (via Shipment.ships)"),
       ("order_date", "date", "linked sales order date"),
       ("required_date", "date", "the linked sales order's governing required date (same "
        "rule as sales_orders: empty when entry-default own_day, source default, or missing)"),
       ("required_date_note", "text", "why required_date is empty: no_order, "
        "required_date_own_day, required_date_default, no_required_date, "
        "required_date_unreadable, or required_date_disputed"),
       ("on_time", "boolean", "true when ship_date <= required_date, false when "
        "ship_date > required_date; NULL when either date is missing"),
       ("days_late", "integer", "ship_date - required_date in days (0 or less: on time; "
        "positive: late); NULL when either date is missing"),
       ("customer", "text", "customer name (from Shipment.shipped_to, else the linked "
        "sales order's customer)"),
       CHAIN_COL,
       ("pilot", "boolean", PILOT_DOC),
       ("order_handle", "text", "handle of the linked sales order (join sales_orders on "
        "handle), when linked"),
       handle_col("customer", f"the shipment's {SHIPMENT_CUSTOMER_LINK} link")],
      reads=("Shipment", "SalesOrder", "Customer"),
      memos=("customers", "chains"),
      covers=False)   # keep Shipment in generic records too so source measures keep working
def _shipments(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    from operating import po_status
    from operating import sales_order_status as so

    ships = rd.records("Shipment", SHIPMENT_PROPS)
    if not ships:
        return
    ship_things = rd.clusters("Shipment", ships)
    customers, cthings = customer_things(rd)
    chains = customer_chains(rd)
    shipped_to = rd.linked(SHIPMENT_CUSTOMER_LINK, ships, customers)

    # Load SalesOrder headers for linked orders (far lighter than full _sales_orders_model)
    so_props = tuple(dict.fromkeys(("order_number", "order_date", so.DATE_PROP, *so.OWN_DAYS)))
    so_recs = rd.records(so.TYPE, so_props)
    so_things = rd.clusters(so.TYPE, so_recs)
    so_by_id = {r.object_id: t for t in so_things for r in t.members}
    so_ordered_by = rd.linked(ORDER_LINK, so_recs, customers)
    ships_order = rd.linked(SHIPMENT_ORDER_LINK, ships, so_recs)

    # Precompute source date defaults across SalesOrders (same rule as _sales_orders_model)
    so_binding = {oid: po_status.binding_of(oid) or "" for oid in so_recs}
    so_rows_map = {oid: rec.rows(so_props) for oid, rec in so_recs.items()}
    grouped: dict[str, list] = defaultdict(list)
    for oid in so_recs:
        own = [r.get("value") for n in so.OWN_DAYS for r in so_rows_map[oid].get(n, ())]
        for r in so_rows_map[oid].get(so.DATE_PROP, ()):
            grouped[so_binding[oid]].append((so.day_of(r), own))
    defaults = so.date_defaults(grouped)

    wanted_cache: dict[str, tuple[dt.date | None, str | None, dict | None]] = {}

    def order_wanted(ot: Thing) -> tuple[dt.date | None, str | None, dict | None]:
        key = ot.rep.object_id
        if key not in wanted_cache:
            group = [m.object_id for m in ot.members]
            w = so.requested_date([
                (so_rows_map[m].get(so.DATE_PROP, ()),
                 [r.get("value") for n in so.OWN_DAYS for r in so_rows_map[m].get(n, ())],
                 defaults.get(so_binding[m], ())) for m in group])
            day = w.day if w is not None else None
            note = w.reason if w else None
            if note == so.OWN_DAY:
                day = None
            gov = w.governing[0] if (day is not None and w and w.governing) else None
            wanted_cache[key] = (day, note, gov)
        return wanted_cache[key]

    for thing in ship_things:
        row = Row(thing)
        if fill(row, thing, "shipment_number", "shipment_number") is None:
            if fill(row, thing, "shipment_number", "sage_shipment_number") is None:
                fill(row, thing, "shipment_number", "bc_number")
        ship_day = fill(row, thing, "ship_date", "ship_date", to_date)
        fill(row, thing, "delivery_date", "delivery_date", to_date)
        fill(row, thing, "status", "status")
        fill(row, thing, "carrier", "carrier")
        fill(row, thing, "tracking_number", "tracking_number")
        money(row, "freight_cost", *pick(thing, "freight_cost"), None)
        cell, crec = pick(thing, "total_units")
        row.read(cell, "total_units",
                 to_decimal(cell.num if cell is not None and cell.num is not None
                            else cell.value if cell is not None else None), rec=crec)
        fill(row, thing, "customer_po", "customer_po")

        # Linked SalesOrder via Shipment.ships (settled links)
        linked_orders: dict[str, Thing] = {}
        order_link_marks: set[str] = set()
        for m in thing.members:
            for target_oid, lmarks in ships_order.get(m.object_id, ()):
                ot = so_by_id.get(target_oid)
                if ot is not None:
                    linked_orders[ot.rep.object_id] = ot
                    order_link_marks.add(lmarks)

        order_thing = (next(iter(linked_orders.values()))
                       if len(linked_orders) == 1 else None)
        req_day: dt.date | None = None
        req_note: str | None = None
        if order_thing is not None:
            for lm in order_link_marks:
                row.marks.add(lm)
            row.add_thing(order_thing)
            row.values["order_handle"] = _rep_handle(order_thing.rep)
            fill(row, order_thing, "order_number", "order_number")
            fill(row, order_thing, "order_date", "order_date", to_date)
            req_day, req_note, gov = order_wanted(order_thing)
            row.values["required_date"] = req_day
            row.values["required_date_note"] = req_note
            if req_day is not None and gov is not None:
                row.cites["required_date"] = [gov["l1_handle"], gov["field_path"]]
                if gov.get("markings") not in (None, "", "[]"):
                    row.col_marks["required_date"] = gov["markings"]
        else:
            row.values["order_handle"] = None
            row.values["order_number"] = None
            row.values["order_date"] = None
            row.values["required_date"] = None
            row.values["required_date_note"] = ("multiple_orders" if len(linked_orders) > 1
                                                else "no_order")

        if ship_day is not None and req_day is not None:
            delta = (ship_day - req_day).days
            row.values["days_late"] = delta
            row.values["on_time"] = delta <= 0
            row.derive("days_late", row, "ship_date")
            row.derive("days_late", row, "required_date")
            row.derive("on_time", row, "ship_date")
            row.derive("on_time", row, "required_date")
        else:
            row.values["days_late"] = None
            row.values["on_time"] = None

        row.values["pilot"] = all(pilot_binding(m.binding) for m in thing.members)

        # Customer: Shipment.shipped_to first, else linked order's customer
        cust = customer_link(row, thing, shipped_to, cthings, ORDER_RECORD_SYSTEM)
        if cust is None and order_thing is not None:
            cust = customer_link(row, order_thing, so_ordered_by, cthings, ORDER_RECORD_SYSTEM)
        add_chain(row, cust, chains)
        yield row


from operating.pg import business_views as _bv  # noqa: E402
_bv._register_submodules()
