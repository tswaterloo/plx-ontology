"""Typed supply-chain views (PLAN-A18 Part 2): ``biz.receipts``, ``biz.assembly_orders``,
and ``biz.lots`` — one row per real thing (Portal, FileMaker, Sage and Business Central
copies joined by settled identity links), plain columns, door v2 permissions and lineage on
every row.

Registered by ``operating.pg.business_views`` after its own helpers and
``business_views_supply`` (import ``business_views``, never this module first)."""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal
from typing import Iterator, Mapping, Sequence

from operating.pg.business_views import (
    PILOT_DOC, Reader, Rec, Row, Thing, _rep_handle, _text, fill, handle_col,
    name_linked, party_handle, pick, pilot_binding, to_currency, to_date, to_decimal, view)
from operating.pg.business_views_supply import (
    PO_PROPS, hold_of, product_things, supplier_things)

# --------------------------------------------------------------------------- shared helpers


def _single_linked(row: Row, ends: Sequence[tuple[str, str]],
                   things: Mapping[str, Thing]) -> Thing | None:
    """Resolve a single uniquely linked ``Thing`` from ``ends`` (``(target_oid, link_marks)``),
    folding its link markings and member audience keys into ``row``."""
    linked: dict[str, Thing] = {}
    link_marks: set[str] = set()
    for target_oid, lmarks in ends:
        t = things.get(target_oid)
        if t is not None:
            linked[t.rep.object_id] = t
            link_marks.add(lmarks)
    if len(linked) != 1:
        return None
    thing = next(iter(linked.values()))
    for lm in link_marks:
        row.marks.add(lm)
    row.add_thing(thing)
    return thing


def _fill_num(row: Row, thing: Thing, col: str, prop: str) -> Decimal | None:
    """Fill a numeric column from ``prop`` (using ``cell.num`` when present, else ``to_decimal``)."""
    cell, crec = pick(thing, prop)
    val = to_decimal(cell.num if cell is not None and cell.num is not None
                     else cell.value if cell is not None else None)
    row.read(cell, col, val, rec=crec)
    return val


def _merge_marks(raw_marks: Sequence[str]) -> str | None:
    merged: set[str] = set()
    for raw in raw_marks:
        if not raw or raw == "[]":
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = [raw]
        if isinstance(parsed, list):
            merged.update(str(m) for m in parsed if m)
        elif parsed:
            merged.add(str(parsed))
    return json.dumps(sorted(merged)) if merged else None


# --------------------------------------------------------------------------- receipts

RECEIPT_PROPS = (
    "sage_receipt_number", "bc_number", "received_date", "fm_status",
    "po_reference", "bc_order_number", "vendor_number", "currency",
)
RECEIPT_PO_LINK = "Receipt.receives"
RECEIPT_SUPPLIER_LINK = "Receipt.supplied_by"
RECEIPT_LINE_LINK = "ReceiptLine.line_of"
PO_SUPPLIER_LINK = "PurchaseOrder.ordered_from"


@view("receipts", "Every purchase receipt once (Sage PO, FileMaker and Business Central "
      "copies joined by settled identity links), with its received date, status, linked "
      "purchase order, supplier, currency, and line count / total received quantity. "
      "Business Central-only receipts carry pilot = true.",
      [("receipt_number", "text", "receipt number (Sage sage_receipt_number, else BC bc_number)"),
       ("received_date", "date", "the date the receipt was received"),
       ("status", "text", "receipt status as stated (FileMaker fm_status)"),
       ("po_number", "text", "purchase order number (from linked PO via Receipt.receives, "
        "else Sage po_reference, else BC bc_order_number)"),
       ("supplier", "text", "supplier name (via Receipt.supplied_by, else the linked PO's "
        "supplier via PurchaseOrder.ordered_from)"),
       ("vendor_number", "text", "vendor number as stated (BC vendor_number, else linked "
        "supplier's code)"),
       ("currency", "currency", "receipt currency, when stated"),
       ("line_count", "integer", "number of linked receipt lines (via ReceiptLine.line_of)"),
       ("total_received_qty", "numeric", "sum of received_quantity across linked receipt lines; "
        "NULL when no line states a numeric quantity"),
       ("pilot", "boolean", PILOT_DOC),
       ("po_handle", "text", "handle of the linked purchase order (join purchase_orders on "
        "handle), when uniquely linked"),
       handle_col("supplier", f"the receipt's {RECEIPT_SUPPLIER_LINK} link (else linked PO's)")],
      reads=("Receipt", "ReceiptLine", "PurchaseOrder", "Supplier"),
      memos=("suppliers",),
      covers=False)   # keep Receipt in generic records too so source measures keep working
