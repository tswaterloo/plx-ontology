"""The cross-system truth check: every claim the Today, Briefing and Late pages make, checked
against independent evidence in every captured system, after every publish and delta cycle.

The proactive engine judges each claim from the records its rule reads (a late purchase
order from its Business Central record, a lot's expiry from its FileMaker row). This check
reads every claim the engine left open in the gap inbox (and, given ``--late-json``, the
exact late list a reader was shown) and asks the other systems whether it holds:

* **Receipts everywhere.** A purchase order called open or late is looked up in BC, the
  Portal, FileMaker and Sage through the records' own stored identifiers (the Portal's
  ``app_bc_entity_map`` posting, BC's ``Your_Reference``, the Portal's
  ``fmGeneratedPoId``; never a number-text match), and each system's receipt state is
  read: BC receipt lines and ``fullyReceived``; FileMaker items' status, receipt rows and
  QC lots; Portal lines; Sage ``iscomplete`` and receipts. Goods received in any system
  contradict "late".
* **Cancellations and drafts.** A cancelled or draft record of the same order elsewhere.
* **Twins and number collisions.** The same order counted twice, and another system using
  the same number for a different order (never a twin).
* **Default dates.** A need date shared by a large share of a system's records (a bulk
  import or migration day), or equal to the day the record itself was written while no
  other system holds a requested date, is a default, not a requested-by date.
* **Test records.** Invoices, shipments and lines marked as tests, proofs, UAT or demos.
* **Mail and documents.** With ``--pg-dsn``: captured mail, Teams and documents naming the
  order beside "received", "delivered", "shipped" or "arrived" (support, never proof). The
  people exclusion (``PeopleExclusion.from_env`` with the mail capture's directory) decides
  every hit by mailbox, chat members and addresses; when it cannot load or decide, search is
  skipped entirely (fail closed).
* **The raw money profile** (``ontology.money_profile.health``): whether the profile
  readers use profiles the served mirror generation and is at most a week old, and how
  many visible columns of a sample trip its tripwire (a stale profile). Reported in
  ``status.json``, never the check's exit status.
* **Lots, batches, quantities, customer orders, margins** (the other claim kinds), and
  **engine health**: each of the 16 detectors' last run, whether it completed, and what
  it left unjudged; a claim type whose detector did not finish is reported, not trusted.

Every finding carries the citations it rests on. The rules are general (by claim kind and
system), never written for one record. The check is read-only everywhere: it opens every
store with ``mode=ro``, sets Postgres read-only, and writes only its own report directory
(``<state>/truth-check``: ``report.json``, ``report.md`` and ``status.json``, which the
Workbench's status line reads; ``operator_api.backend.truth_flag``).

Run: ``python3 -m operating.truth_check run --state-dir /data/plx-state [--pg-dsn ...]
[--late-json late.json] [--exclude-handle REGEX]``. Exit 0: no claim contradicted; 3: at
least one claim contradicted by evidence; 2: the check could not read its inputs.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

MIRROR = Path("case-runtime") / "source-mirror.sqlite"
BC_CAPTURE = Path("bc-capture") / "capture.sqlite"
INBOX = Path("projection") / "ontology.sqlite.gap-inbox.sqlite"
OUT_DIR = Path("truth-check")

PO, PO_LINE = "public.app_purchase_order", "public.app_purchase_order_line"
ENTITY_MAP = "public.app_bc_entity_map"
FM_PO, FM_PO_ITEM = "public.generated_purchase_order", "public.generated_purchase_order_item"
FM_RECEIPT, FM_RAW_LOT, FM_PACK_LOT = ("public.receipt", "public.receipt_raw_material",
                                       "public.receipt_packaging")
SAGE_PO, SAGE_PO_LINE = "sage.po_header", "sage.po_detail"
SAGE_RECEIPT = "sage.po_receipt_header"
BATCH, FM_BATCH = "public.app_batch_record", "public.batch"
MO = "public.app_manufacturing_order"
SALES_ORDER, SALES_ORDER_LINE = "public.app_sales_order", "public.app_sales_order_line"
FM_ORDER_ENTRY, FM_ORDER_ITEM = "public.order_entry", "public.order_entry_item"
SAGE_ORDER_LINE = "sage.oe_order_detail"
# The keys a Portal order's custom fields hold for its Sage order (the Sage ETL's stamps).
SAGE_ORDER_KEYS = ("sageOrdUniq", "sageSourceOrdUniq")
RELATIONS = (PO, PO_LINE, ENTITY_MAP, FM_PO, FM_PO_ITEM, FM_RECEIPT, FM_RAW_LOT, FM_PACK_LOT,
             SAGE_PO, SAGE_RECEIPT, BATCH, FM_BATCH, MO, SALES_ORDER, SALES_ORDER_LINE,
             FM_ORDER_ENTRY, FM_ORDER_ITEM, SAGE_ORDER_LINE)
BC_SETS = ("purchaseOrders", "purchaseOrderLines", "purchaseReceipts",
           "purchaseReceiptLines", "purchaseInvoices", "purchaseOrderReferences",
           "salesOrders", "salesOrderLines")

# The detectors the engine runs (``operating.proactive_engine.DETECTORS``; read from there
# when importable, so a new detector is never silently out of this check's health rule).
DETECTORS = ("late_or_unconfirmed_po", "eta_slipped", "lot_attention",
             "accepted_quote_no_order", "invoice_quantity_mismatch",
             "po_receipt_quantity_mismatch", "receipt_without_qc", "expired_quote",
             "conflicting_quantity", "late_order", "order_without_customer_po",
             "vendor_no_reply", "negative_margin", "orders_at_risk",
             "vendor_terms_unapplied", "card_rules")
# The claim kinds that say a purchase order is open (and late, or awaiting something).
PO_OPEN_KINDS = frozenset({"late_po", "unconfirmed_po", "late_order", "undated_po",
                           "po_impact", "systems_disagree"})
# FileMaker item words that close an item (received, over-received, cancelled).
FM_ITEM_DONE = frozenset({"complete", "overpo", "cancel", "cancelled", "closed", "close"})
FM_ITEM_CANCEL = frozenset({"cancel", "cancelled"})
PORTAL_DONE = frozenset({"complete", "received", "closed"})
PORTAL_CANCEL = frozenset({"cancel", "cancelled", "closed"})
# Lot QC states that hold no stock (used up, rejected, written off).
LOT_GONE = frozenset({"finished", "rejected", "consumed", "used", "written off", "scrapped"})
# Marks of a test, proof, UAT or demo record in any free-text or number field.
TEST_MARK = re.compile(r"\b(?:invoice ?test|test ?invoice|uat\b|uat-|demo-|[- ]demo\b|"
                       r"pipeline[- ]proof|o2c proof|banktest|\bproof line\b|"
                       r"plx-val-|\btest ?po\b)", re.I)
# Words beside an order's number in mail or documents that say the goods moved.
ARRIVAL = re.compile(r"\b(received|delivered|arrived|shipped|out for delivery|dispatched|"
                     r"completely received|bol|packing list|tracking)\b", re.I)
# Words beside an order's number that say the vendor confirmed, acknowledged or invoiced it.
CONFIRMING = re.compile(r"\b(order confirmation|confirm(?:ed|ing|s)?|acknowledg\w*|proforma|"
                        r"pro forma|invoice|sales order|so#|eta|etd)\b", re.I)
# A date that a large share of a system's records hold is a bulk default (an import or
# migration day): at least this share of the records and this many of them.
BULK_SHARE, BULK_MIN = 0.10, 10
DAY = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


# ---- sources ------------------------------------------------------------------------------

def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def mirror_rows(db: Path, relations: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """relation -> ``[{"row", "citation"}]``: the mirror's current rows, read-only (the
    citation form the app cites: ``api://<relation>/<key>@<generation>-<capture>``)."""
    conn = _ro(db)
    try:
        meta = dict(conn.execute("SELECT name, value FROM mirror_meta"))
        generation = meta.get("generation", "")
        out: dict[str, list[dict[str, Any]]] = {}
        for relation in relations:
            out[relation] = [
                {"row": json.loads(row), "citation":
                 f"api://{relation}/{quote(key, safe='')}@{generation}-{capture}"}
                for key, row, capture in conn.execute(
                    "SELECT key_json, row_json, capture_id FROM row_current WHERE relation = ?"
                    " AND deleted_in_source IS NULL", (relation,))]
        return out
    finally:
        conn.close()


