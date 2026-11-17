"""A record page's title: a person's words for the record, never its key.

The store names a record by its type's ``display`` properties, else its primary
identifier. Neither exists for every record (a line item has no name; a receipt may
carry no number), and the page then fell back to the object id
(``Shipment:bc.sales_shipments:{"id": ...}``). The title is built here from the values
the reader's own card shows, in this order:

1. the store's display, when it reads as words (not the object id, a key or a hash);
2. the type's declared ``display`` properties (``ontology/schema/domains``);
3. ``TYPE_KEYS``: the few types whose schema names no display, each the property a clerk
   would call the record by;
4. a generic pass over the record's own properties (name, number, code, lot, reference);
5. the type's plain name with the record's native key when that key is short and readable
   (never a hash, a GUID or a handle), else with one qualifier it holds (the order it
   names, who it is between, its date), else the record it hangs from, else the document
   it was read from (when the reader may open it) and that document's day, else the plain
   name alone.

A line is titled first by its document and place; when its document has no readable name,
by the item it carries and the kind of document ("Maple syrup on a receipt, lot 043681").
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# Only where the schema declares no display property that the record holds. The value
# follows the type's name ("Purchase order line: Maple syrup"); a bare code or number
# follows without a colon ("Sales invoice line S-100").
TYPE_KEYS: dict[str, tuple[str, ...]] = {
    "PurchaseOrderLine": ("description", "item_number", "item_code"),
    "PurchaseInvoiceLine": ("description", "item_number"),
    "PurchaseCreditMemoLine": ("description", "item_number"),
    "PurchaseRequisitionLine": ("description", "item_number"),
    "SalesOrderLine": ("description", "item_number", "sage_item_code"),
    "SalesInvoiceLine": ("sage_item_code", "item_number", "description"),
    "SalesCreditMemoLine": ("description", "item_number"),
    "ShipmentLine": ("item_number", "sage_item_code", "description"),
    "ReceiptLine": ("supplier_lot_number", "lot_number", "item_number"),
    "AssemblyOrderComponent": ("lot_number", "component_code"),
    "AssemblyOrderDamage": ("damage_type", "category", "description"),
    "AssemblyOrderStatusEntry": ("status",),
    "AssemblyOrderAllocation": ("lot_number",),
    "ProductComponent": ("component_code",),
    "ProductionTask": ("task_type",),
    "ItemLedgerEntry": ("entry_number", "document_number"),
    "GeneralLedgerEntry": ("entry_number", "document_number"),
    "AgedPayable": ("party_number", "supplier_code"),
    "AgedReceivable": ("party_number", "customer_erp_code"),
    "Notice": ("kind", "carrier"),
    "NoticeReference": ("reference",),
    "InventoryLot": ("lot_number",),
    "Warehouse": ("code", "name"),
    "PortalUser": ("name", "email"),
}
GENERIC_KEYS = (
    re.compile(r"^(?:display_)?name$"), re.compile(r"^(?:label|title|subject|topic)$"),
    re.compile(r"^(?:[a-z]+_)?(?:order|invoice|document|receipt|shipment|po|lot|entry)_?(?:number|no)$"),
    re.compile(r"(?:^|_)number$"), re.compile(r"(?:^|_)code$"), re.compile(r"(?:^|_)no$"),
    re.compile(r"(?:^|_)(?:reference|lot|po)$"), re.compile(r"^description$"))
# What tells one record of a type from another when it has no number of its own (an
# extracted document the reader could not number): the order it names, who it is
# between, the day it is dated.
QUALIFIERS = (("po_reference", "PO {}"), ("customer_po", "PO {}"), ("buyer", "{}"),
              ("seller", "{}"), ("document_date", "{}"), ("ship_date", "{}"),
              ("created_at", "{}"), ("received_at", "{}"))
MONEYISH = re.compile(r"(?:amount|cost|price|total|balance|quantity|count|rate|weight)", re.I)
DESCRIPTIVE = frozenset({"description", "status", "task_type", "damage_type", "category",
                         "kind", "carrier", "name", "label", "title", "subject", "topic"})
HASHY = re.compile(r"^(?:[0-9a-f]{16,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                   r"[0-9a-f]{12})$", re.I)


@lru_cache(maxsize=1)
def schema_display() -> dict[str, tuple[str, ...]]:
    """Each type's declared display properties, read from the schema files."""
    import ontology
    out: dict[str, tuple[str, ...]] = {}
    for path in sorted((Path(ontology.__file__).parent / "schema" / "domains").glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for spec in doc.get("object_types") or ():
            if isinstance(spec, Mapping) and isinstance(spec.get("id"), str):
                out[spec["id"]] = tuple(p for p in spec.get("display") or ()
                                        if isinstance(p, str))
    return out


def type_words(object_type: str) -> str:
    """``PurchaseOrderLine`` -> ``Purchase order line``."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", object_type or "").strip().lower()
    text = re.sub(r"\bao\b", "assembly order", text)
    return (text[:1].upper() + text[1:]) if text else "Record"


def readable(text: Optional[str], object_id: str = "", object_type: str = "") -> bool:
    """Whether ``text`` reads as words a person says: not empty, not the record's key, a
    handle, a JSON fragment or a hash."""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not text or text == object_id or HASHY.match(text) or "://" in text:
        return False
    if text.startswith("{") or '{"' in text:
        return False
    return not re.match(r"^[A-Za-z]+:[a-z0-9_]+\.[a-z0-9_.]+:", text)


def native_key(object_id: str) -> Optional[str]:
    """The record's own key when it is a short, readable token (an order number): never a
    hash, a GUID, a long numeric surrogate or a JSON object of several parts."""
    parts = object_id.split(":", 2)
    if len(parts) < 3:
        return None
    raw = parts[2].strip()
    try:
        decoded = json.loads(raw)
    except ValueError:
        decoded = raw
    if isinstance(decoded, Mapping):
        # a lone database row id ({"id": 5321}) is no number anyone uses: never a title
        only = next(iter(decoded)) if len(decoded) == 1 else None
        decoded = (None if only is None or str(only).lower() in ("id", "rowid", "row_id")
                   else decoded[only])
    elif isinstance(decoded, list):
        decoded = decoded[0] if len(decoded) == 1 else None
    if isinstance(decoded, (int, float)) and not isinstance(decoded, bool):
        decoded = str(decoded)
    if not isinstance(decoded, str):
        return None
    decoded = decoded.strip()
    if not decoded or len(decoded) > 24 or HASHY.match(decoded) or re.fullmatch(r"\d{9,}", decoded) \
            or not re.fullmatch(r"[\w .\-/#]+", decoded):
        return None
    return decoded


def _held(values: Sequence[Mapping[str, Any]], plain) -> dict[str, str]:
    held: dict[str, str] = {}
    for value in values:
        prop = str(value.get("property") or "")
        shown = plain(value.get("value"))
        if prop and prop not in held and isinstance(shown, str) and readable(shown):
            held[prop] = shown.strip()
    return held


def linked_records(page: Mapping[str, Any], kind: str, plain) -> list[tuple[str, str, str]]:
    """The records one hop from this one that the reader's card names readably, as
    (link name, type, display), a line's own parent link first."""
    out = []
    for hop in (page.get("around") or {}).get("hops") or ():
        if not isinstance(hop, Mapping):
            continue
        link = str(hop.get("link_type") or "").rsplit(".", 1)[-1]
        for other in ((hop.get("result") or {}).get("objects") or ()
                      if isinstance(hop.get("result"), Mapping) else ()):
            if not isinstance(other, Mapping):
                continue
            shown = plain(other.get("display"))
            if readable(shown, str(other.get("object_id") or ""), str(other.get("object_type") or "")):
                out.append((link, str(other.get("object_type") or ""), shown.strip()))
    return out


def _parent_phrase(kind: str, display: str) -> str:
    """'sales order 77120' ('PO 90388' already says its kind)."""
    words = type_words(kind).lower()
    short = {"purchase order": "po"}.get(words, words)
    low = display.lower()
    return display if low.startswith((words, short)) else f"{words} {display}"


LINE_ITEM_KEYS = ("line_number", "line_no", "description", "sage_item_code", "item_code",
                  "item_number", "supplier_lot_number", "lot_number", "product_name")
LINE_PARENT_LINKS = ("line_of", "of_sales_order")


def _line_title(object_type: str, held: Mapping[str, str],
                linked: Sequence[tuple[str, str, str]]) -> Optional[str]:
    """A line of a document: its place on its parent ('Line 3 of sales order 77120') or
    what it is ('Maple syrup on sales order 77120'), read from the parent link and the
    properties the card already holds."""
    parent = next((r for name in LINE_PARENT_LINKS for r in linked if r[0] == name), None)
    if parent is None:
        return None
    where = _parent_phrase(parent[1], parent[2])
    number = next((held[k] for k in ("line_number", "line_no") if k in held), None)
    # a place on the document (1, 2, 3...); Business Central's Line No. counts in steps of
    # 10000 (10000, 20000, 15000 for an inserted line), which is no place a reader knows:
    # such a line is told by its item instead
    if number and re.fullmatch(r"\d{1,3}(?:\.0+)?", number):
        return f"Line {number.split('.')[0]} of {where}"
    what = next((held[k] for k in LINE_ITEM_KEYS[2:] if k in held), None)
    if what:
        return f"{what[:60]} on {where}"
    return f"{type_words(object_type)} of {where}"


ITEM_LINKS = ("for_item", "of_item", "supplier_item", "item", "of_product", "for_product")
LOT_KEYS = ("supplier_lot_number", "lot_number")


def _line_by_item(object_type: str, held: Mapping[str, str],
                  linked: Sequence[tuple[str, str, str]]) -> Optional[str]:
    """A line whose document has no readable name (a receipt keyed by a database row): what
    it carries and the kind of document it is on ('Maple syrup on a receipt, lot 043681'),
    from its item link. None when the line holds its own description or links no item."""
    if "description" in held or "product_name" in held:
        return None
    item = next((r for name in ITEM_LINKS for r in linked if r[0] == name), None)
    if item is None:
        return None
    doc = type_words(object_type).lower()
    doc = doc[:-len(" line")] if doc.endswith(" line") else doc
    article = "an" if doc[:1] in "aeiou" else "a"
    lot = next((held[k] for k in LOT_KEYS if k in held), None)
    return f"{item[2][:60]} on {article} {doc}" + (f", lot {lot[:30]}" if lot else "")


def source_documents(page: Mapping[str, Any], plain) -> list[tuple[str, str]]:
    """The documents around a record that the reader may open, as (title, date): the mail
    or file a value-less extract was read from. A document the reader may not open gives
    nothing (its name stays behind its source's grant)."""
    out = []
    result = (page.get("documents") or {}).get("result") or {}
    for doc in (result.get("documents") or ()) if isinstance(result, Mapping) else ():
        if not isinstance(doc, Mapping) or doc.get("openable") is not True:
            continue
        title = plain(doc.get("title")) or plain(doc.get("subject"))
        title = re.sub(r"\.(?:pdf|docx?|xlsx?|msg|eml|txt|csv)$", "", title.strip(), flags=re.I) \
            if isinstance(title, str) else ""
        when = doc.get("date") or doc.get("at")
        when = when[:10] if isinstance(when, str) and re.match(r"\d{4}-\d{2}-\d{2}", when) else ""
        if not readable(title):
            title = ""
        if title or when:
            out.append((title[:60].strip(), when))
    return out


def page_title(object_type: str, display: Optional[str], object_id: str,
               values: Sequence[Mapping[str, Any]], plain,
               linked: Sequence[tuple[str, str, str]] = (),
               documents: Sequence[tuple[str, str]] = ()) -> str:
    """The title of a record's page. ``plain`` shows a value as text (an untrusted-text
    envelope's text, a quantity's number); ``linked``: ``linked_records`` of its card;
    ``documents``: ``source_documents`` of its page."""
    held = _held(values, plain)
    if object_type.endswith("Line"):
        # a line is told by its parent and place, not by a figure it holds or a stored name
        got = _line_title(object_type, held, linked) or _line_by_item(object_type, held,
                                                                       linked)
        if got:
            return got
    if readable(display, object_id, object_type):
        return str(display).strip()
    name = type_words(object_type)
    for prop in schema_display().get(object_type, ()):
        if prop in held:
            return held[prop]
    for prop in TYPE_KEYS.get(object_type, ()):
        if prop in held:
            return _joined(name, prop, held[prop])
    for pattern in GENERIC_KEYS:
        for prop, shown in held.items():
            if pattern.search(prop) and not MONEYISH.search(prop) and len(shown) <= 80:
                return _joined(name, prop, shown)
    key = native_key(object_id)
    if key:
        return f"{name} {key}"
    for prop, form in QUALIFIERS:
        if prop in held:
            said = held[prop][:10] if prop.endswith(("_date", "_at")) else held[prop][:60]
            return f"{name}, {form.format(said)}"
    for prop, shown in held.items():
        if prop.endswith(("_date", "_at")) and re.match(r"\d{4}-\d{2}-\d{2}", shown):
            return f"{name}, {shown[:10]}"
    if linked:   # the record it hangs from
        _link, far, shown = linked[0]
        return f"{name}, {_parent_phrase(far, shown)}"
    # a record read from a document that stated none of its fields (a decision record or a
    # forwarder quote with every field not stated): the document it came from and its day
    for title, when in documents:
        if title:
            return f"{name} in {title}" + (f", {when}" if when else "")
    for _title, when in documents:
        if when:
            return f"{name}, {when}"
    return name


def _joined(name: str, prop: str, shown: str) -> str:
    if prop in LOT_KEYS and not name.lower().endswith("lot"):
        # a lot number says it is one ("Receipt line, lot 043681"; "Inventory lot 7" already does)
        return f"{name}, lot {shown}"
    return f"{name}: {shown}" if prop in DESCRIPTIVE else f"{name} {shown}"
