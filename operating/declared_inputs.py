"""Each store-only proactive rule's declared inputs (``operating.proactive_incremental``).

For every rule an incremental pass judges: the types its gaps are about, every link type
it reads (from its own code and the modules it reads through: ``po_status.StoreSource``
for the purchase order rules), how many hops from a subject they reach, the identity links
whose clusters it reads whole and the links to shared records (a supplier, an item) it
reads but never walks through. What a rule reads over every record of a type (number
collisions, default dates, hold profiles, money medians, test-record series) is not here:
those are populations (``proactive_engine._Store.population``), read whole on every pass
and compared with the rule's last fold, so a moved population judges the rule whole.

A link type a rule reads that is missing here makes an incremental pass differ from a full
run; operating/tests/test_proactive_incremental.py compares them on every change it seeds.
"""

from __future__ import annotations

from typing import Any

from . import po_status as ps
from . import proactive_engine as pe
from . import sales_order_status as so_status

ANY = "*"   # a rule whose subject may be a record of any type (a cluster's root)

# Rules an incremental pass leaves whole: the vendor-mail rule reads the whole store's mail
# by conversation (judged whole on each new day and by the full pass, its claims wait a
# week), the customer-PO rule reads the Portal mirror (the full pass).
WHOLE: tuple[str, ...] = ()
NOT_INCREMENTAL = ("vendor_no_reply", "order_without_customer_po")

# Every rule's truth or wording moves with the business day (days late, expiry windows,
# revision windows, the test-record filter's old drafts): a new day judges them whole.
DATED = tuple(d for d in pe.DETECTORS if d in pe.SET_BASED)


def _ps_links() -> tuple[str, ...]:
    """What ``po_status.StoreSource`` and ``po_status.read`` read from an order."""
    return tuple(dict.fromkeys([
        *ps.TWIN_LINKS, ps.LINE_OF, ps.RECEIPT_RECEIVES_LINE, ps.QC_RECEIVES_LINE,
        ps.RECEIVED_LOT, *ps.CONFIRMATION_LINKS]))


def inputs() -> dict[str, Any]:
    from .proactive_gaps import _roster
    from .proactive_incremental import Inputs

    order = _ps_links()
    po_twins = tuple(ps.TWIN_LINKS)
    identity = pe._identity_link_types()
    quotes = tuple(_roster()[1]) or ("Quote",)   # the quote types declaring valid_until
    return {
        # the order across its records and systems: twins (settled, firm) whole, lines,
        # FileMaker receipts and their lots, confirmations and supplier invoices
        "late_or_unconfirmed_po": Inputs(("PurchaseOrder",), order, 3, po_twins),
        "late_customer_order": Inputs(
            ("SalesOrder",), (*so_status.FULFILLED_LINKS, so_status.LINE_OF), 1,
            tuple(so_status.twin_links())),
        "po_receipt_quantity_mismatch": Inputs(
            ("PurchaseOrderLine",), (*order, *pe.RECEIPT_LINKS), 4,
            (*po_twins, "ReceiptLine.same_record_as")),
        "receipt_without_qc": Inputs(
            ("Receipt",), ("ReceiptLine.line_of", *pe.declared_qc_links()), 2),
        "expired_quote": Inputs(quotes, (), 0),
        "conflicting_quantity": Inputs((ANY,), (), 0, identity),
        "late_order": Inputs(
            ("PurchaseOrder",), (*order, pe.CONFIRMATION_LINK), 4, po_twins,
            hubs=("PurchaseOrder.ordered_from",)),
        "eta_slipped": Inputs(("PurchaseOrder",), (*order, "PurchaseOrderLine.line_of",
                                                   pe.CONFIRMATION_LINK), 3, po_twins),
        "lot_attention": Inputs(("Lot", "Batch"), (), 1, ("Batch.same_record_as",)),
        "accepted_quote_no_order": Inputs(
            ("Quote",), ("Quote.converted_to", "SalesOrder.created_from_quote"), 1),
        "invoice_quantity_mismatch": Inputs(
            ("PurchaseOrder",), (*order, "PurchaseInvoice.invoices",
                                 "PurchaseInvoiceLine.line_of"), 3, po_twins,
            hubs=("PurchaseInvoiceLine.for_item", "PurchaseOrderLine.for_item")),
    }