def _receipts(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs = rd.records("Receipt", RECEIPT_PROPS)
    if not recs:
        return
    receipt_things = rd.clusters("Receipt", recs)
    suppliers, sthings = supplier_things(rd)
    supplied_by = rd.linked(RECEIPT_SUPPLIER_LINK, recs, suppliers)

    po_recs = rd.records("PurchaseOrder", PO_PROPS)
    po_things = rd.clusters("PurchaseOrder", po_recs)
    po_by_id = {r.object_id: t for t in po_things for r in t.members}
    receives_po = rd.linked(RECEIPT_PO_LINK, recs, po_recs)
    po_ordered_from = rd.linked(PO_SUPPLIER_LINK, po_recs, suppliers)

    line_recs = rd.records("ReceiptLine", ("received_quantity",))
    line_to_receipt = rd.linked(RECEIPT_LINE_LINK, line_recs, recs)
    lines_by_receipt: dict[str, list[tuple[Rec, str]]] = defaultdict(list)
    for loid, ends in line_to_receipt.items():
        lrec = line_recs[loid]
        for roid, lmarks in ends:
            lines_by_receipt[roid].append((lrec, lmarks))

    for thing in receipt_things:
        row = Row(thing)
        if fill(row, thing, "receipt_number", "sage_receipt_number") is None:
            fill(row, thing, "receipt_number", "bc_number")
        fill(row, thing, "received_date", "received_date", to_date)
        fill(row, thing, "status", "fm_status")
        fill(row, thing, "currency", "currency", to_currency)

        po_thing = _single_linked(
            row, [e for m in thing.members for e in receives_po.get(m.object_id, ())], po_by_id)
        if po_thing is not None:
            row.values["po_handle"] = _rep_handle(po_thing.rep)
            if fill(row, po_thing, "po_number", "po_number") is None:
                if fill(row, po_thing, "po_number", "sage_po_number") is None:
                    fill(row, po_thing, "po_number", "fm_po_number")
        else:
            row.values["po_handle"] = None
            row.values["po_number"] = None
        if row.values.get("po_number") is None:
            if fill(row, thing, "po_number", "po_reference") is None:
                fill(row, thing, "po_number", "bc_order_number")

        sup_ends = [e for m in thing.members for e in supplied_by.get(m.object_id, ())]
        sup = name_linked(row, sup_ends, sthings, "supplier")
        if sup is None and po_thing is not None:
            po_sup_ends = [e for m in po_thing.members
                           for e in po_ordered_from.get(m.object_id, ())]
            sup = name_linked(row, po_sup_ends, sthings, "supplier")
        party_handle(row, "supplier", sup)

        if fill(row, thing, "vendor_number", "vendor_number") is None and sup is not None:
            fill(row, sup, "vendor_number", "code")
        else:
            row.values.setdefault("vendor_number", None)

        seen_lines: dict[str, Rec] = {}
        qty_sum = Decimal(0)
        qty_any = False
        qty_marks: list[str] = []
        qty_cite: list[str] | None = None
        for m in thing.members:
            for lrec, lmarks in lines_by_receipt.get(m.object_id, ()):
                row.marks.add(lmarks)
                if lrec.object_id in seen_lines:
                    continue
                seen_lines[lrec.object_id] = lrec
                row.read_record(lrec.object_id, "*")
                cell = lrec.cell("received_quantity")
                if cell is not None:
                    val = to_decimal(cell.num if cell.num is not None else cell.value)
                    if val is not None:
                        qty_sum += val
                        qty_any = True
                        if qty_cite is None:
                            qty_cite = [cell.handle, cell.field_path]
                        if cell.marks not in ("", "[]"):
                            qty_marks.append(cell.marks)
        row.values["line_count"] = len(seen_lines)
        row.values["total_received_qty"] = qty_sum if qty_any else None
        if qty_any and qty_cite is not None:
            row.cites["total_received_qty"] = qty_cite
            merged = _merge_marks(qty_marks)
            if merged:
                row.col_marks["total_received_qty"] = merged

        row.values["pilot"] = all(pilot_binding(m.binding) for m in thing.members)
        yield row


# --------------------------------------------------------------------------- assembly_orders

ASSEMBLY_ORDER_PROPS = (
    "code", "mo_code", "ao_code", "status", "pick_request_status",
    "scheduled_date", "realized_date", "start_date",
    "expected_quantity", "ordered_pieces", "ordered_boxes", "sample_quantity",
    "lot_number", "created_by_account",
)
AO_PRODUCT_LINK = "AssemblyOrder.makes"
AO_BATCH_LINKS = ("AssemblyOrder.fills_from", "AssemblyOrder.for_batch")


@view("assembly_orders", "Every assembly / manufacturing order once (Portal and FileMaker "
      "copies joined by settled identity links), with the finished product it makes, status, "
      "scheduled and realized dates, quantities (expected, pieces, boxes, samples), and "
      "linked bulk batch / lot.",
      [("order_number", "text", "assembly order code (code, else mo_code, else ao_code)"),
       ("status", "text", "assembly order status as stated (Realized, RELEASED, Cancelled, ...)"),
       ("pick_request_status", "text", "pick request status as stated (Portal)"),
       ("product", "text", "finished product name (via AssemblyOrder.makes)"),
       ("item_code", "text", "finished product item code"),
       ("scheduled_date", "date", "scheduled production date"),
       ("realized_date", "date", "realized / completed production date"),
       ("start_date", "date", "production start date, when stated"),
       ("expected_quantity", "numeric", "expected production quantity"),
       ("ordered_pieces", "numeric", "ordered pieces"),
       ("ordered_boxes", "numeric", "ordered boxes"),
       ("sample_quantity", "numeric", "sample quantity"),
       ("batch_code", "text", "linked bulk batch code (via fills_from / for_batch)"),
       ("lot_number", "text", "lot number (order lot_number, else linked batch's lot_number)"),
       ("created_by", "text", "account that created the assembly order (FileMaker)"),
       ("product_handle", "text", "handle of the linked finished product (join products on handle)"),
       ("batch_handle", "text", "handle of the linked bulk batch (join batches on handle)")],
      reads=("AssemblyOrder", "Product", "Batch"),
      memos=("products",),
      covers=False)   # keep AssemblyOrder in generic records too so source measures keep working
def _assembly_orders(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs = rd.records("AssemblyOrder", ASSEMBLY_ORDER_PROPS)
    if not recs:
        return
    ao_things = rd.clusters("AssemblyOrder", recs)
    products, pthings = product_things(rd)
    makes = rd.linked(AO_PRODUCT_LINK, recs, products)

    batch_recs = rd.records("Batch", ("batch_code", "batch_number", "lot_number"))
    batch_things = rd.clusters("Batch", batch_recs)
    batch_by_id = {r.object_id: t for t in batch_things for r in t.members}
    batch_links = [rd.linked(lt, recs, batch_recs) for lt in AO_BATCH_LINKS]

    for thing in ao_things:
        row = Row(thing)
        if fill(row, thing, "order_number", "code") is None:
            if fill(row, thing, "order_number", "mo_code") is None:
                fill(row, thing, "order_number", "ao_code")
        fill(row, thing, "status", "status")
        fill(row, thing, "pick_request_status", "pick_request_status")

        product = name_linked(
            row, [e for m in thing.members for e in makes.get(m.object_id, ())],
            pthings, "product")
        if product is not None:
            row.values["product_handle"] = _rep_handle(product.rep)
            if fill(row, product, "item_code", "code") is None:
                fill(row, product, "item_code", "erp_code")
        else:
            row.values["product_handle"] = None
            row.values["item_code"] = None

        fill(row, thing, "scheduled_date", "scheduled_date", to_date)
        fill(row, thing, "realized_date", "realized_date", to_date)
        fill(row, thing, "start_date", "start_date", to_date)
        for col in ("expected_quantity", "ordered_pieces", "ordered_boxes", "sample_quantity"):
            _fill_num(row, thing, col, col)

        b_ends = [e for bm in batch_links for m in thing.members
                  for e in bm.get(m.object_id, ())]
        batch_thing = _single_linked(row, b_ends, batch_by_id)
        if batch_thing is not None:
            row.values["batch_handle"] = _rep_handle(batch_thing.rep)
            if fill(row, batch_thing, "batch_code", "batch_code") is None:
                fill(row, batch_thing, "batch_code", "batch_number")
        else:
            row.values["batch_handle"] = None
            row.values["batch_code"] = None

        if fill(row, thing, "lot_number", "lot_number") is None and batch_thing is not None:
            fill(row, batch_thing, "lot_number", "lot_number")
        else:
            row.values.setdefault("lot_number", None)

        fill(row, thing, "created_by", "created_by_account")
        yield row


# --------------------------------------------------------------------------- lots

LOT_PROPS = ("lot_number", "lot_text", "qc_status", "expiry_date")
LOT_PRODUCT_LINK = "Lot.of_item"
LOT_RECEIPT_LINK = "Lot.received_in"
LOT_QC_RL_LINK = "Lot.qc_decided_receipt_line"
RL_LOT_LINK = "ReceiptLine.received_lot"


@view("lots", "Every received raw-material and packaging lot once (FileMaker QC lot records "
      "joined by settled identity links), with its item, QC status (on_hold: QC holds or "
      "quarantines it), expiry date, lab ID, and linked receipt.",
      [("lot_number", "text", "lot number (lot_number, else lot_text)"),
       ("product", "text", "item name (via Lot.of_item)"),
       ("item_code", "text", "item code"),
       ("qc_status", "text", "QC status as stated (Finished, Released, Expired, Quarantine, "
        "Rejected)"),
       ("on_hold", "boolean", "from qc_status: true when it says hold or quarantine, false "
        "when it says released, empty for any other word"),
       ("expiry_date", "date", "lot expiry date (Lot.expiry_date, else linked receipt line's)"),
       ("received_date", "date", "receipt date (from linked receipt)"),
       ("receipt_number", "text", "linked receipt number (Sage sage_receipt_number, else BC "
        "bc_number)"),
       ("lab_id", "text", "QC lab identifier (from linked receipt line)"),
       ("product_handle", "text", "handle of the linked item (join products on handle)"),
       ("receipt_handle", "text", "handle of the linked receipt (join receipts on handle)")],
      reads=("Lot", "Product", "Receipt", "ReceiptLine"),
      memos=("products",),
      covers=False)   # keep Lot in generic records too so source measures keep working
def _lots(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs = rd.records("Lot", LOT_PROPS)
    if not recs:
        return
    lot_things = rd.clusters("Lot", recs)
    products, pthings = product_things(rd)
    of_item = rd.linked(LOT_PRODUCT_LINK, recs, products)

    rec_recs = rd.records("Receipt", ("sage_receipt_number", "bc_number", "received_date"))
    rec_things = rd.clusters("Receipt", rec_recs)
    rec_by_id = {r.object_id: t for t in rec_things for r in t.members}
    received_in = rd.linked(LOT_RECEIPT_LINK, recs, rec_recs)

    rl_recs = rd.records("ReceiptLine", ("lab_id", "expiry_date"))
    rl_by_id = {oid: Thing([r]) for oid, r in rl_recs.items()}
    qc_rl = rd.linked(LOT_QC_RL_LINK, recs, rl_recs)
    rl_to_lot = rd.linked(RL_LOT_LINK, rl_recs, recs)
    lot_from_rl: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for rl_oid, ends in rl_to_lot.items():
        for lot_oid, lmarks in ends:
            lot_from_rl[lot_oid].append((rl_oid, lmarks))
    rl_to_rec = rd.linked(RECEIPT_LINE_LINK, rl_recs, rec_recs)

    for thing in lot_things:
        row = Row(thing)
        if fill(row, thing, "lot_number", "lot_number") is None:
            fill(row, thing, "lot_number", "lot_text")

        product = name_linked(
            row, [e for m in thing.members for e in of_item.get(m.object_id, ())],
            pthings, "product")
        if product is not None:
            row.values["product_handle"] = _rep_handle(product.rep)
            if fill(row, product, "item_code", "code") is None:
                fill(row, product, "item_code", "erp_code")
        else:
            row.values["product_handle"] = None
            row.values["item_code"] = None

        qc = fill(row, thing, "qc_status", "qc_status")
        row.values["on_hold"] = hold_of(qc)
        if "qc_status" in row.cites:
            row.cites["on_hold"] = row.cites["qc_status"]
            if "qc_status" in row.col_marks:
                row.col_marks["on_hold"] = row.col_marks["qc_status"]

        rl_ends = [e for m in thing.members for e in qc_rl.get(m.object_id, ())]
        if not rl_ends:
            rl_ends = [e for m in thing.members for e in lot_from_rl.get(m.object_id, ())]
        rl_thing = _single_linked(row, rl_ends, rl_by_id)

        if fill(row, thing, "expiry_date", "expiry_date", to_date) is None and rl_thing is not None:
            fill(row, rl_thing, "expiry_date", "expiry_date", to_date)
        else:
            row.values.setdefault("expiry_date", None)

        if rl_thing is not None:
            fill(row, rl_thing, "lab_id", "lab_id")
        else:
            row.values["lab_id"] = None

        rec_ends = [e for m in thing.members for e in received_in.get(m.object_id, ())]
        if not rec_ends and rl_thing is not None:
            rec_ends = [e for m in rl_thing.members for e in rl_to_rec.get(m.object_id, ())]
        rec_thing = _single_linked(row, rec_ends, rec_by_id)
        if rec_thing is not None:
            row.values["receipt_handle"] = _rep_handle(rec_thing.rep)
            fill(row, rec_thing, "received_date", "received_date", to_date)
            if fill(row, rec_thing, "receipt_number", "sage_receipt_number") is None:
                fill(row, rec_thing, "receipt_number", "bc_number")
        else:
            row.values["receipt_handle"] = None
            row.values["received_date"] = None
            row.values["receipt_number"] = None

        yield row
