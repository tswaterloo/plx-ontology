"""Raw-row oracle for "is this late order really late": the lateness oracle's late order
groups (``evals.oracle.po_lateness``) judged against every captured system's own records.

Independent of the product: it runs no Workbench code (the import ban,
``scripts/check_oracle_imports.py``) and shares none of the product truth check's rules or
joins. The order groups and their records are ``po_lateness``'s own (stored identifiers
only); this module only reads raw rows of those records:

* **received**: every live FileMaker item of the group's FileMaker order has a receiving
  row (``receipt.idgenpoitem``), a QC lot that QC gave a status (``receipt_raw_material`` /
  ``receipt_packaging`` ``idpogenitem`` with ``status_c``; a lot's ``sumqty`` is a running
  total and never read), or a closing item word (``Complete``, ``OverPO``); or a Sage order
  of the same number has ``iscomplete`` 1; or BC's posted receipt lines of the order cover
  every ordered line (net of reversals);
* **cancelled**: every FileMaker item is ``Cancel``, or a Portal header says cancelled;
* **test**: an invoice, receipt or line of the BC order carries a test, UAT, demo or proof
  mark;
* **draft**: the group's Portal header is a ``DRAFT`` last changed over a year before;
* **default date**: the governing due date is a bulk day of BC's line dates (held by at
  least 10% and 10 of them) or the BC header's own last-modified day.

A group none of these holds is late. Input: ``portal`` relation -> ``[{"row",
"citation"}]`` and ``bc`` ``[{"entity_set", "id", "fields", "citation"}]`` (the eval world).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Sequence

FM_ITEM = "public.generated_purchase_order_item"
FM_RECEIPT = "public.receipt"
FM_LOTS = ("public.receipt_raw_material", "public.receipt_packaging")
SAGE_PO = "sage.po_header"
PORTAL_PO = "public.app_purchase_order"
RELATIONS = (FM_ITEM, FM_RECEIPT, *FM_LOTS, SAGE_PO)
BC_SETS = ("purchaseOrders", "purchaseOrderLines", "purchaseReceipts", "purchaseReceiptLines",
           "purchaseInvoices")
ITEM_CLOSED = {"complete", "overpo"}
ITEM_CANCELLED = {"cancel", "cancelled"}
MARK = re.compile(r"invoice\s?test|test\s?invoice|\buat\b|uat-|demo-|pipeline.proof|"
                  r"o2c proof|banktest", re.I)
_DAY = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def _day(value: Any) -> date | None:
    found = _DAY.search(str(value or ""))
    if not found or found.group(1) == "0001":
        return None
    return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))


def _qty(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _word(value: Any) -> str:
    return str(value or "").strip().lower()


class Reads:
    """The raw rows, indexed once."""

    def __init__(self, portal: Mapping[str, Sequence[Mapping[str, Any]]],
                 bc: Sequence[Mapping[str, Any]]) -> None:
        self.by_citation: dict[str, Mapping[str, Any]] = {}
        for relation in (PORTAL_PO, "public.generated_purchase_order"):
            for item in portal.get(relation) or ():
                self.by_citation[item["citation"]] = item
        for record in bc:
            self.by_citation[record["citation"]] = record
        self.items: dict[str, list] = defaultdict(list)
        for item in portal.get(FM_ITEM) or ():
            self.items[str(item["row"].get("idpogen"))].append(item)
        self.received: dict[str, list[str]] = defaultdict(list)
        for item in portal.get(FM_RECEIPT) or ():
            self.received[str(item["row"].get("idgenpoitem"))].append(item["citation"])
        for relation in FM_LOTS:
            for item in portal.get(relation) or ():
                if _word(item["row"].get("status_c")):
                    self.received[str(item["row"].get("idpogenitem"))].append(item["citation"])
        self.sage: dict[str, list] = defaultdict(list)
        for item in portal.get(SAGE_PO) or ():
            self.sage[_word(item["row"].get("ponumber"))].append(item)
        self.bc_lines: dict[str, list] = defaultdict(list)
        self.bc_receipts: dict[str, list] = defaultdict(list)
        self.bc_receipt_lines: dict[str, list] = defaultdict(list)
        self.bc_invoices: dict[str, list] = defaultdict(list)
        line_days: Counter = Counter()
        for record in bc:
            f, kind = record["fields"], record["entity_set"]
            if kind == "purchaseOrderLines":
                self.bc_lines[str(f.get("documentId"))].append(record)
                if _day(f.get("expectedReceiptDate")):
                    line_days[_day(f.get("expectedReceiptDate"))] += 1
            elif kind == "purchaseReceipts":
                self.bc_receipts[_word(f.get("orderNumber"))].append(record)
            elif kind == "purchaseReceiptLines":
                self.bc_receipt_lines[str(f.get("documentId"))].append(record)
            elif kind == "purchaseInvoices":
                self.bc_invoices[_word(f.get("orderNumber"))].append(record)
        total = sum(line_days.values())
        self.bulk_days = {d for d, n in line_days.items()
                          if n >= 10 and total and n * 10 >= total}


def judge(reads: Reads, group: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    """One late group: ``late`` True unless a raw record says otherwise; ``why`` the
    reasons and ``cites`` the records they rest on."""
    why: list[str] = []
    cites: list[str] = []
    heads = [(h["system"], reads.by_citation.get(h["citation"])) for h in group["headers"]]
    for kind, head in heads:
        if head is None:
            continue
        if kind == "fm":
            items = reads.items.get(str(head["row"].get("id")), [])
            live = [i for i in items if _word(i["row"].get("status")) not in ITEM_CANCELLED]
            if items and not live:
                why.append("cancelled in FileMaker")
                cites.append(head["citation"])
            got = [reads.received.get(str(i["row"].get("id"))) or (
                [i["citation"]] if _word(i["row"].get("status")) in ITEM_CLOSED else [])
                for i in live]
            if live and all(got):
                why.append("received in FileMaker")
                cites += [head["citation"], *(c for g in got for c in g)]
        elif kind == "portal":
            row = head["row"]
            if _word(row.get("status")) in ITEM_CANCELLED:
                why.append("cancelled in the Portal")
                cites.append(head["citation"])
            changed = _day(row.get("updated_at"))
            if _word(row.get("status")) == "draft" and changed and (as_of - changed).days > 365:
                why.append("an old Portal draft")
                cites.append(head["citation"])
            for sage in reads.sage.get(_word(row.get("po_number")), []):
                if str(sage["row"].get("iscomplete")).strip() == "1":
                    why.append("complete in Sage")
                    cites.append(sage["citation"])
        elif kind == "bc":
            f = head["fields"]
            number = _word(f.get("number"))
            lines = reads.bc_lines.get(head["id"], [])
            posted: Counter = Counter()
            for receipt in reads.bc_receipts.get(number, []):
                for line in reads.bc_receipt_lines.get(receipt["id"], []):
                    posted[_word(line["fields"].get("description"))] += _qty(
                        line["fields"].get("quantity"))
            ordered: Counter = Counter()
            for line in lines:
                ordered[_word(line["fields"].get("description"))] += _qty(
                    line["fields"].get("quantity"))
            if ordered and all(posted[k] >= v for k, v in ordered.items() if v > 0):
                why.append("received in BC (posted receipt lines)")
                cites += [r["citation"] for r in reads.bc_receipts.get(number, [])]
            texts = [(i["fields"].get("vendorInvoiceNumber"), i["citation"])
                     for i in reads.bc_invoices.get(number, [])]
            texts += [(l["fields"].get("description"), l["citation"]) for l in lines]
            marked = [c for t, c in texts if t and MARK.search(str(t))]
            if marked:
                why.append("a test record")
                cites += marked
            due = [_day(d) for d in group.get("governing_dates") or ()]
            written = _day(f.get("lastModifiedDateTime"))
            if due and all(d in reads.bulk_days or d == written for d in due if d):
                why.append("its due date is a bulk or record-write default")
                cites.append(head["citation"])
    return {"numbers": group.get("numbers"), "late": not why, "why": sorted(set(why)),
            "cites": sorted(set(cites))}


def really_late(portal: Mapping[str, Sequence[Mapping[str, Any]]],
                bc: Sequence[Mapping[str, Any]], groups: Sequence[Mapping[str, Any]],
                as_of: date) -> list[dict[str, Any]]:
    """Every late group with its verdict."""
    reads = Reads(portal, bc)
    return [judge(reads, g, as_of) for g in groups]
