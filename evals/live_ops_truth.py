"""The proactive-truth key: how many purchase orders are really late, once every captured
system's own records are read (``evals.oracle.po_truth`` over the lateness oracle's late
groups).

The lateness oracle reads the governing record's status and dates the way the product's
late rule does, so a BC order a migration left open, dated by its import day, is late
there although FileMaker received its goods long ago. This key keeps only the late groups
no raw record contradicts: a FileMaker receipt or released lot (or a closed item), a Sage
completion, BC posted receipt lines, a cancellation, a test mark, an old draft twin, or a
due date that is a bulk or record-write default. A reply counting the others as late is
scored wrong.

Independent of the Workbench: this file imports only the stdlib and ``evals.oracle`` (the
import ban covers it, ``KEY_FILES`` in ``scripts/check_oracle_imports.py``) and shares no
rule with the product's truth check. ``evals.live_ops_keys`` wraps the key as a card family.
"""

from __future__ import annotations

from typing import Any

from evals.oracle import po_truth

PORTAL_RELATIONS = po_truth.RELATIONS
BC_SETS = po_truth.BC_SETS
RULE = ("the lateness oracle's late order groups (po_lateness.late_orders) less those a raw "
        "record contradicts (po_truth.judge): every live FileMaker item received (receipt "
        "row, QC lot with a status, or a closing item word) or cancelled; Sage complete; BC "
        "posted receipt lines covering every ordered line; a Portal cancellation or year-old "
        "draft; a test, UAT or demo mark; a due date that is a bulk day of BC line dates or "
        "the BC record's own last-modified day")
READS = ["po_lateness.late_orders (as late_po_count)",
         "po_truth over the same captured rows: FileMaker generated_purchase_order_item, "
         "receipt, receipt_raw_material, receipt_packaging; Sage po_header; Portal "
         "app_purchase_order; BC purchaseOrders, purchaseOrderLines, purchaseReceipts, "
         "purchaseReceiptLines, purchaseInvoices"]


def truth_key(world: Any) -> dict[str, Any] | None:
    """The really-late count, the records it rests on, and each group's verdict; None when
    the lateness oracle finds no late group (nothing to correct)."""
    groups = world.lateness()["late"]
    if not groups:
        return None
    verdicts = po_truth.really_late(world.portal, world.bc, groups, world.as_of)
    records = sorted({h["citation"] for g in groups for h in g["headers"]}
                     | {c for v in verdicts for c in v["cites"]})
    return {"value": sum(1 for v in verdicts if v["late"]), "records": records,
            "verdicts": verdicts}
