"""Item cross-references: a supplier's own item number <-> our item <-> its Portal and BC
records, so a supplier's mail that names its item number maps to our item and to the open
PO it is about.

* **Vendor number -> our item** (``vendor_items``): the ``SupplierItem`` records (Portal
  ``app_supplier_product`` / FileMaker supplier rows) whose ``supplier_item_number`` is
  exactly the token (the store's value word index, then an exact match after dropping
  punctuation and case) *and* whose supplier is one of the mail's supplier records (with
  their twins). A number another supplier also uses never maps through the wrong supplier;
  a token naming more than ``MAX_ITEMS`` items is ambiguous and left out.
* **Our item <-> Portal item <-> BC item** (``xref``): the item record and its twins
  (``Product.same_record_as``), each side's item number (``code``, ``erp_code``) and the
  binding it came from, plus every supplier number on file for it.
* **Item -> the supplier's open PO** (``open_pos_for_items``): the open POs ordered from
  those suppliers that carry a line for one of the items.

Every record is read through the handler's context (``ctx.read`` / ``ctx.linked``), so a
prepared item carries the audiences and markings of each cross-reference it used (a
``SupplierItem`` is finance marked: an item built on it shows only to a finance reader).
No rule names a supplier, item or person.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from . import autopilot as ap

SUPPLIER_ITEM_OF = "SupplierItem.offered_by"
SUPPLIER_ITEM_FOR = "SupplierItem.for_item"
PRODUCT_TWIN = "Product.same_record_as"
SUPPLIER_TWIN = "Supplier.same_record_as"
PO_SUPPLIER = "PurchaseOrder.ordered_from"
MAX_TOKENS = 12
MAX_ITEMS = 3
MIN_CHARS = 4


def _norm(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


def candidate_tokens(*texts: str) -> list[str]:
    """Tokens that could be a supplier's item number: at least ``MIN_CHARS`` letters or
    digits with one digit, not a year or a date."""
    out = []
    for t in texts:
        for m in ap.TOKEN.finditer(t or ""):
            tok = m.group(1).strip("-")
            if len(_norm(tok)) >= MIN_CHARS and any(c.isdigit() for c in tok) and \
                    not re.fullmatch(r"(?:19|20)\d{2}(?:-\d{1,2}){0,2}", tok):
                out.append(tok)
    return list(dict.fromkeys(out))[:MAX_TOKENS]


def with_twins(ctx: Any, oids: Iterable[str], link: str) -> set[str]:
    oids = set(oids)
    return oids | set(ctx.linked_many(oids, (link,))) if oids else set()


def mail_suppliers(ctx: Any, event: Mapping[str, Any]) -> set[str]:
    """The supplier records a message is from: its ``sent_by_supplier`` link, else the
    supplier records stating the sender's address (else its domain), with their twins."""
    linked = [o for o in ctx.linked(event["object_id"], ("Email.sent_by_supplier",))
              if o.startswith("Supplier:")]
    if not linked and event.get("sender"):
        linked = [o for side, o in ap._parties(ctx.st, str(event["sender"]))
                  if side == "supplier"]
    return with_twins(ctx, linked, SUPPLIER_TWIN)


def vendor_items(ctx: Any, suppliers: Iterable[str], tokens: Sequence[str]
                 ) -> dict[str, list[str]]:
    """token -> our item records its supplier number names (module doc); a token that maps
    to none, or to more than ``MAX_ITEMS``, is left out."""
    suppliers = set(suppliers)
    if not suppliers:
        return {}
    out: dict[str, list[str]] = {}
    for tok in tokens:
        hits = ctx.find("SupplierItem", "supplier_item_number", tok)
        if not hits:
            continue
        offered = {a: b for _lt, a, b in ctx.touching(hits, (SUPPLIER_ITEM_OF,))}
        mine = [h for h in hits if offered.get(h) in suppliers]
        if not mine:
            continue
        rows = ctx.read(mine, ("supplier_item_number",))
        items: set[str] = set()
        for _lt, a, b in ctx.touching(mine, (SUPPLIER_ITEM_FOR,)):
            if a in mine and b.startswith("Product:"):
                items.add(b)
                ctx.cite(rows.get(a), "supplier_item_number")
        items = with_twins(ctx, items, PRODUCT_TWIN)
        if items and len({min(_twin_group(ctx, i)) for i in items}) <= MAX_ITEMS:
            out[tok] = sorted(items)
    return out


def _twin_group(ctx: Any, item: str) -> set[str]:
    return with_twins(ctx, [item], PRODUCT_TWIN)


def xref(ctx: Any, item: str) -> dict[str, Any]:
    """Our item's numbers on every side and the supplier numbers on file for it."""
    group = sorted(_twin_group(ctx, item))
    rows = ctx.read(group, ("code", "erp_code", "name"))
    sides = []
    for oid in group:
        row = rows.get(oid) or {}
        binding = oid.split(":", 2)[1] if oid.count(":") >= 2 else ""
        sides.append({"object_id": oid, "system": binding.split(".", 1)[0] or None,
                      "code": ap._v(row, "code") or None,
                      "erp_code": ap._v(row, "erp_code") or None})
    offers = [a for _lt, a, b in ctx.touching(group, (SUPPLIER_ITEM_FOR,))
              if a.startswith("SupplierItem:")]
    srows = ctx.read(offers[:50], ("supplier_item_number",))
    by = {a: b for _lt, a, b in ctx.touching(offers[:50], (SUPPLIER_ITEM_OF,))}
    vendor = sorted({(ap._v(r, "supplier_item_number"), by.get(o, ""))
                     for o, r in srows.items() if ap._v(r, "supplier_item_number")})
    return {"items": sides,
            "vendor_numbers": [{"number": n, "supplier": s or None} for n, s in vendor]}


def open_pos_for_items(ctx: Any, suppliers: Iterable[str], items: Iterable[str]
                       ) -> list[str]:
    """The open POs from ``suppliers`` with a line for one of ``items`` (twins of one PO
    are one PO: the first kept)."""
    from . import autopilot_work as aw
    suppliers = set(suppliers)
    lines = ctx.linked_many(items, (ap.PO_LINE_ITEM,))
    lines = [x for x in lines if x.startswith("PurchaseOrderLine:")][:ap.MAX_LINES * 4]
    pos = sorted({b for _lt, a, b in ctx.touching(lines, (ap.PO_LINE_OF,))
                  if b.startswith("PurchaseOrder:")})
    if not pos:
        return []
    from_supplier = {a for _lt, a, b in ctx.touching(pos, (PO_SUPPLIER,)) if b in suppliers}
    heads = ctx.read(sorted(from_supplier), aw.PO_HEAD)
    out, numbers = [], set()
    for po in sorted(from_supplier):
        row = heads.get(po) or {}
        number = ap._v(row, "po_number").upper()
        if not aw._po_open(row) or (number and number in numbers):
            continue
        numbers.add(number)
        out.append(po)
    return out


def po_from_vendor_numbers(ctx: Any, event: Mapping[str, Any], *texts: str
                           ) -> tuple[list[str], dict[str, list[str]]]:
    """(the open POs the message's supplier item numbers point to, token -> items)."""
    suppliers = mail_suppliers(ctx, event)
    found = vendor_items(ctx, suppliers, candidate_tokens(*texts))
    if not found:
        return [], {}
    items = sorted({i for v in found.values() for i in v})
    return open_pos_for_items(ctx, suppliers, items), found