def bc_records(db: Path, sets: Sequence[str]) -> list[dict[str, Any]]:
    """Every current captured BC record of ``sets``: ``{"entity_set", "id", "fields",
    "citation", "company_id"}``, read-only."""
    conn = _ro(db)
    try:
        generation = dict(conn.execute("SELECT key, value FROM meta")).get("generation", "")
        out = []
        for capture, unit, path, payload in conn.execute(
                "SELECT capture_id, unit, path, payload FROM versions WHERE kind = 'bc' AND "
                "superseded_at IS NULL AND missing_from_source_since IS NULL"):
            company, _, entity_set = unit.partition(":")[2].partition("/")
            if entity_set not in sets:
                continue
            fields = json.loads(payload)["row"]
            out.append({"company_id": company, "entity_set": entity_set,
                        "id": str(fields.get("id")), "fields": fields,
                        "citation": f"bc://{path}@{generation}-{capture}"})
        return out
    finally:
        conn.close()


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _num(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).strip()) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def _day(value: Any) -> str | None:
    found = DAY.search(str(value or ""))
    return found.group(1) if found and not found.group(1).startswith("0001") else None


@dataclass
class Sources:
    """The raw rows the check reads (the eval world's shape, so the eval family and the
    product monitor share one rule set): ``portal`` relation -> ``[{"row", "citation"}]``,
    ``bc`` ``[{"entity_set", "id", "fields", "citation"}]``; ``search(tokens)`` (optional)
    -> captured texts naming any token: ``[{"kind", "at", "title", "handle", "text"}]``."""

    portal: Mapping[str, Sequence[Mapping[str, Any]]]
    bc: Sequence[Mapping[str, Any]]
    as_of: date
    search: Callable[[Sequence[str]], list[dict[str, Any]]] | None = None
    _ix: dict[str, Any] = field(default_factory=dict)

    def rows(self, relation: str) -> Sequence[Mapping[str, Any]]:
        return self.portal.get(relation) or ()

    def index(self) -> dict[str, Any]:
        # built once; other caches share ``_ix`` (``_row_of``'s rows_by_id), so a lot or
        # batch claim checked first must never leave a partial index behind
        if self._ix.get("_built"):
            return self._ix
        ix = self._ix
        by = lambda: defaultdict(list)  # noqa: E731
        ix["bc"] = {(r["entity_set"], r["id"]): r for r in self.bc}
        ix["bc_po_number"] = by()
        ix["bc_lines"], ix["bc_receipts"], ix["bc_rlines"] = by(), by(), by()
        ix["bc_invoices"], ix["bc_refs"] = by(), {}
        for r in self.bc:
            f, es = r["fields"], r["entity_set"]
            if es == "purchaseOrders":
                ix["bc_po_number"][_norm(f.get("number"))].append(r)
            elif es == "purchaseOrderLines":
                ix["bc_lines"][f.get("documentId")].append(r)
            elif es == "purchaseReceipts":
                ix["bc_receipts"][_norm(f.get("orderNumber"))].append(r)
            elif es == "purchaseReceiptLines":
                ix["bc_rlines"][f.get("documentId")].append(r)
            elif es == "purchaseInvoices":
                ix["bc_invoices"][_norm(f.get("orderNumber"))].append(r)
            elif es == "purchaseOrderReferences":
                ix["bc_refs"][_norm(f.get("No"))] = r
        ix["po"] = {str(i["row"].get("id")): i for i in self.rows(PO)}
        ix["po_number"] = by()
        for i in self.rows(PO):
            row = i["row"]
            for number in {row.get("po_number"), (row.get("custom_fields") or {}).get(
                    "fmPoNumber") if isinstance(row.get("custom_fields"), dict) else None}:
                if number:
                    ix["po_number"][_norm(number)].append(i)
        ix["po_lines"] = by()
        for i in self.rows(PO_LINE):
            ix["po_lines"][str(i["row"].get("purchase_order_id"))].append(i)
        ix["map_bc"], ix["map_src"] = by(), by()
        for i in self.rows(ENTITY_MAP):
            row = i["row"]
            if row.get("entity_type") == "purchaseOrder" and row.get("status") == "LINKED" \
                    and _norm(row.get("bc_environment")) == "production":
                ix["map_bc"][str(row.get("bc_system_id"))].append(i)
                ix["map_src"][str(row.get("source_id"))].append(i)
        ix["fm"] = {str(i["row"].get("id")): i for i in self.rows(FM_PO)}
        ix["fm_number"] = by()
        for i in self.rows(FM_PO):
            ix["fm_number"][_norm(i["row"].get("ponumber"))].append(i)
        ix["fm_items"], ix["fm_receipts"], ix["fm_lots"] = by(), by(), by()
        for i in self.rows(FM_PO_ITEM):
            ix["fm_items"][str(i["row"].get("idpogen"))].append(i)
        for i in self.rows(FM_RECEIPT):
            ix["fm_receipts"][str(i["row"].get("idgenpoitem"))].append(i)
        for relation in (FM_RAW_LOT, FM_PACK_LOT):
            for i in self.rows(relation):
                ix["fm_lots"][str(i["row"].get("idpogenitem"))].append(i)
        ix["sage"], ix["sage_rcpt"] = by(), by()
        for i in self.rows(SAGE_PO):
            ix["sage"][_norm(i["row"].get("ponumber"))].append(i)
        for i in self.rows(SAGE_RECEIPT):
            ix["sage_rcpt"][_norm(i["row"].get("ponumber"))].append(i)
        ix["bulk_days"] = bulk_days(self)
        ix["_built"] = True
        return ix


def bulk_days(src: Sources) -> dict[str, dict[str, int]]:
    """Per date field of each system, the days a bulk share of its records hold (an import
    or migration day): field -> {day: records}. General: no day is named here."""
    fields = {
        "bc.purchaseOrderLines.expectedReceiptDate":
            [r["fields"].get("expectedReceiptDate") for r in src.bc
             if r["entity_set"] == "purchaseOrderLines"],
        "bc.purchaseOrders.orderDate":
            [r["fields"].get("orderDate") for r in src.bc if r["entity_set"] == "purchaseOrders"],
        "bc.purchaseOrders.lastModified":
            [r["fields"].get("lastModifiedDateTime") for r in src.bc
             if r["entity_set"] == "purchaseOrders"],
        "portal.app_purchase_order.date_generated":
            [i["row"].get("date_generated") for i in src.rows(PO)],
    }
    out: dict[str, dict[str, int]] = {}
    for name, values in fields.items():
        days = Counter(d for d in (_day(v) for v in values) if d)
        total = sum(days.values())
        out[name] = {d: n for d, n in days.items()
                     if total and n >= BULK_MIN and n / total >= BULK_SHARE}
    return out


# ---- one purchase order across every system -------------------------------------------------

def parse_object(object_id: str) -> tuple[str, str, dict[str, Any]]:
    """``Type:binding:{key}`` -> (type, binding, key)."""
    kind, _, rest = object_id.partition(":")
    binding, _, key = rest.partition(":")
    try:
        parsed = json.loads(key) if key else {}
    except ValueError:
        parsed = {}
    return kind, binding, parsed if isinstance(parsed, dict) else {}


@dataclass
class Group:
    bc: list[Mapping[str, Any]] = field(default_factory=list)
    portal: list[Mapping[str, Any]] = field(default_factory=list)
    fm: list[Mapping[str, Any]] = field(default_factory=list)
    sage: list[Mapping[str, Any]] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)
    collisions: list[Mapping[str, Any]] = field(default_factory=list)


def _add(items: list, item: Mapping[str, Any] | None) -> bool:
    if item is not None and all(i is not item for i in items):
        items.append(item)
        return True
    return False


