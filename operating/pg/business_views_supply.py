"""The business views of products, batches, stock and purchasing (suppliers, purchase
orders and lines, supplier documents and balances): the second half of
``operating.pg.business_views``, which imports this module once its own helpers are defined
(import ``business_views``, never this module first). Same rules as there: one row per real
thing, plain columns, currency beside every amount, system of record resolved."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping, Sequence

from operating.pg.business_views import (
    CATEGORY_DOC, Cell, MERGE_RULES_DOC, doc_type_doc, PARTY_MERGE, PILOT_DOC, Reader, Rec, Row, Thing, _text,
    aging_bucket, binding_rank, cents, fill, fill_category, handle_col, money, money_of, name_linked,
    party_codes, party_handle, pick, pilot_binding, to_currency, to_date, to_decimal,
    unique_things, view)


# --------------------------------------------------------------------------- products

PRODUCT_PROPS = ("code", "erp_code", "name", "product_type", "bc_item_type", "item_category",
                 "uom", "bc_base_uom", "is_active", "blocked", "bc_unit_cost",
                 "bc_unit_price", "quantity_on_hand")


def product_things(rd: Reader) -> tuple[dict[str, Rec], dict[str, Thing]]:
    def make() -> tuple[dict[str, Rec], dict[str, Thing]]:
        recs = rd.records("Product", PRODUCT_PROPS)
        return recs, {r.object_id: t for t in rd.clusters("Product", recs) for r in t.members}
    return rd.memo("products", make)


def _product_row(thing: Thing) -> Row:
    row = Row(thing)
    fill(row, thing, "name", "name")
    if fill(row, thing, "item_code", "code") is None:
        fill(row, thing, "item_code", "erp_code")
    if fill(row, thing, "item_type", "product_type") is None:
        fill(row, thing, "item_type", "bc_item_type")
    fill_category(row, thing)
    return row


def _on_hand(row: Row, thing: Thing) -> bool:
    """``on_hand`` and its unit from the first copy stating a readable quantity."""
    cell, rec = pick(thing, "quantity_on_hand")
    amount = to_decimal(cell.num if cell is not None and cell.num is not None else
                        cell.value if cell is not None else None)
    if amount is None:
        row.values["on_hand"] = row.values["on_hand_unit"] = None
        return False
    row.read(cell, "on_hand", amount, rec=rec)
    unit = _text(cell.unit)
    if unit is not None:
        row.read(cell, "on_hand_unit", unit.upper(), rec=rec)
    else:
        row.values["on_hand_unit"] = None
    return True


@view("products", "Every item once (Portal, FileMaker, Sage and BC copies of one item joined "
      "by identity links): finished goods, bulk, raw materials and packaging.",
      [("name", "text", "item name"), ("item_code", "text", "item code"),
       ("item_type", "text", "item type (bulk, finished, raw material, ...; BC: Inventory)"),
       ("category", "text", "item category: " + CATEGORY_DOC),
       ("uom", "text", "unit of measure"),
       ("active", "text", "Portal active flag"), ("blocked", "text", "blocked flag"),
       ("unit_cost", "money", "unit cost (BC)"), ("unit_price", "money", "unit price (BC)"),
       ("on_hand", "numeric", "quantity on hand, all locations (BC)"),
       ("on_hand_unit", "text", "unit of on_hand")],
      reads=("Product",), memos=("products",))
def _products(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs, _things = product_things(rd)
    for thing in rd.clusters("Product", recs):
        row = _product_row(thing)
        if fill(row, thing, "uom", "uom") is None:
            fill(row, thing, "uom", "bc_base_uom")
        fill(row, thing, "active", "is_active")
        fill(row, thing, "blocked", "blocked")
        money_of(row, thing, "unit_cost", "bc_unit_cost")
        money_of(row, thing, "unit_price", "bc_unit_price")
        _on_hand(row, thing)
        yield row


# --------------------------------------------------------------------------- batches

BATCH_PROPS = ("batch_code", "lot_number", "batch_number", "status", "quality_status",
               "scheduled_date", "realized_date", "released_at", "planned_quantity",
               "produced_quantity", "production_line", "po_number", "tested_by",
               "checked_by", "qc_ph_24h", "qc_viscosity_24h", "qc_tpc_yeast")
BATCH_PRODUCT_LINK = "Batch.makes_bulk"
QC_COLS = (("qc_ph_24h", "pH after 24 h"), ("qc_viscosity_24h", "viscosity after 24 h"),
           ("qc_tpc_yeast", "total plate count / yeast and mould result"))


def hold_of(status: str | None) -> bool | None:
    """True for a QC status that holds the batch (On-Hold, hold, quarantine), False for a
    released one, None for anything else (unknown, never guessed)."""
    word = re.sub(r"[^a-z]", "", (status or "").lower())
    if not word:
        return None
    if "hold" in word or "quarantin" in word:
        return True
    if word.startswith("release"):
        return False
    return None


@view("batches", "Every production batch once (Portal and FileMaker copies of one batch "
      "joined): its lot, the bulk product it makes, production status and dates, QC status "
      "(on_hold: QC holds it) and the 24 h QC readings as recorded.",
      [("batch_code", "text", "batch code"), ("lot_number", "text", "lot number"),
       ("product", "text", "the bulk product it makes"),
       ("item_code", "text", "that product's item code"),
       ("status", "text", "production status as the Portal states it (Allocated ... Realized)"),
       ("quality_status", "text", "QC status as stated (On-Hold, Released, ...)"),
       ("on_hold", "boolean", "from quality_status: true when it says hold or quarantine, "
        "false when it says released, empty for any other word"),
       ("scheduled_date", "date", "scheduled production date"),
       ("realized_date", "date", "the day it was made"),
       ("released_date", "date", "the day QC released it"),
       ("planned_quantity", "numeric", "planned quantity"),
       ("produced_quantity", "numeric", "produced quantity"),
       ("production_line", "text", "production line or pot"),
       ("po_number", "text", "customer PO it is made for"),
       *((c, "text", d) for c, d in QC_COLS),
       ("tested_by", "text", "who tested it"), ("checked_by", "text", "who checked it")],
      reads=("Batch", "Product"), memos=("products",))
def _batches(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs = rd.records("Batch", BATCH_PROPS)
    products, pthings = product_things(rd)
    makes = rd.linked(BATCH_PRODUCT_LINK, recs, products)
    for thing in rd.clusters("Batch", recs):
        row = Row(thing)
        if fill(row, thing, "batch_code", "batch_code") is None:
            fill(row, thing, "batch_code", "batch_number")
        fill(row, thing, "lot_number", "lot_number")
        product = name_linked(row, [e for r in thing.members for e in makes.get(r.object_id, ())],
                              pthings, "product")
        if product is not None:
            if fill(row, product, "item_code", "code") is None:
                fill(row, product, "item_code", "erp_code")
        else:
            row.values["item_code"] = None
        fill(row, thing, "status", "status")
        quality = fill(row, thing, "quality_status", "quality_status")
        row.values["on_hold"] = hold_of(quality)
        if "quality_status" in row.cites:
            row.cites["on_hold"] = row.cites["quality_status"]
            if "quality_status" in row.col_marks:
                row.col_marks["on_hold"] = row.col_marks["quality_status"]
        fill(row, thing, "scheduled_date", "scheduled_date", to_date)
        fill(row, thing, "realized_date", "realized_date", to_date)
        fill(row, thing, "released_date", "released_at", to_date)
        for column in ("planned_quantity", "produced_quantity"):
            fill(row, thing, column, column, to_decimal)
        for column in ("production_line", "po_number", "tested_by", "checked_by",
                       *(c for c, _d in QC_COLS)):
            fill(row, thing, column, column)
        yield row


@view("stock", "Stock on hand: every item whose record states a quantity on hand (BC states "
      "it per item over all its locations; no served source states stock per location), in "
      "its own unit. A quantity of 0 is stated stock of 0.",
      [("name", "text", "item name"), ("item_code", "text", "item code"),
       ("item_type", "text", "item type"), ("category", "text", "item category"),
       ("on_hand", "numeric", "quantity on hand, all locations"),
       ("on_hand_unit", "text", "unit of on_hand (sums only within one unit)"),
       ("as_of", "date", "the day the view was built")],
      reads=("Product",), memos=("products",))
def _stock(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    recs, _things = product_things(rd)
    for thing in rd.clusters("Product", recs):
        row = _product_row(thing)
        if not _on_hand(row, thing):
            continue
        row.values["as_of"] = as_of
        yield row


# --------------------------------------------------------------------------- engine reads

class EngineView:
    """The proactive engine's store reads (``values``, ``links``, ``versions``, as
    ``proactive_engine._Store`` shapes them) over a build's ``Reader``, so the one
    purchase-order reader (``po_status.StoreSource`` + ``po_status.read``) runs here as it
    runs in the engine: records and links its source no longer holds left out by the
    store's own removal predicate, PLX's own documents held as supplier documents too."""

    def __init__(self, rd: Reader) -> None:
        self.rd = rd
        self._values: dict[tuple, dict[str, dict[str, Any]]] = {}

    def _gone(self, object_id: str) -> bool:
        return object_id in self.rd.removed(object_id.split(":", 1)[0]) \
            or object_id in self.rd.hidden()

    def values(self, types: Sequence[str], props: Sequence[str] | None = None, *,
               objects: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for object_type in types:
            key = (object_type, tuple(props) if props is not None else None)
            if key not in self._values:
                self._values[key] = self._read(object_type, props)
            out.update(self._values[key])
        if objects is not None:
            wanted = set(objects)
            out = {k: v for k, v in out.items() if k in wanted}
        return out

    def _read(self, object_type: str, props: Sequence[str] | None) -> dict[str, dict[str, Any]]:
        gone = self.rd.removed(object_type) | self.rd.hidden()
        sql = ("SELECT o.object_id, o.object_type, f.property, k.value_type, c.value, k.unit, "
               "k.quantity_role, v.l1_handle, k.markings, COALESCE(s.lineage, v.lineage) AS "
               "lineage, c.currency FROM objects o JOIN object_versions v ON v.object_id = "
               "o.object_id AND v.superseded_at IS NULL JOIN value_sources s ON s.ver = v.ver "
               "AND s.superseded_at IS NULL CROSS JOIN LATERAL (SELECT c.value, c.currency, "
               "c.field, c.kind FROM value_cells c WHERE c.src = s.src OFFSET 0) c "   # as records()
               "JOIN value_kinds k ON k.kind = c.kind JOIN value_fields f ON f.field = c.field "
               "WHERE o.object_type = :t")
        params: dict[str, Any] = {"t": object_type}
        if props is not None:
            sql += " AND f.property IN (SELECT value FROM json_each(:p))"
            params["p"] = json.dumps(list(props))
        out: dict[str, dict[str, Any]] = {}
        for row in self.rd.conn.execute(sql + " ORDER BY o.object_id, f.property, v.l1_handle",
                                        params):
            if row["object_id"] in gone:
                continue
            entry = out.setdefault(row["object_id"], {"type": row["object_type"], "props": {}})
            entry["props"].setdefault(row["property"], []).append(dict(row))
        return out

    def links(self, types: Sequence[str], tiers: Sequence[str] | None = None, *,
              held: bool = True) -> list[dict[str, Any]]:
        rows = self.rd.conn.execute(
            "SELECT link_id, link_type, from_object, to_object, tier, markings FROM links "
            "WHERE link_type IN (SELECT value FROM json_each(:lt)) AND superseded_at IS NULL "
            "AND tier <> 'contradicted'", {"lt": json.dumps(list(dict.fromkeys(types)))})
        keep = None if tiers is None else frozenset(tiers)
        return [dict(row) for row in rows if (keep is None or row["tier"] in keep)
                and not (held and (self._gone(row["from_object"])
                                   or self._gone(row["to_object"])))]

    def versions(self, object_type: str | None = None, handles: Iterable[str] | None = None,
                 objects: Iterable[str] | None = None) -> list[dict[str, Any]]:
        wanted = sorted(set(objects or ()))
        out = []
        for start in range(0, len(wanted), 5000):
            for row in self.rd.conn.execute(
                    "SELECT v.l1_handle, v.object_id, v.markings, o.object_type FROM "
                    "object_versions v JOIN objects o ON o.object_id = v.object_id WHERE "
                    "v.superseded_at IS NULL AND v.object_id IN (SELECT value FROM "
                    "json_each(:o))", {"o": json.dumps(wanted[start:start + 5000])}):
                if not self._gone(row["object_id"]):
                    out.append(dict(row))
        return out


# --------------------------------------------------------------------------- purchasing

SUPPLIER_PROPS = ("name", "code", "currency", "status", "bc_blocked", "incoterms",
                  "lead_time_days", "supplier_type", "bc_email", "bc_phone_number",
                  "rep_email", "website")


@view("suppliers", "Every supplier once: its Portal, FileMaker and BC (each company) copies "
      "joined by identity links and the party merge rule (merged_by says which).",
      [("name", "text", "supplier name"), ("supplier_code", "text", "supplier code"),
       ("codes", "text", "every copy's code as 'system:code' (Supplier code per copy), "
        "comma separated: any system's code finds the supplier"),
       ("merged_by", "text", MERGE_RULES_DOC),
       ("currency", "text", "the supplier's currency"),
       ("status", "text", "the supplier record's status as stated"),
       ("blocked", "text", "BC blocked flag, when set"),
       ("supplier_type", "text", "supplier type"), ("incoterms", "text", "stated incoterms"),
       ("lead_time_days", "integer", "stated lead time in days"),
       ("email", "text", "rep email, else the BC email"),
       ("phone", "text", "BC phone number"), ("website", "text", "stated website")],
      reads=("Supplier",), memos=("suppliers",))
def _suppliers(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    _recs, things = supplier_things(rd)
    for thing in unique_things(things):
        row = Row(thing)
        fill(row, thing, "name", "name")
        fill(row, thing, "supplier_code", "code")
        party_codes(row, thing, "codes", PARTY_MERGE["Supplier"].code_props)
        row.values["merged_by"] = ", ".join(sorted(thing.merged_by)) or None
        fill(row, thing, "currency", "currency", to_currency)
        fill(row, thing, "status", "status")
        fill(row, thing, "blocked", "bc_blocked")
        fill(row, thing, "supplier_type", "supplier_type")
        fill(row, thing, "incoterms", "incoterms")
        fill(row, thing, "lead_time_days", "lead_time_days", _integer)
        if fill(row, thing, "email", "rep_email") is None:
            fill(row, thing, "email", "bc_email")
        fill(row, thing, "phone", "bc_phone_number")
        fill(row, thing, "website", "website")
        yield row


def _integer(raw: Any) -> int | None:
    got = to_decimal(raw)
    return int(got) if got is not None and got == got.to_integral_value() else None


def supplier_things(rd: Reader) -> tuple[dict[str, Rec], dict[str, Thing]]:
    def make() -> tuple[dict[str, Rec], dict[str, Thing]]:
        recs = rd.records("Supplier", SUPPLIER_PROPS)
        return recs, {r.object_id: t for t in rd.clusters("Supplier", recs) for r in t.members}
    return rd.memo("suppliers", make)


PO_PROPS = ("po_number", "fm_po_number", "sage_po_number", "order_date", "expected_date",
            "vendor_number", "vendor_name", "currency", "total_cost", "purchaser_code",
            "requisitioner", "incoterms", "tracking_number", "status", "fm_status",
            "bc_status", "sage_is_complete")
PO_LINE_PROPS = ("ordered_quantity", "received_quantity", "invoiced_quantity", "unit_price",
                 "line_total", "uom", "description", "item_code", "bc_expected_receipt_date",
                 "line_type")


@dataclass
class Purchase:
    thing: Thing
    read: Any                        # the po_status.PoRead the row states
    verdict: str | None
    verdict_reason: str | None
    days_late: int | None
    marks: set[str]
    lines: list[tuple[str, str]]     # (line, link markings) of the copy that holds them
    reads: "Sources"                 # every other record the row's values were read from
    # per column, the records only that column was read from (the supplier's promise, held
    # in mail or SharePoint: its own column class, never the whole row's)
    cols: dict[str, "Sources"]
    note: str | None = None          # receipt_note: where the order's copies disagree


@dataclass
class Sources:
    """Records read (object id -> handles, ``Row.read_record``), the markings of the links
    followed and of the value cells read."""
    ids: dict[str, set[str]] = dc_field(default_factory=lambda: defaultdict(set))
    links: set[str] = dc_field(default_factory=set)
    cells: set[str] = dc_field(default_factory=set)

    def add(self, items: Iterable[Any]) -> "Sources":
        """Every item a po_status read cites: a value row (its record and version), a
        ``(row, link)`` pair (the link's source record and markings too), a record id."""
        for item in items:
            if isinstance(item, tuple) and len(item) == 2:
                row, link = item
                self.add([row])
                if isinstance(link, Mapping):
                    self.ids[link.get("from_object")].add("*")
                    self.ids[link.get("to_object")]   # noqa: B018
                    self.links.add(link.get("markings") or "[]")
                continue
            if isinstance(item, Mapping):
                self.ids[item.get("object_id")].add(item.get("l1_handle") or "*")
                if item.get("markings") not in (None, "", "[]"):
                    self.cells.add(item["markings"] if isinstance(item["markings"], str)
                                   else json.dumps(sorted(item["markings"])))
                continue
            self.ids[item if isinstance(item, str) else None].add("*")
        return self

    def into(self, row: Row, column: str | None = None) -> None:
        for oid, handles in self.ids.items():
            for h in handles or {""}:
                row.read_record(oid, h, column=column)
        if column is None:
            row.marks |= self.links | self.cells
            return
        row.col_link_marks[column] |= self.links | self.cells


def purchase_model(rd: Reader, as_of: dt.date) -> dict[str, Any]:
    return rd.memo("purchase_orders", lambda: _purchase_model(rd, as_of))


def _purchase_model(rd: Reader, as_of: dt.date) -> dict[str, Any]:
    """Every purchase order once, read by the one purchase-order reader as the late-PO
    rule reads it (``po_status.read`` over ``po_status.StoreSource``): settled twins one
    PO, and a PO and each other system's record compared with it (``date_twins``) one
    order. The order is late when any of its reads is (the latest days late)."""
    from operating import po_status
    src = po_status.StoreSource(EngineView(rd))
    reads = [po_status.read(src, top, as_of) for top in sorted(src.clusters)]
    joined = [(po.members[0], other) for po in reads
              for other in (*po.members[1:], *po.date_twins)]
    root = po_status.clusters(joined, [m for po in reads for m in (*po.members,
                                                                   *po.date_twins)])
    groups: dict[str, list] = defaultdict(list)
    for po in reads:
        groups[root.get(po.root, po.root)].append(po)
    recs = rd.records(po_status.PO_TYPE, PO_PROPS)
    out: dict[str, Purchase] = {}
    for top in sorted(groups):
        found = sorted(groups[top], key=lambda po: (binding_rank(
            po_status.binding_of(po.root) or ""), po.root))
        members = sorted({m for po in found for m in po.members if m in recs},
                         key=lambda m: (binding_rank(recs[m].binding), m))
        if not members:
            continue
        thing = Thing([recs[m] for m in members])
        late = [po for po in found if po.verdict and po.verdict[0] == po_status.LATE]
        risk = [po for po in found if po.verdict and po.verdict[0] == po_status.AT_RISK]
        chosen = (late or risk or found)[0]
        days = None
        if late:
            days = max((as_of - po.need.hi).days for po in late if po.need and po.need.hi)
            chosen = max(late, key=lambda po: (as_of - po.need.hi).days
                         if po.need and po.need.hi else -1)
        verdict, reason = chosen.verdict if chosen.verdict else (None, None)
        marks = {m for po in found for m in (*po.marks, *po.identity_marks) if m}
        marks |= {r.get("markings") or "[]" for po in found for r in po.evidence
                  if isinstance(r, Mapping) and isinstance(r.get("markings"), str)}
        carrier = next((m for m in members if src.lines_of.get(m)), None)
        reads, promised = Sources(), Sources()
        for po in found:
            reads.add([*po.evidence, *(m for m in (*po.members, *po.date_twins)
                                       if m not in members)])
            if po.promise is not None:
                promised.add(po.promise.items)
            if po.need is not None and po.need.basis != "vendor_eta":
                n = po.need
                reads.add([*n.governing, *n.others, *n.unparseable, *n.undated, *n.defaulted])
        cols = {"promised_date": promised}
        if any(po.need is not None and po.need.basis == "vendor_eta" for po in found):
            # the requested-by date itself is the supplier's promise
            cols.update(dict.fromkeys(("requested_date", "days_late", "verdict",
                                       "verdict_reason"), promised))
        elif promised.ids and any(po.status.cls == po_status.OPEN and po.verdict and po.verdict[0]
                                  in (po_status.AT_RISK, po_status.NEITHER) for po in found):
            # at risk (or not) only by the promise: the verdict is read from it
            cols.update(dict.fromkeys(("verdict", "verdict_reason"), promised))
        note, noted = receipt_note(found)
        if note is not None:
            cols["receipt_note"] = noted
        out[top] = Purchase(thing, chosen, verdict, reason, days, marks,
                            sorted(src.lines_of.get(carrier, ())) if carrier else [],
                            reads, cols, note)
    return {"orders": out}


RECEIPT_FIELDS = ("receipt_state", "received_quantity")


SYSTEM_WORDS = {"bc": "BC", "portal": "Portal", "filemaker": "FileMaker", "sage": "Sage"}


def _copy_state(system: str, raw: Any) -> str | None:
    """What one copy's own header word says, coarsely: ``draft`` (not issued), ``ended``
    (complete, closed or cancelled; Sage iscomplete 1), ``issued``; None: no word or one
    no rule maps (never a guess)."""
    from operating import po_status
    if system == "sage":
        flag = str(raw).strip() if raw is not None else ""
        return "ended" if flag in ("1", "true", "True") else (
            "issued" if flag in ("0", "false", "False") else None)
    kind = po_status.header_class(raw, "bc" if system == "bc" else "portal")
    if kind == po_status.NOT_ISSUED:
        return "draft"
    if kind == po_status.IGNORED or po_status.word(raw) in po_status.COMPLETE_WORDS:
        return "ended"
    return "issued" if kind == po_status.ISSUED else None


def copy_statuses(row: Row, thing: Thing) -> str | None:
    """Where the order's own copies state different header statuses (a BC copy still a
    Draft, never released to the supplier, of an order the Portal marks Complete), each
    copy's system, number and status word as that system states it; None when the copies
    agree or only one states a status. Its own column class: every copy it was read from
    (audience key, version markings, id) and every cell's markings."""
    from operating import po_status
    shown: list[str] = []
    states: set[str] = set()
    cells: list[tuple[Cell, Rec]] = []
    for rec in thing.members:
        system = po_status.system_name(rec.object_id)
        prop = {"bc": "bc_status", "filemaker": "fm_status", "sage": "sage_is_complete"
                }.get(system, "status")
        cell = rec.cell(prop) or (rec.cell("fm_status") if system == "portal" else None)
        if cell is None or cell.value in (None, ""):
            continue
        state = _copy_state(system, cell.value)
        if state is None:
            continue
        states.add(state)
        number = next((n.value for n in (rec.cell(p) for p in (
            "po_number", "fm_po_number", "sage_po_number")) if n is not None and n.value), "")
        word = ("complete" if state == "ended" else "not complete") \
            if system == "sage" else str(cell.value).strip()
        shown.append(" ".join(x for x in (SYSTEM_WORDS.get(system, system), str(number),
                                          word) if x))
        cells.append((cell, rec))
    if len(states) < 2:
        return None
    first = cells[0][0]
    row.cites["copy_statuses"] = [first.handle, first.field_path]
    marks = sorted({m for c, _r in cells for m in json.loads(c.marks or "[]")})
    if marks:
        row.col_marks["copy_statuses"] = json.dumps(marks)
    for c, r in cells:
        # each copy read joins this column's class only: the row's own stays as it was
        row.read_record(r.object_id, c.handle, column="copy_statuses")
    return "; ".join(shown)


def receipt_note(found: Sequence[Any]) -> tuple[str | None, "Sources"]:
    """The receipt conflicts the PO reader names between an order's copies (``_compare``:
    a BC copy posted from a Portal/FileMaker order, each recording its own receipts), as
    one line, and the records and link marks it was read from (its own column class: the
    other copy may be one a reader cannot read). A receipt in any copy closes the order;
    the note keeps the open
    copy visible for reconciliation instead of dropping it."""
    parts: list[str] = []
    seen: set[Any] = set()
    noted = Sources()
    for po in found:
        for c in po.conflicts:
            if c.get("field") not in RECEIPT_FIELDS:
                continue
            # each copy's reader names the same conflict from its own side (the sides in
            # its order): one conflict is one note part, whichever copy named it first
            key = (c["field"], c.get("unit"), c.get("governs"), tuple(sorted(
                (str(side.get("system")), str(side.get("value")))
                for side in c.get("sides") or ())))
            fresh = key not in seen
            seen.add(key)
            sides = ", ".join(
                f"{side['system']} {str(side['value']).replace('_', ' ')}"
                + (" (holds)" if c.get("governs") == side["system"] else "")
                for side in c.get("sides") or ())
            unit = f" {c['unit']}" if c.get("unit") else ""
            text = f"{c['field'].replace('_', ' ')}: {sides}{unit}"
            if fresh and text not in parts:
                parts.append(text)
            for side in c.get("sides") or ():
                noted.add([*(side.get("records") or ()), *(side.get("items") or ())])
            noted.links |= {m for m in c.get("marks") or () if isinstance(m, str)}
    return ("; ".join(parts) if parts else None), noted


@view("purchase_orders", "Every purchase order once (Portal, FileMaker, Sage and BC copies "
      "joined), its status and lateness by the one purchase-order reader (operating/"
      "po_status.py, the late-PO rule the engine, Home and the late list share): late = "
      "status open (issued and a quantity still to receive) and the requested-by date has "
      "passed. A draft (Portal/FileMaker DRAFT, BC Draft = not released) is never late; "
      "the supplier's promised date never replaces the requested-by date, it only makes an "
      "order at_risk; an order with no requested-by date is listed apart (verdict unknown, "
      "verdict_reason no_requested_date), never counted late.",
      [("po_number", "text", "PO number"),
       ("supplier", "text", "supplier name (the linked supplier)"),
       ("vendor_number", "text", "vendor number on the PO"),
       ("order_date", "date", "order date"),
       ("requested_date", "date", "requested-by date the verdict is judged against: "
        "PurchaseOrder.expected_date of the top-ranked source (po_delivery_window "
        "lateness.precedence), else the earliest open BC line's bc_expected_receipt_date; "
        "a day a source stamps on many orders at once (a migration or bulk default) or a "
        "placeholder day is no date"),
       ("promised_date", "date", "the supplier's latest promised date (SupplierConfirmation "
        "delivery_date by document date, or its invoice): drives at_risk only. COLUMN "
        "CLASS: read from the supplier's documents (often mail or SharePoint), so its "
        "sources' audience keys, markings and ids are this column's own (the row stays "
        "visible to a reader without them; a read using this column withholds the row)"),
       ("status", "text", "open (issued: Portal/FileMaker Pending, B.Order, Ordered or BC "
        "Open = released, with a line quantity still to receive), closed (Complete, Cancel, "
        "received in full, Sage iscomplete 1), draft (Portal/FileMaker DRAFT, BC Draft: "
        "not released) or unknown (status_reason; copies that disagree are unknown)"),
       ("status_reason", "text", "why the status is unknown"),
       ("copy_statuses", "text", "set only when the order's own copies state different "
        "statuses: each copy's system, number and status as that system states it (e.g. "
        "a BC copy still Draft, never released to the supplier, of an order the Portal "
        "marks Complete); status is the joined order's. Say both when it matters"),
       ("receipt_note", "text", "where the order's copies disagree on receipt (a BC copy "
        "posted from a Portal/FileMaker order): each copy's receipt state or received "
        "quantity and the one that holds. A receipt in any copy closes the order, so an "
        "order closed here may still show open lines in BC: reconcile, never late. COLUMN "
        "CLASS: read from both copies"),
       ("verdict", "text", "late (status open, requested_date before as_of, the "
        "America/New_York day), at_risk (open, not yet due, promised_date after "
        "requested_date), neither (closed, draft or not due) or unknown (verdict_reason)"),
       ("verdict_reason", "text", "why the verdict is unknown. verdict and verdict_reason "
        "carry promised_date's column class when the promise decided them (at_risk, or "
        "neither for an open order not yet due); requested_date, days_late and the verdict "
        "do when the requested-by date is the supplier's promise (no record states one)"),
       ("days_late", "integer", "as_of - requested_date in days, late POs only (a joined "
        "order: its latest late copy); requested_date's column marks"),
       ("total", "money", "PO total as its source states it (a zeroed copy is skipped); "
        "when no copy states a total above 0, the sum of its lines (lines_total, "
        "total_basis 'lines')"),
       ("total_basis", "text", "header (a copy's stated total) or lines (no copy states "
        "one above 0: the sum of the lines, before tax and charges); none without a total"),
       ("lines_total", "money", "the sum of the PO's lines' line_total (the lines "
        "purchase_order_lines shows), when every line states one in one currency; it may "
        "differ from a header total (tax, freight). COLUMN CLASS: read from the lines"),
       ("purchaser", "text", "purchaser code"),
       ("line_count", "integer", "PO lines (of the copy that holds them)"),
       ("as_of", "date", "the day the verdict was computed"),
       handle_col("supplier", "the PO's PurchaseOrder.ordered_from link")],
      reads=("PurchaseOrder", "PurchaseOrderLine", "SupplierConfirmation", "SupplierInvoice",
             "Supplier"),
      memos=("suppliers", "purchase_orders"))
def _purchase_orders(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    model = purchase_model(rd, as_of)
    suppliers, things = supplier_things(rd)
    found = {r.object_id: r for p in model["orders"].values() for r in p.thing.members}
    ordered_from = rd.linked("PurchaseOrder.ordered_from", found, suppliers)
    lines, held = _held_lines(rd, model)
    for top, po in model["orders"].items():
        yield _po_row(po, things, ordered_from, as_of, lines, held)


def _held_lines(rd: Reader, model: Mapping[str, Any]) -> tuple[dict[str, Rec], Counter]:
    """Every PO line record, and how many orders hold each (a line two hold is shown by
    neither)."""
    return (rd.records("PurchaseOrderLine", PO_LINE_PROPS),
            Counter(ln for p in model["orders"].values() for ln, _m in p.lines))


def _shown_lines(po: Purchase, lines: Mapping[str, Rec], held: Counter
                 ) -> list[tuple[Rec, str]]:
    """The lines ``purchase_order_lines`` shows for ``po`` (record, link markings)."""
    return [(lines[ln], marks) for ln, marks in po.lines if ln in lines and held[ln] == 1]


def _carrier(po: Purchase, lines: Mapping[str, Rec]) -> Rec:
    return next((m for m in po.thing.members if any(ln in lines for ln, _ in po.lines)),
                po.thing.rep)


def _lines_total(row: Row, po: Purchase, lines: Mapping[str, Rec], held: Counter) -> None:
    """``lines_total``: the shown lines' line totals summed, when every one states an
    amount in one currency (the line's own, else the order's, as the lines view reads it);
    else none. Read from the lines: their records, cells and link markings are the
    column's class, never the whole row's."""
    shown = _shown_lines(po, lines, held)
    row.values["lines_total"] = row.values["lines_total_currency"] = None
    if not shown:
        return
    carrier = _carrier(po, lines)
    order_cur = to_currency(_text(getattr(carrier.cell("currency"), "value", None)))
    amount, codes, used = Decimal(0), set(), []
    for rec, link_marks in shown:
        cell = rec.cell("line_total")
        got = to_decimal(cell.num if cell is not None and cell.num is not None
                         else cell.value if cell is not None else None)
        code = (to_currency(cell.currency) if cell is not None else None) or order_cur
        if got is None or code is None:
            return
        amount += got
        codes.add(code)
        used.append((rec, cell, link_marks))
    if len(codes) != 1:
        return
    row.values["lines_total"], row.values["lines_total_currency"] = cents(amount), codes.pop()
    first = used[0][1]
    for col in ("lines_total", "lines_total_currency"):
        row.cites[col] = [first.handle, first.field_path]
        for rec, cell, link_marks in used:
            row.read_record(rec.object_id, cell.handle, column=col)
            row.col_link_marks[col].add(link_marks or "[]")
            if cell.marks not in ("", "[]"):
                row.col_link_marks[col].add(cell.marks)
        if order_cur is not None and col.endswith("_currency"):
            row.read_record(carrier.object_id, column=col)


def _po_row(po: Purchase, things: Mapping[str, Thing], ordered_from: Mapping[str, list],
            as_of: dt.date, lines: Mapping[str, Rec], held: Counter) -> Row:
    thing = po.thing
    row = Row(thing)
    row.marks |= po.marks
    for prop in ("po_number", "sage_po_number", "fm_po_number"):
        if fill(row, thing, "po_number", prop) is not None:
            break
    party_handle(row, "supplier", name_linked(
        row, [e for r in thing.members for e in ordered_from.get(r.object_id, ())],
        things, "supplier"))
    if row.values.get("supplier") is None:
        fill(row, thing, "supplier", "vendor_name")
    fill(row, thing, "vendor_number", "vendor_number")
    fill(row, thing, "order_date", "order_date", to_date)
    read = po.read
    need = read.need
    row.values["requested_date"] = (need.hi or need.lo) if need is not None else None
    row.values["promised_date"] = read.promise.latest if read.promise is not None else None
    row.values["status"] = read.status.cls
    row.values["status_reason"] = read.status.reason
    row.values["copy_statuses"] = copy_statuses(row, thing)
    row.values["receipt_note"] = po.note
    row.values["verdict"], row.values["verdict_reason"] = po.verdict, po.verdict_reason
    row.values["days_late"] = po.days_late
    po.reads.into(row)
    for column, sources in po.cols.items():
        sources.into(row, column)
    if read.promise is not None and read.promise.items and row.values["promised_date"]:
        first = read.promise.items[-1]
        cited = first[0] if isinstance(first, tuple) else first
        if isinstance(cited, Mapping) and cited.get("l1_handle"):
            row.cites["promised_date"] = [cited["l1_handle"], cited.get("field_path")
                                         or cited.get("property") or ""]
    # the first copy stating a non-zero total (a Portal header is zeroed on some copies)
    _lines_total(row, po, lines, held)
    for rec in thing.members:
        cell = rec.cell("total_cost")
        amount = to_decimal(cell.num if cell and cell.num is not None else
                            cell.value if cell else None)
        if amount:
            money(row, "total", cell, rec, (rec.cell("currency"), rec))
            break
    else:
        money_of(row, thing, "total", "total_cost")
        if row.values.get("lines_total"):
            # no copy states a total above 0 (the Portal leaves a header at 0 until it is
            # costed): the order is worth its lines, said as such (total_basis)
            row.values["total"] = row.values["lines_total"]
            row.values["total_currency"] = row.values["lines_total_currency"]
            for col in ("total", "total_currency"):
                row.cites[col] = row.cites["lines_total"]
                row.col_marks.pop(col, None)
                row.derive(col, row, "lines_total")
            row.values["total_basis"] = "lines"
    row.values.setdefault("total_basis", "header" if row.values.get("total") is not None
                          else None)
    if fill(row, thing, "purchaser", "purchaser_code") is None:
        fill(row, thing, "purchaser", "requisitioner")
    row.values["line_count"] = len(po.lines)
    row.values["as_of"] = as_of
    return row


@view("purchase_order_lines", "The lines of every purchase order once (the lines of the one "
      "copy that holds them), with the PO's number, supplier, status and verdict.",
      [("po_number", "text", "PO number"), ("supplier", "text", "supplier name"),
       ("po_status", "text", "the PO's status (purchase_orders.status)"),
       ("verdict", "text", "the PO's verdict (purchase_orders.verdict)"),
       ("item_code", "text", "item code"), ("description", "text", "line description"),
       ("ordered_quantity", "numeric", "quantity ordered"),
       ("received_quantity", "numeric", "quantity received"),
       ("invoiced_quantity", "numeric", "quantity invoiced"),
       ("uom", "text", "unit of measure"),
       ("unit_price", "money", "unit price"), ("line_total", "money", "line total"),
       ("expected_date", "date", "expected receipt date (BC)"),
       ("po_handle", "text", "the purchase_orders row it is a line of (join "
        "purchase_orders on handle)"),
       handle_col("supplier", "its PO's PurchaseOrder.ordered_from link (the PO's "
                  "supplier_handle)")],
      reads=("PurchaseOrderLine", "PurchaseOrder", "SupplierConfirmation", "SupplierInvoice",
             "Supplier"),
      memos=("suppliers", "purchase_orders"))
def _purchase_order_lines(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    model = purchase_model(rd, as_of)
    suppliers, things = supplier_things(rd)
    found = {r.object_id: r for p in model["orders"].values() for r in p.thing.members}
    ordered_from = rd.linked("PurchaseOrder.ordered_from", found, suppliers)
    lines, held = _held_lines(rd, model)
    for top, po in model["orders"].items():
        if not po.lines:
            continue
        head = _po_row(po, things, ordered_from, as_of, lines, held)
        carrier = _carrier(po, lines)
        for ln, link_marks in po.lines:
            rec = lines.get(ln)
            if rec is None or held[ln] != 1:
                continue   # not held, or a line two orders hold: never twice
            lthing = Thing([rec])
            row = Row(lthing)
            row.add_thing(po.thing)
            row.inherit(head, (("po_number", "po_number"), ("supplier", "supplier"),
                               ("po_status", "status"), ("verdict", "verdict"),
                               ("supplier_handle", "supplier_handle")))
            row.marks.add(link_marks or "[]")
            row.values["po_handle"] = head.handle
            fill(row, lthing, "item_code", "item_code")
            fill(row, lthing, "description", "description")
            for column in ("ordered_quantity", "received_quantity", "invoiced_quantity"):
                fill(row, lthing, column, column, to_decimal)
            fill(row, lthing, "uom", "uom")
            cur = (carrier.cell("currency"), carrier)
            money(row, "unit_price", *pick(lthing, "unit_price"), cur)
            money(row, "line_total", *pick(lthing, "line_total"), cur)
            fill(row, lthing, "expected_date", "bc_expected_receipt_date", to_date)
            yield row


AP_OPEN = "RelSageApOpenDocument"
VENDOR_SUPPLIER_LINKS = ("RelSageApVendor.portal_supplier_by_sage_id",
                         "RelSageApVendor.portal_supplier", "RelSageApVendor.bc_vendor")
# Sage AP's due date is ``dateinvcdu`` (APOBL; AR's ARODOC field is ``datedue``, which AP
# rows do not hold: 0 of 73,437 live on 11-21, every AP due date and aging was empty)
AP_PROPS = ("idinvc", "idvend", "txttrxtype", "dateinvc", "dateinvcdu", "datedue", "amtduetc",
            "amtinvctc", "codecurn", "swpaid", "idponbr", "descinvc")
SAGE_AP_DOC = {"1": "invoice", "2": "debit note", "3": "credit note", "4": "interest",
               "5": "unapplied cash", "10": "prepayment", "11": "payment"}
AP_DOC_TYPE_DOC = doc_type_doc(SAGE_AP_DOC, "payments, prepayments, unapplied cash and "
                               "credit notes") + "; a BC pilot purchase invoice is an invoice"
PURCHASE_INVOICE_PROPS = ("bc_number", "vendor_invoice_number", "order_number",
                          "vendor_number", "invoice_date", "due_date", "status", "currency",
                          "subtotal", "total_amount", "remaining_amount")


def _ap_vendors(rd: Reader) -> dict[str, Thing]:
    recs = rd.records("RelSageApVendor", ("vendname", "vendorid", "shortname", "idgrp"))
    return {oid: Thing([rec]) for oid, rec in recs.items()}


def _ap_row(rd: Reader, rec: Rec, vendors: Mapping[str, Thing],
            owner: Mapping[str, list], as_of: dt.date) -> Row:
    thing = Thing([rec])
    row = Row(thing)
    fill(row, thing, "supplier_code", "idvend")
    fill(row, thing, "document_number", "idinvc")
    kind, krec = pick(thing, "txttrxtype")
    row.read(kind, "document_type", SAGE_AP_DOC.get(_text(kind.value) or "", "other")
             if kind else None, rec=krec)
    fill(row, thing, "document_date", "dateinvc", to_date)
    if fill(row, thing, "due_date", "dateinvcdu", to_date) is None:
        fill(row, thing, "due_date", "datedue", to_date)
    cur = pick(thing, "codecurn")
    cell, crec = pick(thing, "amtduetc")
    if cell is None:
        cell, crec = rec.cell("amtduetc"), rec   # a zero balance is a value
    money(row, "amount_due", cell, crec, cur)
    money(row, "original_amount", *pick(thing, "amtinvctc"), cur)
    fill(row, thing, "po_number", "idponbr")
    fill(row, thing, "description", "descinvc")
    vendor = name_linked(row, owner.get(rec.object_id) or [], vendors, "supplier", "vendname")
    if vendor is not None:
        fill(row, vendor, "vendor_group", "idgrp")
    suppliers, things, of_vendor = vendor_suppliers(rd)
    party_handle(row, "supplier", one_party(
        row, of_vendor.get(vendor.rep.object_id, []) if vendor is not None else [], things))
    return row


def vendor_suppliers(rd: Reader) -> tuple[dict[str, Rec], dict[str, Thing],
                                          dict[str, list[tuple[str, str]]]]:
    """The suppliers, their things and Sage AP vendor id -> its supplier link ends (the
    vendor's settled links to a Portal or BC supplier; memoised)."""
    def make() -> tuple[dict[str, Rec], dict[str, Thing], dict[str, list[tuple[str, str]]]]:
        suppliers, things = supplier_things(rd)
        vendors = _ap_vendors(rd)
        out: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for link_type in VENDOR_SUPPLIER_LINKS:
            for vid, ends in rd.linked(link_type, vendors, suppliers).items():
                out[vid].extend(ends)
        return suppliers, things, out
    return rd.memo("vendor_suppliers", make)


def one_party(row: Row, ends: Sequence[tuple[str, str]], things: Mapping[str, Thing]
              ) -> Thing | None:
    """The one real party ``ends`` reach (its copies' audience keys and the links'
    markings join the row), else None (none, or two different parties)."""
    held = [(c, marks) for c, marks in ends if c in things]
    if len({things[c].rep.object_id for c, _ in held}) != 1:
        return None
    for _c, marks in held:
        row.marks.add(marks)
    thing = things[held[0][0]]
    row.add_thing(thing)
    return thing


VENDOR_GROUP_DOC = ("the Sage AP vendor group of the document's vendor (idgrp: DEF, USD, PAY, PAYACH, "
                    "CONTU). It is how Sage books and pays the vendor, NOT a trade flag: trade "
                    "suppliers and non-trade payees (card issuers, tax bodies, utilities, "
                    "individuals' expenses) share DEF and USD, PAYACH pays by ACH, PAY is "
                    "payroll. Sage states no trade / non-trade split: separate them by supplier "
                    "name, never by group alone")


@view("supplier_invoices", "Every supplier document once: Sage AP documents of every kind "
      "(invoices, debit and credit notes, interest, prepayments, payments; paid or not: "
      "document_type tells them apart, an invoice count is document_type = 'invoice') and "
      "BC's pilot purchase invoices (pilot = true, left out unless asked for). Amounts in "
      "the document's own currency.",
      [("supplier", "text", "supplier name"), ("supplier_code", "text", "vendor code"),
       ("document_number", "text", "the supplier's document number"),
       ("document_type", "text", AP_DOC_TYPE_DOC),
       ("document_date", "date", "document date"), ("due_date", "date", "due date"),
       ("original_amount", "money", "document amount"),
       ("amount_due", "money", "still to pay (0 once paid)"),
       ("paid", "boolean", "fully paid: Sage swpaid 1, else (BC) amount_due 0"),
       ("po_number", "text", "the PO it bills, when stated"),
       ("description", "text", "Sage document description (descinvc)"),
       ("vendor_group", "text", VENDOR_GROUP_DOC),
       ("pilot", "boolean", PILOT_DOC),
       handle_col("supplier", "a Sage document's vendor (its RelSageApOpenDocument.vendor link) "
                  "and that vendor's settled link to a Portal or BC supplier "
                  f"({', '.join(VENDOR_SUPPLIER_LINKS)}); a BC invoice's "
                  "PurchaseInvoice.billed_by link")],
      reads=(AP_OPEN, "RelSageApVendor", "PurchaseInvoice", "Supplier"),
      memos=("suppliers", "vendor_suppliers"))
def _supplier_invoices(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    docs = rd.records(AP_OPEN, AP_PROPS)
    vendors = _ap_vendors(rd)
    owner = rd.linked(f"{AP_OPEN}.vendor", docs, vendors)
    for oid in sorted(docs):
        rec = docs[oid]
        row = _ap_row(rd, rec, vendors, owner, as_of)
        paid = rec.cell("swpaid")
        row.used(rec, paid)
        row.values["paid"] = None if paid is None else (_text(paid.value) or "") == "1"
        row.values["pilot"] = False
        yield row
    suppliers, things = supplier_things(rd)
    bc = rd.records("PurchaseInvoice", PURCHASE_INVOICE_PROPS)
    billed_by = rd.linked("PurchaseInvoice.billed_by", bc, suppliers)
    for thing in rd.clusters("PurchaseInvoice", bc):
        row = Row(thing)
        party_handle(row, "supplier", name_linked(
            row, [e for r in thing.members for e in billed_by.get(r.object_id, ())],
            things, "supplier"))
        fill(row, thing, "supplier_code", "vendor_number")
        if fill(row, thing, "document_number", "vendor_invoice_number") is None:
            fill(row, thing, "document_number", "bc_number")
        row.values["document_type"] = "invoice"
        fill(row, thing, "document_date", "invoice_date", to_date)
        fill(row, thing, "due_date", "due_date", to_date)
        money_of(row, thing, "original_amount", "total_amount")
        money_of(row, thing, "amount_due", "remaining_amount")
        left = row.values.get("amount_due")
        row.values["paid"] = None if left is None else left == 0
        fill(row, thing, "po_number", "order_number")
        row.values["description"] = None
        row.values["pilot"] = all(pilot_binding(r.binding) for r in thing.members)
        yield row


@view("supplier_balances", "What PLX owes suppliers now: every open (unpaid) Sage AP "
      "document with its remaining balance, aged by due date, in its own currency "
      "(prepayments and credit notes negative). BC aged payables are pilot data, not used.",
      [("supplier", "text", "supplier name"), ("supplier_code", "text", "vendor code"),
       ("document_number", "text", "the supplier's document number"),
       ("document_type", "text", AP_DOC_TYPE_DOC),
       ("document_date", "date", "document date"), ("due_date", "date", "due date"),
       ("amount_due", "money", "remaining balance, Sage AP amtduetc in codecurn; only documents "
        "Sage marks unpaid (swpaid 0)"),
       ("original_amount", "money", "document amount"),
       ("days_overdue", "integer", "as_of - due_date in days (Sage AP dateinvcdu); 0 or less: "
        "not yet due. Overdue = owed and past due: amount_due > 0 and days_overdue > 0; a "
        "credit, receipt or prepayment (amount_due < 0) is never overdue whatever its date "
        "(net it in only when a net balance is asked for)"),
       ("aging_bucket", "text", "days_overdue in buckets: current (0 or less), 1-30, "
        "31-60, 61-90, over 90"),
       ("po_number", "text", "the PO it bills, when stated"),
       ("description", "text", "Sage document description (descinvc)"),
       ("vendor_group", "text", VENDOR_GROUP_DOC),
       ("as_of", "date", "the day the aging was computed"),
       handle_col("supplier", "a Sage document's vendor (its RelSageApOpenDocument.vendor link) "
                  "and that vendor's settled link to a Portal or BC supplier "
                  f"({', '.join(VENDOR_SUPPLIER_LINKS)}); a BC invoice's "
                  "PurchaseInvoice.billed_by link")],
      reads=(AP_OPEN, "RelSageApVendor", "Supplier"),
      memos=("suppliers", "vendor_suppliers"), covers=False)   # the open documents only
def _supplier_balances(rd: Reader, as_of: dt.date) -> Iterator[Row]:
    docs = rd.records(AP_OPEN, AP_PROPS)
    vendors = _ap_vendors(rd)
    owner = rd.linked(f"{AP_OPEN}.vendor", docs, vendors)
    for oid in sorted(docs):
        rec = docs[oid]
        paid = rec.cell("swpaid")
        if paid is None or (_text(paid.value) or "") != "0":
            continue   # paid (or no paid flag stated): not open
        row = _ap_row(rd, rec, vendors, owner, as_of)
        row.used(rec, paid)
        days, bucket = aging_bucket(row.values.get("due_date"), as_of)
        row.values["days_overdue"], row.values["aging_bucket"] = days, bucket
        for col in ("days_overdue", "aging_bucket"):
            row.derive(col, row, "due_date")
        row.values["as_of"] = as_of
        yield row
