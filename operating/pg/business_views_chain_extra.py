"""Typed supply-chain views (PLAN-A18 Part 3): ``biz.receipt_lines``, ``biz.quotes``,
and ``biz.freight_bills`` — one row per real thing, plain columns, door v2 permissions
and lineage on every row.

Registered by ``operating.pg.business_views`` after ``business_views_chain``
(import ``business_views``, never this module first)."""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from typing import Any, Iterator, Mapping, Sequence

from operating.pg.business_views import (
    BUSINESS_SCHEMES, IDENTITY_TIERS, NEVER_AUTHORITY_LINEAGES, PILOT_DOC, SOLID_TIERS,
    Cell, Reader, Rec, Row, Thing, _binds, _marks, _marks_size, _rep_handle, _text,
    binding_rank, customer_things, fill, money, name_linked, party_handle, pick,
    pilot_binding, to_date, view)
from operating.pg.business_views_chain import _fill_num, _single_linked
from operating.pg.business_views_supply import PO_PROPS, product_things

DOC_SCHEMES = BUSINESS_SCHEMES + ("graph-mail", "sharepoint")


# --------------------------------------------------------------------------- document-scheme helpers

def _doc_records(rd: Reader, object_type: str, props: Sequence[str]) -> dict[str, Rec]:
    """Read held records of ``object_type`` including ``graph-mail`` and ``sharepoint``
    document-extracted objects (same authority, removal, and marking rules as
    ``Reader.records``)."""
    gone = rd.removed(object_type) | rd.hidden()
    out: dict[str, Rec] = {}
    pool: dict[Any, Any] = {}
    same = pool.setdefault
    rows = rd._rows(
        "SELECT o.object_id, o.binding_id, o.audience_key, v.l1_handle, v.markings AS vm, "
        "COALESCE(s.lineage, v.lineage) AS lineage, f.property, f.field_path, c.value, "
        "c.value_num, c.currency, k.authority_rank, k.markings AS km, k.unit, k.tax_basis "
        "FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
        "AND v.superseded_at IS NULL "
        "JOIN value_sources s ON s.ver = v.ver AND s.superseded_at IS NULL "
        "CROSS JOIN LATERAL (SELECT c.value, c.value_num, c.currency, c.field, c.kind "
        "FROM value_cells c WHERE c.src = s.src OFFSET 0) c "
        "JOIN value_fields f ON f.field = c.field "
        "JOIN value_kinds k ON k.kind = c.kind "
        f"WHERE o.object_type = :t AND f.property IN ({_marks('p', props)})",
        {"t": object_type, **_binds("p", props)})
    for row in rows:
        oid = row["object_id"]
        if oid in gone:
            continue
        handle = row["l1_handle"]
        if handle is None or handle.split("://", 1)[0] not in DOC_SCHEMES:
            continue
        rec = out.get(oid)
        if rec is None:
            rec = out[oid] = Rec(oid, object_type, row["binding_id"] or "",
                                 row["audience_key"] or "")
        handle = same(handle, handle)
        rec.handles[handle] = same(row["vm"] or "[]", row["vm"] or "[]")
        lineage = row["lineage"]
        if lineage:
            lineage = same(lineage, lineage)
            rec.lineages.add(lineage)
        rec.cells.setdefault(same(row["property"], row["property"]), []).append(Cell(
            row["value"], row["value_num"], same(row["currency"], row["currency"]),
            same(row["field_path"] or "", row["field_path"] or ""),
            row["authority_rank"] if row["authority_rank"] is not None else 1 << 30,
            same(row["km"] or "[]", row["km"] or "[]"), handle,
            same(row["unit"], row["unit"]), same(row["tax_basis"], row["tax_basis"]),
            lineage))
    for rec in out.values():
        for cells in rec.cells.values():
            cells.sort(key=lambda c: (c.rank, _marks_size(rec.handles.get(c.handle, "")),
                                      c.field_path, c.handle, str(c.value)))
    return out