def order_group(src: Sources, object_id: str) -> Group:
    """The records of one order in every system, joined only on stored identifiers: the
    Portal's posting map (Portal order -> BC order), BC ``Your_Reference`` naming exactly
    one Portal order, the Portal's ``fmGeneratedPoId`` (-> FileMaker), and the same order
    number in Sage. Another system's record holding the same number but not joined is a
    number collision (a different order), never a twin."""
    ix = src.index()
    _, binding, key = parse_object(object_id)
    g = Group()
    ident = str(key.get("id", ""))
    if binding.startswith("bc."):
        _add(g.bc, ix["bc"].get(("purchaseOrders", ident)))
    elif binding.endswith("app_purchase_order"):
        _add(g.portal, ix["po"].get(ident))
    elif "generated_purchase_order" in binding:
        _add(g.fm, ix["fm"].get(ident))
    for _ in range(3):   # a few passes close the joins in every direction
        for b in list(g.bc):
            for m in ix["map_bc"].get(b["id"], []):
                if _add(g.portal, ix["po"].get(str(m["row"].get("source_id")))):
                    g.joins.append(f"posting map {m['citation']}")
            ref = ix["bc_refs"].get(_norm(b["fields"].get("number")))
            named = _norm(ref["fields"].get("Your_Reference")) if ref else ""
            # a reference naming the order's own number names no other record
            hits = ix["po_number"].get(named, []) if named and named != _norm(
                b["fields"].get("number")) else []
            if len(hits) == 1 and _add(g.portal, hits[0]):
                g.joins.append(f"Your_Reference {ref['citation']}")
        for p in list(g.portal):
            row = p["row"]
            for m in ix["map_src"].get(str(row.get("id")), []):
                if _add(g.bc, ix["bc"].get(("purchaseOrders", str(m["row"].get(
                        "bc_system_id"))))):
                    g.joins.append(f"posting map {m['citation']}")
            cf = row.get("custom_fields") if isinstance(row.get("custom_fields"), dict) else {}
            fm_id = cf.get("fmGeneratedPoId") or (cf.get("fmId") if cf.get("fmTable") is None
                                                  and str(row.get("id", "")).startswith("fm-po-")
                                                  else None)
            if fm_id is not None and _add(g.fm, ix["fm"].get(str(fm_id))):
                g.joins.append(f"fmGeneratedPoId {p['citation']}")
        for f in list(g.fm):
            for p in src.rows(PO):
                cf = p["row"].get("custom_fields")
                if isinstance(cf, dict) and str(cf.get("fmGeneratedPoId")) == str(
                        f["row"].get("id")) and _add(g.portal, p):
                    g.joins.append(f"fmGeneratedPoId {p['citation']}")
    numbers = {_norm(b["fields"].get("number")) for b in g.bc} | {
        _norm(p["row"].get("po_number")) for p in g.portal} | {
        _norm(f["row"].get("ponumber")) for f in g.fm}
    numbers.discard("")
    for n in numbers:
        for s in ix["sage"].get(n, []):
            _add(g.sage, s)
        for other in ix["po_number"].get(n, []):
            if all(other is not p for p in g.portal):
                g.collisions.append({"system": "portal", "citation": other["citation"],
                                     "number": n, "status": other["row"].get("status")})
        for other in ix["bc_po_number"].get(n, []):
            if all(other is not b for b in g.bc):
                g.collisions.append({"system": "bc", "citation": other["citation"],
                                     "number": n, "status": other["fields"].get("status")})
    return g


def _reversed(lines: Sequence[Mapping[str, Any]]) -> bool:
    qty = [_num(r["fields"].get("quantity")) for r in lines]
    return any(q is not None and q < 0 for q in qty)


def po_evidence(src: Sources, g: Group) -> dict[str, Any]:
    """Each system's receipt state of one order, with the citations it rests on."""
    ix = src.index()
    ev: dict[str, Any] = {"bc": [], "fm": [], "portal": [], "sage": [], "tests": [],
                          "requested_dates": []}
    for b in g.bc:
        f = b["fields"]
        lines = ix["bc_lines"].get(b["id"], [])
        open_lines = [l for l in lines if (_num(l["fields"].get("quantity")) or 0) > 0 and (
            _num(l["fields"].get("receivedQuantity")) or 0) < (_num(l["fields"].get(
                "quantity")) or 0)]
        receipts = ix["bc_receipts"].get(_norm(f.get("number")), [])
        rlines = [rl for r in receipts for rl in ix["bc_rlines"].get(r["id"], [])]
        invoices = ix["bc_invoices"].get(_norm(f.get("number")), [])
        ev["bc"].append({
            "citation": b["citation"], "number": f.get("number"), "status": f.get("status"),
            "fully_received": f.get("fullyReceived"), "lines": len(lines),
            "open_lines": len(open_lines),
            "line_dates": sorted({d for d in (_day(l["fields"].get("expectedReceiptDate"))
                                             for l in lines) if d}),
            "order_date": _day(f.get("orderDate")), "posting_date": _day(f.get("postingDate")),
            "written": _day(f.get("lastModifiedDateTime")),
            "requested": _day(f.get("requestedReceiptDate")),
            "receipts": [{"number": r["fields"].get("number"), "posted": _day(
                r["fields"].get("postingDate")), "citation": r["citation"]} for r in receipts],
            "receipts_undone": _reversed(rlines),
            "invoices": [r["citation"] for r in invoices]})
        if _day(f.get("requestedReceiptDate")):
            ev["requested_dates"].append(("bc", _day(f.get("requestedReceiptDate")),
                                          b["citation"]))
        texts = [(r["fields"].get("vendorInvoiceNumber"), r["citation"]) for r in invoices]
        texts += [(r["fields"].get("description"), r["citation"]) for r in lines]
        texts += [(r["fields"].get(k), r["citation"]) for r in receipts
                  for k in ("vendorShipmentNumber", "vendorInvoiceNumber")]
        ev["tests"] += [{"text": str(t)[:80], "citation": c} for t, c in texts
                        if t and TEST_MARK.search(str(t))]
    for p in g.portal:
        row = p["row"]
        lines = ix["po_lines"].get(str(row.get("id")), [])
        cf = row.get("custom_fields") if isinstance(row.get("custom_fields"), dict) else {}
        full = bool(lines) and all((_num(l["row"].get("quantity_received")) or 0) >= (
            _num(l["row"].get("quantity")) or 0) > 0 for l in lines)
        ev["portal"].append({
            "citation": p["citation"], "number": row.get("po_number"),
            "status": row.get("status"), "updated": _day(row.get("updated_at")),
            "created": _day(row.get("created_at")),
            # an FM-mirrored order's received quantities are ETL copies, not a receipt
            "mirrored": bool(cf.get("fmGeneratedPoId") or cf.get("fmId")),
            "lines_received_in_full": full, "received_date": _day(row.get("received_date")),
            "closed_at": _day(row.get("closed_at"))})
        if _day(row.get("expected_date")):
            ev["requested_dates"].append(("portal", _day(row.get("expected_date")),
                                          p["citation"]))
    for f in g.fm:
        row = f["row"]
        items = []
        for i in ix["fm_items"].get(str(row.get("id")), []):
            irow = i["row"]
            receipts = ix["fm_receipts"].get(str(irow.get("id")), [])
            lots = ix["fm_lots"].get(str(irow.get("id")), [])
            items.append({
                "citation": i["citation"], "status": irow.get("status"),
                "qty": irow.get("qty"),
                "receipts": [{"date": _day(r["row"].get("datereceived")),
                              "status": r["row"].get("status"), "citation": r["citation"]}
                             for r in receipts],
                # a QC lot's ``sumqty`` is a running total across lots, never this lot's
                # quantity: only the lot's existence and status are read
                "lots": [{"status": l["row"].get("status_c"), "citation": l["citation"]}
                         for l in lots]})
        ev["fm"].append({"citation": f["citation"], "number": row.get("ponumber"),
                         "status": row.get("status"), "items": items})
        if _day(row.get("datearrive")):
            ev["requested_dates"].append(("fm", _day(row.get("datearrive")), f["citation"]))
    for s in g.sage:
        row = s["row"]
        ev["sage"].append({"citation": s["citation"], "number": str(row.get("ponumber")).strip(),
                           "complete": str(row.get("iscomplete")).strip() == "1",
                           "receipts": [r["citation"] for r in src.index()["sage_rcpt"].get(
                               _norm(row.get("ponumber")), [])]})
    return ev


