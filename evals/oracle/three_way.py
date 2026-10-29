"""The oracle's own reading of a purchase order's three-way state (stratum ``po_three_way``).

The unfamiliar sample asks the product (``plx.process``, process
``purchase_to_invoice``) where a sampled purchase order stands and whether its sides agree,
and scores the answer against this reading. It is computed from the raw source reads only
(Portal rows and BC documents through the injected ports, joined on ``keys.EDGES`` and the
F4 lookups of ``families``), so it shares no code with the projection or ``plx.process``.

One reading per root, aspects as enums (no value leaves this module):

* ``stage:purchase_order``: ``present`` unless the PO status is ``DRAFT`` (``absent``: not
  issued) or cancelled (undecided: a cancelled PO is no stage at all), as
  derivePoFulfillmentCode reads it;
* ``stage:receipt``: ``present`` when the PO has a Portal receipt (``app_receipt``).
  Otherwise undecided: a twin system (FM, Sage, a BC posted receipt) may hold one, and
  this reading does not read them;
* ``stage:vendor_invoice``: ``present`` when a BC purchase invoice of the PO's own vendor
  (found as Portal finds it: by order No. and by the vendor invoice Nos. on its receipts)
  is neither a draft nor cancelled; ``absent`` when the complete BC read finds none, or
  only drafts/cancelled ones; undecided on any BC gap (vendor unverifiable, company span,
  cut-off read) or without BC;
* ``side:ordered_received``: per item (Portal product code), received + 0.0001 >= ordered
  (Portal po-fulfillment-status.ts EPS, one-sided); compared only once the receipt stage is
  present;
* ``side:received_invoiced``: per item, Portal received equals the counted BC invoice item
  lines (BC item No. = product code, BC unit code), exactly; only once both stages are
  present.

A side is ``True`` when every item is decided and agrees, ``False`` when any decided item
disagrees, undecided otherwise. An item with lines on one side only, mixed or missing
units, an empty quantity or an unresolved code is undecided, never zero. Each aspect names
the ``sources`` (relations, ``bc:`` entity sets) its evidence lives in, so the scorer can
tell an aspect the product never captured from one it got wrong. A root the oracle cannot
read (not exactly one PO, a failed read) is ``blocked`` with a reason.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

from . import keys as K
from .families import Context, Node, Sources, po_invoices
from .source import decimal_of, key_text

STAGES = ("purchase_order", "receipt", "vendor_invoice")
SIDES = ("ordered_received", "received_invoiced")
ASPECTS = tuple(f"stage:{s}" for s in STAGES) + tuple(f"side:{s}" for s in SIDES)
PO_NOT_ISSUED = frozenset({"DRAFT"})
PO_IGNORED = frozenset({"CANCELLED", "CANCELED", "CANCEL"})
INVOICE_NOT_COUNTED = frozenset({"DRAFT", "CANCELED", "CANCELLED"})
RECEIVE_EPS = Decimal("0.0001")
BC_INVOICES = "bc:purchaseInvoices"
# Gap reasons that leave a BC-side aspect undecided (identity or completeness unproven).
_BC_DOUBT = ("bc_vendor_unverifiable", "bc_match_spans_companies", "bc_read_incomplete",
             "bc_not_read")


def _aspect(value: Any, reason: str, sources: Sequence[str]) -> dict[str, Any]:
    return {"value": value, "reason": reason, "sources": sorted(set(sources))}


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _bc_doubt(ctx: Context) -> str | None:
    for gap in ctx.gaps:
        if gap["reason"] in _BC_DOUBT or (gap["reason"].startswith("read_failed_")
                                          and gap["subject"].startswith("bc:")):
            return gap["reason"]
    return None


def _portal_failed(ctx: Context) -> dict[str, Any] | None:
    """The first failed Portal read: its gap reason and ``ReadFailed.fields`` (blocked,
    HTTP status, error code)."""
    for gap in ctx.gaps:
        if gap["state"] == "inaccessible" and not gap["subject"].startswith("bc:"):
            return {"reason": gap["reason"], **(ctx.portal.failure(gap["subject"]) or {})}
    return None


def _items(ctx: Context, nodes: Sequence[Node], code_of: Any, unit_of: Any, qty: str
           ) -> dict[str | None, list[tuple[str | None, Decimal | None]]]:
    out: dict[str | None, list[tuple[str | None, Decimal | None]]] = {}
    for node in nodes:
        out.setdefault(code_of(node), []).append((unit_of(node), decimal_of(node.row.get(qty))))
    return out


def _total(entries: Sequence[tuple[str | None, Decimal | None]]
           ) -> tuple[str, Decimal] | None:
    """(unit, exact sum) of one item's lines: None on a missing/mixed unit or an empty
    quantity (never summed as zero)."""
    units = {u for u, _ in entries}
    if len(units) != 1 or None in units or any(q is None for _, q in entries):
        return None
    return units.pop(), sum((q for _, q in entries), Decimal(0))  # type: ignore[misc]


def _side(a: Mapping[str | None, list], b: Mapping[str | None, list], agree: Any
          ) -> tuple[bool | None, str]:
    """PO-level verdict over the items of both sides (see module docstring)."""
    verdicts: list[bool | None] = []
    for code in sorted(set(a) | set(b), key=lambda c: (c is None, c or "")):
        left = _total(a.get(code, [])) if code is not None and code in a else None
        right = _total(b.get(code, [])) if code is not None and code in b else None
        if left is None or right is None or left[0] != right[0]:
            verdicts.append(None)
        else:
            verdicts.append(bool(agree(left[1], right[1])))
    if False in verdicts:
        return False, "item_disagrees"
    if verdicts and all(v is True for v in verdicts):
        return True, "every_item_agrees"
    return None, "item_undecided" if verdicts else "no_lines"


def reading(sources: Sources, relation: str, key: Sequence[tuple[str, Any]]
            ) -> dict[str, Any]:
    """``{"status": "read"|"blocked", "reason", "aspects": {aspect: {"value", "reason",
    "sources"}}}`` for the Portal purchase order ``relation``/``key``."""
    if relation != K.PO:
        return {"status": "blocked", "reason": "root_not_a_portal_po", "aspects": {}}
    ctx = Context(sources, {})
    column, value = next(iter(key), ("id", None))
    orders = ctx.portal_lookup(K.PO, column, value)
    if len(orders) != 1:
        return {"status": "blocked", "aspects": {},
                **(_portal_failed(ctx) or {"reason": "root_not_found" if not orders
                                           else "root_not_unique"})}
    order = orders[0]
    lines = ctx.portal_lookup(K.PO_LINE, "purchase_order_id", order.row.get("id"))
    receipts = ctx.portal_lookup(K.RECEIPT, "purchase_order_id", order.row.get("id"))
    receipt_lines = [line for r in receipts
                     for line in ctx.portal_lookup(K.RECEIPT_LINE, "receipt_id", r.row.get("id"))]
    failed = _portal_failed(ctx)
    if failed:
        return {"status": "blocked", "aspects": {}, **failed}

    def code(node: Node) -> str | None:
        products = ctx.portal_lookup(K.PRODUCT, "id", node.row.get("product_id"))
        return key_text(products[0].row.get("code")) if len(products) == 1 else None

    def portal_unit(node: Node) -> str | None:
        return K.bc_unit_code(node.row.get("uom"))

    aspects: dict[str, dict[str, Any]] = {}
    status = _upper(order.row.get("status"))
    aspects["stage:purchase_order"] = _aspect(
        None if status in PO_IGNORED else ("absent" if status in PO_NOT_ISSUED else "present"),
        "po_cancelled" if status in PO_IGNORED else
        ("po_draft" if status in PO_NOT_ISSUED else "po_issued"), [K.PO])
    aspects["stage:receipt"] = _aspect("present" if receipts else None,
                                       "portal_receipt" if receipts else "twin_receipts_not_read",
                                       [K.RECEIPT])

    invoice_lines: list[Node] = []
    if sources.bc is None:
        aspects["stage:vendor_invoice"] = _aspect(None, "bc_not_read", [BC_INVOICES])
    else:
        invoices, _ = po_invoices(ctx, order, receipts, 1)
        counted = [n for n in invoices.values()
                   if _upper(n.row.get("status")) not in INVOICE_NOT_COUNTED]
        for node in counted:
            for line in ctx.bc_lines(node):
                if line["fields"].get("lineType") == "Item":
                    invoice_lines.append(Node("bc:purchaseInvoiceLines", line["fields"], line))
        doubt = _bc_doubt(ctx)
        if counted:
            value, reason = "present", "counted_invoice"
        elif doubt:
            value, reason = None, doubt
        else:
            value, reason = "absent", ("only_draft_or_cancelled" if invoices
                                       else "no_invoice_found")
        aspects["stage:vendor_invoice"] = _aspect(value, reason, [BC_INVOICES])

    ordered = _items(ctx, lines, code, portal_unit, "quantity")
    received = _items(ctx, receipt_lines, code, portal_unit, "quantity")
    if aspects["stage:receipt"]["value"] != "present":
        aspects["side:ordered_received"] = _aspect(None, "receipt_stage_not_present",
                                                   [K.PO_LINE, K.RECEIPT_LINE])
    else:
        value, reason = _side(ordered, received, lambda o, r: r + RECEIVE_EPS >= o)
        aspects["side:ordered_received"] = _aspect(value, reason, [K.PO_LINE, K.RECEIPT_LINE])
    if aspects["stage:receipt"]["value"] != "present" or \
            aspects["stage:vendor_invoice"]["value"] != "present":
        aspects["side:received_invoiced"] = _aspect(None, "stage_not_present",
                                                    [K.RECEIPT_LINE, BC_INVOICES])
    elif _bc_doubt(ctx):
        aspects["side:received_invoiced"] = _aspect(None, str(_bc_doubt(ctx)),
                                                    [K.RECEIPT_LINE, BC_INVOICES])
    else:
        invoiced = _items(ctx, invoice_lines, lambda n: key_text(n.row.get("lineObjectNumber")),
                          lambda n: key_text(n.row.get("unitOfMeasureCode")), "quantity")
        value, reason = _side(received, invoiced, lambda r, i: r == i)
        aspects["side:received_invoiced"] = _aspect(value, reason, [K.RECEIPT_LINE, BC_INVOICES])
    return {"status": "read", "reason": "read", "aspects": aspects}


def decided(entry: Mapping[str, Any]) -> list[str]:
    """The aspects this reading decides (a value other than None)."""
    return [name for name in ASPECTS
            if (entry.get("aspects") or {}).get(name, {}).get("value") is not None]


__all__ = ["ASPECTS", "SIDES", "STAGES", "decided", "reading"]