def _doc_link_rows(rd: Reader, link_types: Sequence[str],
                   tiers: Sequence[str] | None) -> list[tuple[str, str, str, str, str]]:
    """Live links of ``link_types`` at ``tiers`` whose evidence handles are in ``DOC_SCHEMES``."""
    if not link_types:
        return []
    rows = rd._rows(
        "SELECT l.from_object, l.to_object, l.markings, l.link_type, l.evidence_handles, "
        f"l.tier FROM links l WHERE l.link_type IN ({_marks('lt', link_types)}) "
        "AND l.superseded_at IS NULL "
        + ("" if tiers is None else f"AND l.tier IN ({_marks('ti', tiers)}) ")
        + "AND l.from_object <> l.to_object",
        {**_binds("lt", link_types), **_binds("ti", tiers or ())})
    out = []
    for row in rows:
        try:
            ev = json.loads(row["evidence_handles"] or "[]")
        except ValueError:
            continue
        if any(not isinstance(h, str) or h.split("://", 1)[0] not in DOC_SCHEMES
               for h in ev or ()):
            continue
        out.append((row["from_object"], row["to_object"], row["markings"] or "[]",
                    row["link_type"], row["tier"]))
    return out


def _doc_linked(rd: Reader, link_type: str, ends: Mapping[str, Any], targets: Mapping[str, Any],
                tiers: Sequence[str] = SOLID_TIERS) -> dict[str, list[tuple[str, str]]]:
    """``from_object -> [(to_object, link_markings)]`` accepting ``DOC_SCHEMES`` evidence."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for a, b, marks, _, _t in _doc_link_rows(rd, [link_type], tiers):
        if a in ends and b in targets:
            out[a].append((b, marks))
    for v in out.values():
        v.sort()
    return out


def _doc_clusters(rd: Reader, object_type: str, recs: Mapping[str, Rec]) -> list[Thing]:
    """Identity-link clusters for document-extracted types accepting ``DOC_SCHEMES`` evidence."""
    parent: dict[str, str] = {oid: oid for oid in recs}
    link_marks: dict[str, set[str]] = defaultdict(set)

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    never = set(NEVER_AUTHORITY_LINEAGES)
    for a, b, marks, _, _tier in _doc_link_rows(rd, rd.identity_types(object_type), IDENTITY_TIERS):
        if a not in recs or b not in recs:
            continue
        if recs[a].lineages & never or recs[b].lineages & never:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
        link_marks[a].add(marks)

    groups: dict[str, list[Rec]] = defaultdict(list)
    for oid in sorted(recs):
        groups[find(oid)].append(recs[oid])
    things = []
    for members in groups.values():
        members.sort(key=lambda r: (binding_rank(r.binding), r.object_id))
        marks = set().union(*(link_marks.get(r.object_id, set()) for r in members))
        things.append(Thing(members, marks))
    things.sort(key=lambda t: t.rep.object_id)
    return things


# --------------------------------------------------------------------------- receipt_lines

RECEIPT_LINE_PROPS = (
    "received_quantity", "supplier_lot_number", "lab_id", "expiry_date",
    "bc_unit_cost", "line_type",
)
RL_RECEIPT_LINK = "ReceiptLine.line_of"
RL_ITEM_LINK = "ReceiptLine.for_item"
RL_LOT_LINK = "ReceiptLine.received_lot"
RECEIPT_PO_LINK = "Receipt.receives"


@view("receipt_lines",
      "Every purchase receipt line once (Sage, FileMaker, and BC), with linked receipt, PO, "
      "product, quantity, unit cost, and lot.",
      [("receipt_number", "text", "linked receipt number (Sage sage_receipt_number, else BC bc_number)"),
       ("received_date", "date", "received date from the linked receipt"),
       ("po_number", "text", "purchase order number from linked receipt or PO"),
       ("product", "text", "item name (via ReceiptLine.for_item)"),
       ("item_code", "text", "item code from linked product"),
       ("received_quantity", "numeric", "received quantity as stated"),
       ("unit", "text", "unit of measure on received_quantity, when stated"),
       ("unit_cost", "money", "unit cost in its currency (BC bc_unit_cost), when stated"),
       ("line_type", "text", "line type as stated (BC line_type)"),
       ("lot_number", "text", "lot number (linked Lot, else supplier_lot_number)"),
       ("lab_id", "text", "QC lab identifier (FileMaker lab_id)"),
       ("expiry_date", "date", "expiry date as stated"),
       ("pilot", "boolean", PILOT_DOC),
       ("receipt_handle", "text", "handle of linked receipt (join receipts on handle)"),
       ("po_handle", "text", "handle of linked purchase order (join purchase_orders on handle)"),
       ("product_handle", "text", "handle of linked item (join products on handle)"),
       ("lot_handle", "text", "handle of linked lot (join lots on handle)")],
      reads=("ReceiptLine", "Receipt", "PurchaseOrder", "Product", "Lot"),
      memos=("products",),
      covers=False)
def _receipt_lines(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs = rd.records("ReceiptLine", RECEIPT_LINE_PROPS)
    if not recs:
        return
    rl_things = rd.clusters("ReceiptLine", recs)

    rec_recs = rd.records("Receipt", ("sage_receipt_number", "bc_number", "received_date",
                                      "po_reference", "bc_order_number"))
    rec_things = rd.clusters("Receipt", rec_recs)
    rec_by_id = {r.object_id: t for t in rec_things for r in t.members}
    rl_to_rec = rd.linked(RL_RECEIPT_LINK, recs, rec_recs)

    po_recs = rd.records("PurchaseOrder", PO_PROPS)
    po_things = rd.clusters("PurchaseOrder", po_recs)
    po_by_id = {r.object_id: t for t in po_things for r in t.members}
    rec_to_po = rd.linked(RECEIPT_PO_LINK, rec_recs, po_recs)

    products, pthings = product_things(rd)
    for_item = rd.linked(RL_ITEM_LINK, recs, products)

    lot_recs = rd.records("Lot", ("lot_number", "lot_text"))
    lot_things = rd.clusters("Lot", lot_recs)
    lot_by_id = {r.object_id: t for t in lot_things for r in t.members}
    rl_to_lot = rd.linked(RL_LOT_LINK, recs, lot_recs)

    for thing in rl_things:
        row = Row(thing)
        rec_thing = _single_linked(
            row, [e for m in thing.members for e in rl_to_rec.get(m.object_id, ())], rec_by_id)
        po_thing = None
        if rec_thing is not None:
            row.values["receipt_handle"] = _rep_handle(rec_thing.rep)
            if fill(row, rec_thing, "receipt_number", "sage_receipt_number") is None:
                fill(row, rec_thing, "receipt_number", "bc_number")
            fill(row, rec_thing, "received_date", "received_date", to_date)
            po_thing = _single_linked(
                row, [e for m in rec_thing.members for e in rec_to_po.get(m.object_id, ())],
                po_by_id)
        else:
            row.values["receipt_handle"] = None
            row.values["receipt_number"] = None
            row.values["received_date"] = None

        if po_thing is not None:
            row.values["po_handle"] = _rep_handle(po_thing.rep)
            if fill(row, po_thing, "po_number", "po_number") is None:
                if fill(row, po_thing, "po_number", "sage_po_number") is None:
                    fill(row, po_thing, "po_number", "fm_po_number")
        else:
            row.values["po_handle"] = None
            row.values["po_number"] = None
        if row.values.get("po_number") is None and rec_thing is not None:
            if fill(row, rec_thing, "po_number", "po_reference") is None:
                fill(row, rec_thing, "po_number", "bc_order_number")

        product = name_linked(
            row, [e for m in thing.members for e in for_item.get(m.object_id, ())],
            pthings, "product")
        if product is not None:
            row.values["product_handle"] = _rep_handle(product.rep)
            if fill(row, product, "item_code", "code") is None:
                fill(row, product, "item_code", "erp_code")
        else:
            row.values["product_handle"] = None
            row.values["item_code"] = None

        _fill_num(row, thing, "received_quantity", "received_quantity")
        qcell, qrec = pick(thing, "received_quantity")
        unit_val = _text(qcell.unit) if qcell is not None and qcell.unit else None
        if unit_val is not None and qcell is not None:
            row.read(qcell, "unit", unit_val, rec=qrec)
        else:
            row.values["unit"] = None

        money(row, "unit_cost", *pick(thing, "bc_unit_cost"), None)
        fill(row, thing, "line_type", "line_type")

        lot_thing = _single_linked(
            row, [e for m in thing.members for e in rl_to_lot.get(m.object_id, ())], lot_by_id)
        if lot_thing is not None:
            row.values["lot_handle"] = _rep_handle(lot_thing.rep)
            if fill(row, lot_thing, "lot_number", "lot_number") is None:
                fill(row, lot_thing, "lot_number", "lot_text")
        else:
            row.values["lot_handle"] = None
            row.values["lot_number"] = None
        if row.values.get("lot_number") is None:
            fill(row, thing, "lot_number", "supplier_lot_number")

        fill(row, thing, "lab_id", "lab_id")
        fill(row, thing, "expiry_date", "expiry_date", to_date)
        row.values["pilot"] = all(pilot_binding(m.binding) for m in thing.members)
        yield row


# --------------------------------------------------------------------------- quotes

QUOTE_PROPS = (
    "quote_number", "status", "issue_date", "valid_until",
    "total_amount", "currency",
)
FQ_PROPS = (
    "document_number", "document_date", "valid_until",
    "buyer", "seller", "amount", "po_reference", "tier",
)
QUOTE_CUSTOMER_LINK = "Quote.for_customer"
FQ_SO_LINK = "ForwarderQuote.for_sales_order"
SO_NUM_PROPS = ("order_number", "sage_order_number", "bc_number")


@view("quotes",
      "Every customer quote (Portal) and forwarder quote (email/SharePoint), with party, "
      "status, dates, total, and order ref.",
      [("quote_number", "text", "quote number (Quote.quote_number, else ForwarderQuote.document_number)"),
       ("quote_kind", "text", "quote kind: customer (Portal Quote) or forwarder (ForwarderQuote)"),
       ("party", "text", "customer name (via Quote.for_customer), else buyer or seller"),
       ("status", "text", "quote status (Quote.status, else ForwarderQuote.tier)"),
       ("quote_date", "date", "issue or document date"),
       ("valid_until", "date", "quote expiry date, when stated"),
       ("total", "money", "quote total in its currency, when stated"),
       ("order_number", "text", "linked sales order number, else po_reference"),
       ("customer_handle", "text", "handle of linked customer (join customers on handle)"),
       ("order_handle", "text", "handle of linked sales order (join sales_orders on handle)")],
      reads=("Quote", "ForwarderQuote", "Customer", "SalesOrder"),
      memos=("customers",),
      covers=False)
def _quotes(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    q_recs = rd.records("Quote", QUOTE_PROPS)
    fq_recs = _doc_records(rd, "ForwarderQuote", FQ_PROPS)
    if not q_recs and not fq_recs:
        return

    if q_recs:
        customers, cthings = customer_things(rd)
        for_cust = rd.linked(QUOTE_CUSTOMER_LINK, q_recs, customers)
        for thing in rd.clusters("Quote", q_recs):
            row = Row(thing)
            fill(row, thing, "quote_number", "quote_number")
            row.values["quote_kind"] = "customer"
            cust = name_linked(
                row, [e for m in thing.members for e in for_cust.get(m.object_id, ())],
                cthings, "party")
            party_handle(row, "customer", cust)
            fill(row, thing, "status", "status")
            fill(row, thing, "quote_date", "issue_date", to_date)
            fill(row, thing, "valid_until", "valid_until", to_date)
            money(row, "total", *pick(thing, "total_amount"), pick(thing, "currency"))
            row.values["order_number"] = None
            row.values["order_handle"] = None
            yield row

    if fq_recs:
        so_recs = rd.records("SalesOrder", SO_NUM_PROPS)
        so_things = rd.clusters("SalesOrder", so_recs)
        so_by_id = {r.object_id: t for t in so_things for r in t.members}
        fq_to_so = _doc_linked(rd, FQ_SO_LINK, fq_recs, so_recs)
        for thing in _doc_clusters(rd, "ForwarderQuote", fq_recs):
            row = Row(thing)
            fill(row, thing, "quote_number", "document_number")
            row.values["quote_kind"] = "forwarder"
            if fill(row, thing, "party", "buyer") is None:
                fill(row, thing, "party", "seller")
            row.values["customer_handle"] = None
            fill(row, thing, "status", "tier")
            fill(row, thing, "quote_date", "document_date", to_date)
            fill(row, thing, "valid_until", "valid_until", to_date)
            money(row, "total", *pick(thing, "amount"), None)
            so_thing = _single_linked(
                row, [e for m in thing.members for e in fq_to_so.get(m.object_id, ())], so_by_id)
            if so_thing is not None:
                row.values["order_handle"] = _rep_handle(so_thing.rep)
                if fill(row, so_thing, "order_number", "order_number") is None:
                    if fill(row, so_thing, "order_number", "sage_order_number") is None:
                        fill(row, so_thing, "order_number", "bc_number")
            else:
                row.values["order_handle"] = None
                row.values["order_number"] = None
            if row.values.get("order_number") is None:
                fill(row, thing, "order_number", "po_reference")
            yield row


# --------------------------------------------------------------------------- freight_bills

FREIGHT_BILL_PROPS = (
    "invoice_number", "tracking_number", "carrier", "shipper", "consignee",
    "tier", "document_date", "ship_date", "eta", "amount", "po_reference", "incoterm",
)
FB_SO_LINK = "FreightBill.for_sales_order"
FB_PO_LINK = "FreightBill.for_purchase_order"


@view("freight_bills",
      "Every freight bill (email/SharePoint), with bill and tracking numbers, carrier, "
      "shipper, consignee, dates, amount, and order ref.",
      [("bill_number", "text", "freight bill or invoice number as stated"),
       ("tracking_number", "text", "carrier tracking or PRO number as stated"),
       ("carrier", "text", "carrier name as stated"),
       ("shipper", "text", "shipper name as stated"),
       ("consignee", "text", "consignee name as stated"),
       ("status", "text", "extraction tier as stated"),
       ("bill_date", "date", "freight bill document date"),
       ("ship_date", "date", "ship date, when stated"),
       ("eta", "date", "estimated arrival date, when stated"),
       ("amount", "money", "freight bill amount in its currency, when stated"),
       ("po_reference", "text", "PO or order reference as stated (else linked PO/SO number)"),
       ("incoterm", "text", "incoterm, when stated"),
       ("order_handle", "text", "handle of linked sales order (join sales_orders on handle)"),
       ("po_handle", "text", "handle of linked purchase order (join purchase_orders on handle)")],
      reads=("FreightBill", "SalesOrder", "PurchaseOrder"),
      covers=False)
def _freight_bills(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs = _doc_records(rd, "FreightBill", FREIGHT_BILL_PROPS)
    if not recs:
        return
    fb_things = _doc_clusters(rd, "FreightBill", recs)

    so_recs = rd.records("SalesOrder", SO_NUM_PROPS)
    so_things = rd.clusters("SalesOrder", so_recs)
    so_by_id = {r.object_id: t for t in so_things for r in t.members}
    fb_to_so = _doc_linked(rd, FB_SO_LINK, recs, so_recs)

    po_recs = rd.records("PurchaseOrder", PO_PROPS)
    po_things = rd.clusters("PurchaseOrder", po_recs)
    po_by_id = {r.object_id: t for t in po_things for r in t.members}
    fb_to_po = _doc_linked(rd, FB_PO_LINK, recs, po_recs)

    for thing in fb_things:
        row = Row(thing)
        fill(row, thing, "bill_number", "invoice_number")
        fill(row, thing, "tracking_number", "tracking_number")
        fill(row, thing, "carrier", "carrier")
        fill(row, thing, "shipper", "shipper")
        fill(row, thing, "consignee", "consignee")
        fill(row, thing, "status", "tier")
        fill(row, thing, "bill_date", "document_date", to_date)
        fill(row, thing, "ship_date", "ship_date", to_date)
        fill(row, thing, "eta", "eta", to_date)
        money(row, "amount", *pick(thing, "amount"), None)
        fill(row, thing, "incoterm", "incoterm")

        so_thing = _single_linked(
            row, [e for m in thing.members for e in fb_to_so.get(m.object_id, ())], so_by_id)
        row.values["order_handle"] = _rep_handle(so_thing.rep) if so_thing is not None else None

        po_thing = _single_linked(
            row, [e for m in thing.members for e in fb_to_po.get(m.object_id, ())], po_by_id)
        row.values["po_handle"] = _rep_handle(po_thing.rep) if po_thing is not None else None

        if fill(row, thing, "po_reference", "po_reference") is None:
            if po_thing is not None:
                if fill(row, po_thing, "po_reference", "po_number") is None:
                    if fill(row, po_thing, "po_reference", "sage_po_number") is None:
                        fill(row, po_thing, "po_reference", "fm_po_number")
            elif so_thing is not None:
                if fill(row, so_thing, "po_reference", "order_number") is None:
                    if fill(row, so_thing, "po_reference", "sage_order_number") is None:
                        fill(row, so_thing, "po_reference", "bc_number")
        yield row