def _fm_received(fm: Mapping[str, Any]) -> tuple[bool, list[str], list[str]]:
    """(every live item settled by FileMaker, the receipt and lot citations, the items
    still open: not settled, a partial receipt included). A QC lot counts as a receipt
    only once QC gave it a status: a status-less packaging lot row is a placeholder."""
    cites, open_items = [], []
    live = [i for i in fm["items"] if _norm(i["status"]) not in FM_ITEM_CANCEL]
    for item in live:
        got = [r["citation"] for r in item["receipts"]] + [l["citation"] for l in item["lots"]
                                                           if l["status"]]
        word = _norm(item["status"])
        if word in FM_ITEM_DONE:
            cites += got or [item["citation"]]   # FileMaker closed the item itself
        elif word or not got:
            # a partial receipt never closes an item (owner, 2026-11-06): a back-ordered
            # or waiting item is open, partly received when goods are recorded against it
            cites += got
            open_items.append(item["citation"])
        else:
            cites += got   # no word served: its receipts are the only evidence
    return bool(live) and not open_items and bool(cites), cites, open_items


def po_verdict(src: Sources, object_id: str, need: str | None = None) -> dict[str, Any]:
    """One purchase-order claim checked against every system: ``verdict`` one of
    ``test_record``, ``received_elsewhere``, ``cancelled_elsewhere``, ``draft_twin``,
    ``date_not_real``, ``supported``, ``unknown``; ``contradicted`` when the evidence
    says the order is not open-and-late as claimed; every reason with its citations."""
    g = order_group(src, object_id)
    ev = po_evidence(src, g)
    reasons: list[dict[str, Any]] = []

    def reason(rule: str, text: str, cites: Iterable[str]) -> None:
        reasons.append({"rule": rule, "text": text, "citations": list(dict.fromkeys(cites))[:8]})

    bc_open = any(_norm(b["status"]) not in ("draft",) and b["fully_received"] is not True
                  and b["open_lines"] for b in ev["bc"])
    if ev["tests"]:
        reason("T1", "the order carries test/UAT/demo marks: " + "; ".join(
            sorted({t["text"] for t in ev["tests"]}))[:200], (t["citation"] for t in ev["tests"]))
    fm_open = False
    for fm in ev["fm"]:
        done, cites, open_items = _fm_received(fm)
        # FileMaker's own order still waiting on items under a header that closes nothing
        fm_open = fm_open or bool(open_items) and _norm(fm["status"]) not in (
            FM_ITEM_DONE | FM_ITEM_CANCEL)
        if done:
            reason("P1-fm", f"FileMaker {fm['number']}: every item is received or closed "
                   f"(header says {fm['status']!r})", [fm["citation"], *cites])
        elif cites and open_items:
            reason("P1-fm-partial", f"FileMaker {fm['number']}: {len(open_items)} item(s) "
                   "still open (none or part received); others received",
                   [fm["citation"], *cites])
        if fm["items"] and all(_norm(i["status"]) in FM_ITEM_CANCEL for i in fm["items"]):
            reason("P2-fm", f"FileMaker {fm['number']}: every item cancelled", [fm["citation"]])
    for p in ev["portal"]:
        word = _norm(p["status"])
        if word in PORTAL_CANCEL - {"closed"}:
            reason("P2-portal", f"Portal {p['number']} is {p['status']}", [p["citation"]])
        elif not p["mirrored"] and (word in PORTAL_DONE or p["lines_received_in_full"]):
            reason("P1-portal", f"Portal {p['number']} is {p['status']} with its lines "
                   "received", [p["citation"]])
        if word == "draft" and p["updated"] and p["updated"] < f"{src.as_of.year - 1}":
            reason("P3", f"the order's Portal twin {p['number']} is a draft last changed "
                   f"{p['updated']}: an old draft carried into BC, not an issued order",
                   [p["citation"]])
    for s in ev["sage"]:
        if s["complete"]:
            reason("P1-sage", f"Sage {s['number']} is complete", [s["citation"], *s["receipts"]])
    for b in ev["bc"]:
        if b["receipts_undone"]:
            reason("P5", f"BC {b['number']}: receipts were posted and then undone (negative "
                   "receipt lines)", [r["citation"] for r in b["receipts"]])
    # the need date: requested by someone, or a default
    bulk = src.index()["bulk_days"]
    if need:
        defaults = []
        for name, days in bulk.items():
            if need in days:
                defaults.append(f"{days[need]} records of {name} hold {need}")
        for b in ev["bc"]:
            if need in (b["written"], b["posting_date"], b["order_date"]) and not \
                    ev["requested_dates"]:
                defaults.append(f"BC {b['number']} was written on {need} and no system holds "
                                "a requested-by date")
        if defaults:
            reason("D1", "the need date is a default, not a requested-by date: "
                   + "; ".join(defaults), [b["citation"] for b in ev["bc"]]
                   + [p["citation"] for p in ev["portal"]])
    for c in g.collisions:
        reason("N1", f"{c['system']} uses the number {c['number']} for a different order "
               f"({c['status']}): not a twin", [c["citation"]])
    mail = []
    if src.search is not None:
        tokens = sorted({str(x) for x in [*(b["number"] for b in ev["bc"]),
                                          *(p["number"] for p in ev["portal"]),
                                          *(f["number"] for f in ev["fm"])] if x})
        found = src.search(tokens)
        mail = [h for h in found if ARRIVAL.search(h.get("text") or "")]
        confirms = [h for h in found if CONFIRMING.search(h.get("text") or "")]
        if confirms:
            reason("E2", f"{len(confirms)} captured message(s)/document(s) name the order "
                   "beside confirmation, invoice or ETA words: a claim of no confirmation "
                   "needs a look", [h["handle"] for h in confirms])
        if mail:
            reason("E1", f"{len(mail)} captured message(s)/document(s) name the order beside "
                   "received/shipped/delivered words (support, not proof)",
                   [h["handle"] for h in mail])
    rules = {r["rule"] for r in reasons}
    if "T1" in rules:
        verdict = "test_record"
    elif rules & {"P1-fm", "P1-portal", "P1-sage"}:
        verdict = "received_elsewhere"
    elif rules & {"P2-fm", "P2-portal"}:
        verdict = "cancelled_elsewhere"
    elif "P3" in rules:
        verdict = "draft_twin"
    elif "D1" in rules:
        verdict = "date_not_real"
    elif bc_open or fm_open or any(_norm(p["status"]) not in PORTAL_DONE | PORTAL_CANCEL
                                   for p in ev["portal"]):
        verdict = "supported"
    else:
        verdict = "unknown"
    return {"object_id": object_id, "verdict": verdict,
            "contradicted": verdict in {"test_record", "received_elsewhere",
                                        "cancelled_elsewhere", "draft_twin", "date_not_real"},
            "needs_check": "E1" in rules and verdict == "supported",
            "systems": {k: len(getattr(g, k)) for k in ("bc", "portal", "fm", "sage")},
            "joins": g.joins, "reasons": reasons, "evidence": ev}


# ---- the other claim kinds ------------------------------------------------------------------

def _row_of(src: Sources, object_id: str) -> Mapping[str, Any] | None:
    """The mirror row an object id names (``portal.app_x`` -> ``public.app_x``,
    ``portal.fm_x[.field]`` -> ``public.x``)."""
    _, binding, key = parse_object(object_id)
    name = binding.split(".")[1] if binding.startswith("portal.") else None
    if not name:
        return None
    relation = "public." + (name[3:] if name.startswith("fm_") else name)
    ident = str(key.get("id"))
    cache = src._ix.setdefault("rows_by_id", {})
    if relation not in cache:
        cache[relation] = {str(i["row"].get("id")): i for i in src.rows(relation)}
    return cache[relation].get(ident)


def lot_verdict(src: Sources, gap: Mapping[str, Any]) -> dict[str, Any]:
    """A lot near expiry holds stock: its QC state is not used up or rejected."""
    item = _row_of(src, gap["object_id"])
    if item is None:
        return {"verdict": "unknown", "contradicted": False, "reasons": [
            {"rule": "L0", "text": "the lot's source row is not in the mirror", "citations": []}]}
    status = item["row"].get("status_c") or item["row"].get("status")
    if _norm(status) in LOT_GONE:
        return {"verdict": "lot_gone", "contradicted": True, "reasons": [
            {"rule": "L1", "text": f"the lot is {status}: no stock left to expire",
             "citations": [item["citation"]]}]}
    expiry = _day(item["row"].get("expirydate") or item["row"].get("expiry_date"))
    if expiry and expiry < src.as_of.isoformat():
        return {"verdict": "expired", "contradicted": True, "reasons": [
            {"rule": "L5", "text": f"the lot already expired on {expiry}",
             "citations": [item["citation"]]}]}
    return {"verdict": "supported", "contradicted": False, "reasons": [
        {"rule": "L1", "text": f"the lot is {status or 'without a QC status'}",
         "citations": [item["citation"]]}]}


def batch_verdict(src: Sources, gap: Mapping[str, Any]) -> dict[str, Any]:
    """A batch "on hold": the hold value is a real hold only when it is not the value
    nearly every batch in the same lifecycle state holds (a default)."""
    item = _row_of(src, gap["object_id"])
    if item is None:
        return {"verdict": "unknown", "contradicted": False, "reasons": []}
    relation = BATCH if "app_batch_record" in gap["object_id"] else FM_BATCH
    qc_field = "status_qc" if relation == BATCH else "statusqc"
    row = item["row"]
    peers = [i["row"] for i in src.rows(relation) if i["row"].get("status") == row.get("status")]
    same = sum(1 for p in peers if p.get(qc_field) == row.get(qc_field))
    if len(peers) >= 20 and same / len(peers) >= 0.95:
        return {"verdict": "default_value", "contradicted": True, "reasons": [
            {"rule": "L3", "text": f"{same} of {len(peers)} {row.get('status')} batches hold "
             f"{qc_field}={row.get(qc_field)!r}: a default, not a hold",
             "citations": [item["citation"]]}]}
    return {"verdict": "supported", "contradicted": False, "reasons": [
        {"rule": "L3", "text": f"{same} of {len(peers)} peers share the value",
         "citations": [item["citation"]]}]}


def quantity_verdict(src: Sources, gap: Mapping[str, Any]) -> dict[str, Any]:
    """Two sources disagreeing on one quantity: a format pair (one stores the other's value
    rounded to an integer) is no disagreement; a closed record leaves nothing to act on."""
    values = [Decimal(v) for v in re.findall(r"(-?\d+(?:\.\d+)?)", gap["summary"].split(
        "(")[-1])][:2]
    reasons = []
    if len(values) == 2:
        a, b = sorted(values, key=lambda v: (v != v.to_integral_value(), v))
        if a == a.to_integral_value() and b != b.to_integral_value() and a in (
                b.to_integral_value(rounding="ROUND_FLOOR"),
                b.to_integral_value(rounding="ROUND_HALF_UP")):
            reasons.append({"rule": "Q1", "text": f"{a} is {b} stored as an integer (format, "
                            "not a disagreement)", "citations": [gap["object_id"]]})
    item = _row_of(src, gap["object_id"])
    if item is not None and _norm(item["row"].get("status")) in {
            "realized", "cancelled", "canceled", "closed", "completed", "complete"}:
        reasons.append({"rule": "Q2", "text": f"the record is {item['row'].get('status')}: "
                        "nothing left to act on", "citations": [item["citation"]]})
    return {"verdict": "format_or_closed" if reasons else "supported",
            "contradicted": any(r["rule"] == "Q1" for r in reasons), "reasons": reasons}


def at_risk_verdict(src: Sources, gap: Mapping[str, Any], cites: Sequence[Mapping[str, Any]]
                    ) -> dict[str, Any]:
    """A customer order held up by a late PO: the cited PO must still be open in every
    system (else the cause given is wrong, whatever the order's own state)."""
    pos = sorted({c["object_id"] for c in cites if (c.get("object_id") or "").startswith(
        "PurchaseOrder:")})
    checks = [po_verdict(src, oid) for oid in pos]
    bad = [c for c in checks if c["contradicted"]]
    return {"verdict": "cause_wrong" if bad else "supported", "contradicted": bool(bad),
            "reasons": [r for c in checks for r in c["reasons"]]}


# A customer order's own status words: closed in either system (the Portal's SHIPPED,
# COMPLETE, CANCELLED; FileMaker's Closed, Complete), open (the Portal's open codes and
# FileMaker's Open).
SO_CLOSED = frozenset({"closed", "shipped", "invoiced", "cancelled", "canceled", "complete",
                       "completed", "delivered", "realized"})
SO_OPEN = frozenset({"open", "ready_to_ship", "confirmed", "in_production",
                     "partially_shipped"})


def _so_index(src: Sources) -> dict[str, Any]:
    """The sales-order joins, built once: Portal and FileMaker rows by id, the stored keys
    between them (the Portal's ``custom_fields.fmId``; a Portal copy without one by its
    order number = FileMaker's customer PO code for the same customer, the Portal holding
    FileMaker customers as ``fm-cust-<idcust>``), their lines and items, the BC order a
    Portal order was posted as (entity map), and each relation's bulk required dates."""
    ix = src._ix
    if "so_built" in ix:
        return ix
    by = lambda: defaultdict(list)  # noqa: E731
    ix["so_portal"] = {str(i["row"].get("id")): i for i in src.rows(SALES_ORDER)}
    ix["so_fm"] = {str(i["row"].get("id")): i for i in src.rows(FM_ORDER_ENTRY)}
    ix["so_lines"], ix["so_items"], ix["so_by_fm"], ix["so_fm_by_po"] = by(), by(), by(), by()
    for i in src.rows(SALES_ORDER_LINE):
        ix["so_lines"][str(i["row"].get("sales_order_id"))].append(i)
    for i in src.rows(FM_ORDER_ITEM):
        ix["so_items"][str(i["row"].get("idoe"))].append(i)
    for oid, i in ix["so_portal"].items():
        fm_id = _fm_of(i["row"])
        if fm_id is not None:
            ix["so_by_fm"][fm_id].append(i)
    for i in src.rows(FM_ORDER_ENTRY):
        row = i["row"]
        if _norm(row.get("custpocode")) and row.get("idcust") is not None:
            ix["so_fm_by_po"][(_norm(row.get("custpocode")), f"fm-cust-{row.get('idcust')}")
                              ].append(i)
    ix["so_sage_lines"], ix["so_bc_lines"] = by(), by()
    for i in src.rows(SAGE_ORDER_LINE):
        ix["so_sage_lines"][str(i["row"].get("orduniq"))].append(i)
    for r in src.bc:
        if r["entity_set"] == "salesOrderLines":
            ix["so_bc_lines"][str(r["fields"].get("documentId"))].append(r)
    ix["so_posted"] = {}
    bc_orders = {r["id"]: r for r in src.bc if r["entity_set"] == "salesOrders"}
    for i in src.rows(ENTITY_MAP):
        row = i["row"]
        if row.get("entity_type") == "salesOrder" and row.get("status") == "LINKED" \
                and str(row.get("bc_system_id")) in bc_orders:
            ix["so_posted"][str(row.get("source_id"))] = bc_orders[str(row.get("bc_system_id"))]
    ix["so_bulk"] = {}
    for relation, column in ((SALES_ORDER, "required_date"), (FM_ORDER_ENTRY, "datereq")):
        days = Counter(d for d in (_day(i["row"].get(column)) for i in src.rows(relation)) if d)
        total = sum(days.values())
        ix["so_bulk"][relation] = {d: n for d, n in days.items()
                                   if n >= 10 and total and n / total >= 0.10}
    ix["so_built"] = True
    return ix


def _fm_of(row: Mapping[str, Any]) -> str | None:
    cf = row.get("custom_fields") if isinstance(row.get("custom_fields"), dict) else {}
    return str(cf["fmId"]) if cf.get("fmId") not in (None, "") else None


def customer_order_verdict(src: Sources, object_id: str) -> dict[str, Any]:
    """A customer order called open past its required date, checked in every copy: closed
    in any copy (C3: its own status, its FileMaker or Portal copy by the stored fmId, a
    Portal copy of a closed FileMaker order of the same customer and customer PO code, or
    the BC order it was posted as fully shipped), no goods on any copy (C4: no Portal, Sage or BC line,
    no FileMaker item), or a required date that is a default (C5: the record's own order or
    creation day, or a day a bulk share of its relation holds) contradicts the claim."""
    ix = _so_index(src)
    _, binding, key = parse_object(object_id)
    ident = str(key.get("id"))
    portal, fm = [], []
    if binding == "portal.app_sales_order" and ident in ix["so_portal"]:
        portal.append(ix["so_portal"][ident])
    elif binding == "portal.fm_order_entry" and ident in ix["so_fm"]:
        fm.append(ix["so_fm"][ident])
    if not portal and not fm:
        return {"verdict": "unknown", "contradicted": False, "reasons": [
            {"rule": "C0", "text": "the order's source row is not in the mirror",
             "citations": []}]}
    # every copy joined by a stored key
    for i in list(portal):
        fm_id = _fm_of(i["row"])
        if fm_id in ix["so_fm"]:
            fm.append(ix["so_fm"][fm_id])
    for i in list(fm):
        portal += [p for p in ix["so_by_fm"].get(str(i["row"].get("id")), []) if p not in portal]
    copies = [i for i in portal if _fm_of(i["row"]) is None]
    dupes = [f for i in copies for f in ix["so_fm_by_po"].get(
        (_norm(i["row"].get("order_number")), str(i["row"].get("customer_id"))), [])]
    reasons: list[dict[str, Any]] = []
    for i in [*portal, *fm, *dupes]:
        word = _norm(i["row"].get("status")).replace(" ", "_")
        if word in SO_CLOSED:
            said = f"a copy of the order is {i['row'].get('status')}"
            if i in dupes:
                said += " (the same customer's order under this customer PO code)"
            reasons.append({"rule": "C3", "text": said, "citations": [i["citation"]]})
    for i in portal:
        bc = ix["so_posted"].get(str(i["row"].get("id")))
        if bc is not None and bc["fields"].get("fullyShipped") is True:
            reasons.append({"rule": "C3", "text": f"BC {bc['fields'].get('number')}, the order "
                            "the Portal posted it as, is fully shipped",
                            "citations": [bc["citation"]]})
    goods = [*(l["citation"] for i in portal for l in ix["so_lines"].get(
        str(i["row"].get("id")), [])), *(t["citation"] for i in fm for t in ix["so_items"].get(
            str(i["row"].get("id")), []))]
    # lines that exist only in Sage (the Portal order's stored Sage order key) or in BC (the
    # order it was posted as) still state goods
    for i in portal:
        custom = i["row"].get("custom_fields")
        for name in SAGE_ORDER_KEYS if isinstance(custom, dict) else ():
            key = str(custom.get(name) or "").strip()
            for form in {key, key.partition(":")[2]} - {""}:
                goods += [l["citation"] for l in ix["so_sage_lines"].get(form, [])]
        bc = ix["so_posted"].get(str(i["row"].get("id")))
        if bc is not None:
            goods += [l["citation"] for l in ix["so_bc_lines"].get(bc["id"], [])]
    if not goods:
        reasons.append({"rule": "C4", "text": "no copy of the order holds a line: no goods are "
                        "stated on it", "citations": [i["citation"] for i in [*portal, *fm]]})
    for relation, rows, column, own in ((SALES_ORDER, portal, "required_date",
                                         ("order_date", "created_at")),
                                        (FM_ORDER_ENTRY, fm, "datereq",
                                         ("audittimestampcreated",))):
        for i in rows:
            day = _day(i["row"].get(column))
            if day and (day in ix["so_bulk"][relation]
                        or day in {_day(i["row"].get(c)) for c in own}):
                reasons.append({"rule": "C5", "text": f"the required date {day} is a default "
                                "(the record's own day or a bulk day of its system)",
                                "citations": [i["citation"]]})
    rules = {r["rule"] for r in reasons}
    verdict = ("closed_elsewhere" if "C3" in rules else "no_goods" if "C4" in rules
               else "date_not_real" if "C5" in rules else "supported")
    return {"verdict": verdict, "contradicted": verdict != "supported", "reasons": reasons}


def probes(src: Sources, claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """False negatives: what the sources say that no open claim reports (general rules)."""
    def ident(object_id: Any) -> tuple[str, str, str] | None:
        # an object id compared by its parts, never its JSON spelling ({"id": 1} and
        # {"id":1} and {"id":"1"} name one record)
        if not isinstance(object_id, str):
            return None
        kind, binding, key = parse_object(object_id)
        return (kind, binding, str(key.get("id"))) if key.get("id") is not None else None

    def named(kinds: set[str]) -> set[tuple[str, str, str]]:
        # every record a claim of these kinds is about or cites (a claim on a twin cites
        # the other copy beside it)
        out_ = set()
        for c in claims:
            if c.get("kind") in kinds:
                out_ |= {i for i in [ident(c["object_id"]), *(ident(x.get("object_id"))
                                                              for x in c.get("cites") or ())]
                         if i is not None}
        return out_
    out = []
    # L6: a batch made but never released by QC is a real hold. A Portal batch record the
    # ETL copied from FileMaker batches (the stored pair namebatch_c = batch_name,
    # ``Batch.same_record_as``) is decided by those origins, counted on them: the copy's
    # QC value is whatever the last upsert left, never a hold of its own
    origins = {_norm(i["row"].get("namebatch_c")) for i in src.rows(FM_BATCH)} - {""}
    for relation, qc_field, binding in ((BATCH, "status_qc", "portal.app_batch_record"),
                                        (FM_BATCH, "statusqc", "portal.fm_batch")):
        held = [i for i in src.rows(relation) if _norm(i["row"].get("status")) == "realized"
                and _norm(i["row"].get(qc_field)) not in {"released", "rejected", ""}
                and not (relation == BATCH and _norm(i["row"].get("batch_name")) in origins)]
        holds = named({"lot_on_hold"})
        missed = [i for i in held
                  if ("Batch", binding, str(i["row"].get("id"))) not in holds]
        if missed:
            out.append({"rule": "L6", "kind": "lot_on_hold", "count": len(missed),
                        "text": f"{len(missed)} {relation} batches were made but never "
                        "released by QC and no claim reports them",
                        "citations": [i["citation"] for i in missed[:10]]})
    # C7: a date field empty on nearly every record is one systemic gap, not N findings
    for relation, column in ((PO, "expected_date"), (FM_PO, "datearrive")):
        rows = src.rows(relation)
        filled = sum(1 for i in rows if _day(i["row"].get(column)))
        if rows and filled / len(rows) < 0.05:
            out.append({"rule": "C7", "kind": "undated_po", "count": len(rows) - filled,
                        "text": f"{relation}.{column} is empty on {len(rows) - filled} of "
                        f"{len(rows)} records: 'no due date' is systemic, not per order",
                        "citations": [i["citation"] for i in rows[:3]]})
    # C2: a customer order open past its own required date: open in the Portal, its
    # required date passed, and not contradicted in any copy (``customer_order_verdict``:
    # closed elsewhere, no goods, a default date)
    today = src.as_of.isoformat()
    overdue = [i for i in src.rows(SALES_ORDER)
               if _norm(i["row"].get("status")).replace(" ", "_") in SO_OPEN
               and (_day(i["row"].get("required_date")) or "9999") < today]
    # any claim about the order credits it (an overdue customer order, or one at risk)
    about = named({"order_at_risk", "late_customer_order", "po_impact"})
    unclaimed = []
    for i in overdue:
        if ("SalesOrder", "portal.app_sales_order", str(i["row"].get("id"))) in about:
            continue
        if not customer_order_verdict(
                src, f'SalesOrder:portal.app_sales_order:{json.dumps({"id": i["row"].get("id")})}'
        )["contradicted"]:
            unclaimed.append(i)
    if unclaimed:
        out.append({"rule": "C2", "kind": "late_customer_order", "count": len(unclaimed),
                    "text": f"{len(unclaimed)} Portal sales orders are open past their "
                    "required date (not closed in any copy, with goods and a real date) and "
                    "no claim reports them",
                    "citations": [i["citation"] for i in unclaimed[:10]]})
    return out


# ---- claims ----------------------------------------------------------------------------------

def inbox_claims(inbox: Path) -> list[dict[str, Any]]:
    """Every open gap of the inbox with its cites (what Today and the Briefing draw on)."""
    conn = _ro(inbox)
    try:
        conn.row_factory = sqlite3.Row
        gaps = [dict(r) for r in conn.execute(
            "SELECT gap_id, kind, detector, object_id, object_type, summary, follow_up, "
            "first_seen, last_seen FROM gaps WHERE state = 'open'")]
        cites: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in conn.execute("SELECT gap_id, grp, handle, object_id FROM gap_cites"):
            cites[r["gap_id"]].append(dict(r))
    finally:
        conn.close()
    for g in gaps:
        g["cites"] = cites.get(g["gap_id"], [])
        try:
            g["follow_up"] = json.loads(g["follow_up"])
        except (TypeError, ValueError):
            g["follow_up"] = {}
    return gaps


def detector_health(inbox: Path, now: datetime) -> list[dict[str, Any]]:
    """H1-H3: each detector's latest run, its last complete run, and what it left unjudged."""
    try:
        from operating.proactive_engine import DETECTORS as names  # the live list
    except Exception:  # noqa: BLE001 - the copy above when the engine cannot be imported
        names = DETECTORS
    conn = _ro(inbox)
    try:
        latest = {r[0]: r for r in conn.execute(
            "SELECT detector, status, reasons, found, not_evaluated, run_at FROM detector_runs")}
        complete = dict(conn.execute(
            "SELECT detector, max(run_at) FROM detector_history WHERE status = 'complete' "
            "GROUP BY detector"))
        try:
            latest_run = (conn.execute("SELECT max(run_at) FROM runs").fetchone() or [None])[0]
            stale_gaps = dict(conn.execute(
                "SELECT detector, count(*) FROM gaps WHERE state = 'open' AND last_seen < ? "
                "GROUP BY detector", (latest_run or "",)))
        except sqlite3.OperationalError:   # an inbox without runs or gaps yet
            stale_gaps = {}
    finally:
        conn.close()
    out = []
    for name in names:
        row = latest.get(name)
        last = complete.get(name)
        age_h = None
        if last:
            try:
                age_h = round((now - datetime.fromisoformat(last)).total_seconds() / 3600, 1)
            except ValueError:
                age_h = None
        try:
            unjudged = sum(json.loads(row[4]).values()) if row and row[4] else 0
        except (ValueError, TypeError, AttributeError):
            unjudged = 0
        flags = []
        if row is None or row[1] != "complete":
            flags.append(f"latest run {row[1] if row else 'missing'}"
                         f" ({row[2] if row else ''})")
        if last is None:
            flags.append("never completed")
        elif age_h is not None and age_h > 24:
            flags.append(f"last complete {age_h} h ago")
        if row is not None and unjudged and row[3] is not None and unjudged > max(row[3], 1) * 4:
            flags.append(f"{unjudged} left unjudged against {row[3]} found")
        if stale_gaps.get(name):   # C12: open claims no run over today's data confirmed
            flags.append(f"{stale_gaps[name]} open claims not confirmed by the latest run")
        out.append({"detector": name, "status": row[1] if row else None,
                    "found": row[3] if row else None, "last_complete": last,
                    "unjudged": unjudged, "flags": flags})
    return out


def _need(claim: Mapping[str, Any]) -> str | None:
    return _day(claim.get("summary")) if claim.get("kind") in {
        "late_po", "unconfirmed_po", "late_order"} else None


def check(src: Sources, claims: Sequence[Mapping[str, Any]],
          late_list: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Every claim checked by its kind's rules; counts per kind and the late list's own."""
    results = []
    po_cache: dict[tuple[str, str | None], dict[str, Any]] = {}

    def po(oid: str, need: str | None) -> dict[str, Any]:
        if (oid, need) not in po_cache:
            po_cache[(oid, need)] = po_verdict(src, oid, need)
        return po_cache[(oid, need)]

    for c in claims:
        kind = c["kind"]
        if kind in PO_OPEN_KINDS:
            r = po(c["object_id"], _need(c))
            if kind == "systems_disagree" and r["verdict"] in {"received_elsewhere",
                                                                "test_record", "draft_twin"}:
                r = dict(r, verdict="governing_side_wrong")
            if kind == "undated_po" and r["verdict"] == "date_not_real":
                r = dict(r, verdict="supported", contradicted=False)
        elif kind == "receipt_reconciliation":
            # "one copy records the goods received, another still shows them open": holds
            # only when some system did receive them
            r = po(c["object_id"], None)
            r = dict(r, verdict="supported", contradicted=False) \
                if r["verdict"] == "received_elsewhere" else \
                dict(r, verdict="not_received_anywhere", contradicted=True)
        elif kind == "lot_near_expiry":
            r = lot_verdict(src, c)
        elif kind == "lot_on_hold":
            r = batch_verdict(src, c)
        elif kind == "conflicting_quantity":
            r = quantity_verdict(src, c)
        elif kind == "order_at_risk":
            r = at_risk_verdict(src, c, c.get("cites", []))
        elif kind == "late_customer_order":
            r = customer_order_verdict(src, c["object_id"])
        else:
            r = {"verdict": "not_checked", "contradicted": False, "reasons": []}
        # a claim that says late: late_po, or an unconfirmed order whose need date passed
        late_claim = kind == "late_po" or (kind == "unconfirmed_po" and (
            c.get("follow_up") or {}).get("days_late") is not None)
        order = ((c.get("follow_up") or {}).get("order") or {}).get("key") or c["object_id"]
        results.append({"gap_id": c.get("gap_id"), "kind": kind, "late_claim": late_claim,
                        "order": order,
                        "detector": c.get("detector"),
                        "object_id": c["object_id"], "summary": c.get("summary"), **{
                            k: r.get(k) for k in ("verdict", "contradicted", "needs_check",
                                                  "reasons", "systems", "joins")}})
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by_kind[r["kind"]]["checked"] += 1
        by_kind[r["kind"]]["contradicted" if r["contradicted"] else "verdict:" + r["verdict"]] += 1
    late = None
    if late_list is not None:
        rows = []
        for item in late_list:
            r = po(item["object_id"], item.get("due_date"))
            rows.append({"label": item.get("label"), "supplier": item.get("supplier"),
                         "due_date": item.get("due_date"), "object_id": item["object_id"],
                         **{k: r[k] for k in ("verdict", "contradicted", "reasons", "systems",
                                               "joins")}})
        late = {"shown": len(rows), "contradicted": sum(r["contradicted"] for r in rows),
                "verdicts": dict(Counter(r["verdict"] for r in rows)), "rows": rows}
    return {"results": results, "by_kind": {k: dict(v) for k, v in by_kind.items()},
            "late_list": late}


# ---- search (mail, Teams, documents) ---------------------------------------------------------

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def people_guard(state_dir: Path | None, environ: Mapping[str, str]) -> tuple[Any, str | None]:
    """The people exclusion (``PeopleExclusion.from_env``, with the mail capture's directory
    so senders can be decided) and None, or None and why search must be skipped: the check
    reads no captured message it cannot prove is no excluded person's (fail closed)."""
    try:
        from integrations.people_exclusion import PeopleExclusion, with_directory
    except Exception as exc:  # noqa: BLE001 - no guard, no search
        return None, f"exclusion_unloadable:{type(exc).__name__}"
    guard = with_directory(PeopleExclusion.from_env(environ), state_dir)
    if guard.unavailable:
        return None, guard.unavailable
    if not (guard.bound or guard.directory_loaded):
        return None, "excluded_people_unresolved"
    return guard, None


def excluded_hit(guard: Any, hit: Mapping[str, Any]) -> bool:
    """Whether a captured text may be an excluded person's: its mailbox or a chat member
    (object ids in the handle) is excluded, or an address in it is; an address of the
    tenant the guard cannot decide counts as excluded (withheld, never guessed)."""
    from urllib.parse import unquote
    try:
        for oid in UUID.findall(unquote(str(hit.get("handle") or ""))):
            if guard.is_excluded(oid):
                return True
        tenant = guard.known_domains()
        for address in ADDRESS.findall(f"{hit.get('header') or ''}\n{hit.get('text') or ''}"):
            address = address.lower()
            if guard.decides(address):
                if guard.is_excluded(address):
                    return True
            elif address.rsplit("@", 1)[1] in tenant:
                return True
    except Exception:  # noqa: BLE001 - a guard that refuses to decide withholds the text
        return True
    return False


def pg_search(dsn: str, guard: Any, exclude: str | None = None, limit: int = 20
              ) -> Callable[[Sequence[str]], list[dict[str, Any]]]:
    """Captured texts naming any token (``search.chunks`` full text), read-only. Every hit
    passes the people exclusion (``excluded_hit``: mailbox, chat members, sender and every
    address in it) before it is used; handles matching ``exclude`` (a regex) are not read."""
    def run(tokens: Sequence[str]) -> list[dict[str, Any]]:
        parts = []
        for t in tokens:
            words = [w for w in re.split(r"[^0-9A-Za-z]+", t.lower()) if w]
            if words:
                parts.append("(" + " <-> ".join(f"'{w}'" for w in words) + ")")
        if not parts:
            return []
        query = " | ".join(parts)
        where = "tsv @@ to_tsquery('simple', %s)" % _sql_text(query)
        if exclude:
            where += " AND source_handle !~* %s" % _sql_text(exclude)
        sql = ("BEGIN; SET TRANSACTION READ ONLY; SET LOCAL statement_timeout = 60000; "
               "SELECT json_build_object('kind', kind, 'at', occurred_at, 'title', title, "
               "'handle', source_handle, 'header', header, 'text', left(body, 2000)) FROM "
               f"search.chunks WHERE {where} ORDER BY occurred_at DESC NULLS LAST "
               f"LIMIT {int(limit) * 3}; COMMIT;")
        done = subprocess.run(["psql", dsn, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                              capture_output=True,
                              text=True, timeout=120)
        if done.returncode != 0:
            return []
        hits = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
        return [h for h in hits if not excluded_hit(guard, h)][:limit]
    return run


def _sql_text(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ---- report -----------------------------------------------------------------------------------

def report_md(out: Mapping[str, Any]) -> str:
    lines = [f"# Truth check {out['generated_at']}", "",
             f"Business day {out['as_of']}. Claims checked: {len(out['results'])}; contradicted "
             f"by evidence: {sum(r['contradicted'] for r in out['results'])}.", ""]
    if out.get("late_list"):
        late = out["late_list"]
        lines += [f"## Late list as shown: {late['shown']} orders, {late['contradicted']} "
                  "contradicted", "", "| PO | Supplier | Due | Verdict | Why (first reason) |",
                  "|---|---|---|---|---|"]
        for r in late["rows"]:
            why = r["reasons"][0]["text"] if r["reasons"] else ""
            lines.append(f"| {r['label']} | {r['supplier']} | {r['due_date']} | {r['verdict']} "
                         f"| {why[:160]} |")
        lines.append("")
    lines += ["## Claims by kind", "", "| Kind | Checked | Contradicted | Other verdicts |",
              "|---|---|---|---|"]
    for kind, counts in sorted(out["by_kind"].items()):
        other = ", ".join(f"{k[8:]} {v}" for k, v in sorted(counts.items())
                          if k.startswith("verdict:"))
        lines.append(f"| {kind} | {counts.get('checked', 0)} | {counts.get('contradicted', 0)}"
                     f" | {other} |")
    lines += ["", "## Not claimed but in the sources (false negatives)", ""]
    lines += [f"- {p['rule']} ({p['kind']}): {p['text']}" for p in out.get("probes", [])] or [
        "- none found"]
    lines += ["", "## Detectors", "", "| Detector | Latest | Found | Last complete | Flags |",
              "|---|---|---|---|---|"]
    for d in out["detectors"]:
        lines.append(f"| {d['detector']} | {d['status']} | {d['found']} | {d['last_complete']} "
                     f"| {'; '.join(d['flags'])} |")
    return "\n".join(lines) + "\n"


def status_of(out: Mapping[str, Any]) -> dict[str, Any]:
    """What the status line reads: counts only, no record content."""
    late = out.get("late_list") or {}
    # late orders (one per order, as the late list shows them): claims past their need
    # date only, never an unconfirmed order not yet due
    orders: dict[str, bool] = {}
    for r in out["results"]:
        if r.get("late_claim"):
            key = r.get("order") or r["object_id"]
            orders[key] = orders.get(key, True) and bool(r["contradicted"])
    return {"generated_at": out["generated_at"], "as_of": out["as_of"],
            "late_claims": len(orders),
            "late_contradicted": sum(orders.values()),
            "late_list_shown": late.get("shown"), "late_list_contradicted": late.get("contradicted"),
            "contradicted": sum(r["contradicted"] for r in out["results"]),
            "claims": len(out["results"]),
            "missed": dict(sum((Counter({p["rule"]: p["count"]}) for p in out.get(
                "probes", [])), Counter())),
            "detectors_incomplete": [d["detector"] for d in out["detectors"] if d["flags"]],
            "money_profile_ok": bool((out.get("money_profile") or {}).get("ok")),
            "money_profile_problems": list((out.get("money_profile") or {}).get("problems")
                                           or ()),
            "money_tripwire_columns": ((out.get("money_profile") or {}).get("tripwire")
                                       or {}).get("columns")}


def _money_profile(state_dir: Path) -> dict[str, Any]:
    """The raw money profile's health (``ontology.money_profile.health``): whether the
    profile readers use is current and how many visible columns of a sample trip."""
    try:
        from ontology import money_profile
        return money_profile.health(state_dir)
    except Exception as error:  # noqa: BLE001 - reported, never the check's failure
        return {"ok": False, "problems": [f"unavailable:{type(error).__name__}"]}


def _derived_code(state_dir: Path) -> dict[str, Any] | None:
    """The code this report was made by and the versions of the claims and profile it
    read (``operating.derived_status``): a later engine run makes it stale."""
    try:
        from operating import derived_status as ds
        return ds.record("truth-check", state_dir=state_dir,
                         inputs=ds.inputs_of("truth-check", state_dir))
    except Exception as error:  # noqa: BLE001 - reported, never the check's failure
        return {"code": None, "error": type(error).__name__}


def run(state_dir: Path, *, out_dir: Path | None = None, pg_dsn: str | None = None,
        exclude: str | None = None, late_json: Path | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        as_of = now.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001
        as_of = now.date()
    guard, skipped = people_guard(state_dir, os.environ) if pg_dsn else (None, "no_dsn")
    src = Sources(portal=mirror_rows(state_dir / MIRROR, RELATIONS),
                  bc=bc_records(state_dir / BC_CAPTURE, BC_SETS), as_of=as_of,
                  search=pg_search(pg_dsn, guard, exclude) if guard is not None else None)
    claims = inbox_claims(state_dir / INBOX)
    late = None
    if late_json is not None:
        raw = json.loads(late_json.read_text())
        late = raw.get("items", raw) if isinstance(raw, dict) else raw
    out = {"generated_at": now.isoformat(), "as_of": as_of.isoformat(),
           "search": "used" if src.search is not None else f"skipped: {skipped}",
           **check(src, claims, late), "probes": probes(src, claims),
           "detectors": detector_health(state_dir / INBOX, now),
           "money_profile": _money_profile(state_dir),
           "derived_code": _derived_code(state_dir)}
    target = out_dir or state_dir / OUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    for name, body in (("report.json", json.dumps(out, indent=1, default=str)),
                       ("report.md", report_md(out)),
                       ("status.json", json.dumps(status_of(out), indent=1))):
        tmp = target / f".{name}.tmp"
        tmp.write_text(body)
        os.replace(tmp, target / name)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.truth_check")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check every open claim against every system")
    r.add_argument("--state-dir", type=Path, required=True)
    r.add_argument("--out-dir", type=Path)
    r.add_argument("--pg-dsn", help="Postgres DSN of search.chunks (mail, Teams, documents)")
    r.add_argument("--exclude-handle", help="regex: source handles never read")
    r.add_argument("--late-json", type=Path, help="the late list as /api/late returned it")
    args = parser.parse_args(argv)
    try:
        out = run(args.state_dir, out_dir=args.out_dir, pg_dsn=args.pg_dsn,
                  exclude=args.exclude_handle, late_json=args.late_json)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"truth check could not read its inputs: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    status = status_of(out)
    print(json.dumps(status))
    return 3 if status["contradicted"] else 0


if __name__ == "__main__":
    sys.exit(main())
