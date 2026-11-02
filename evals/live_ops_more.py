"""More live-ops families: every system the Workbench captures, and the questions an
executive asks beyond the first set (``evals.live_ops_keys`` holds the first set and the
shared helpers; it merges ``FAMILIES`` from here).

Each family reads the raw captured rows of one system, read-only, never the product's
projection or answers, and stores the query it used (``reads``, ``rule``):

* Business Central: inventory on hand, the purchase price trend from posted receipts, a
  customer's open sales orders, received against ordered on an order (posted receipts),
  the vendor's line date against our due date;
* Portal: a customer's open sales orders, the purchase price trend from Portal PO lines,
  quotes accepted with no order;
* FileMaker: who created and who last edited a PO, raw lots in quarantine (FileMaker's QC
  receiving quantities carry no unit, so received against ordered is read from BC);
* Sage: a customer's balance, when an order shipped;
* mail: the last message with a customer; Teams: the last message naming a PO;
  SharePoint: the file named for a lot; Mission Control: a task's stage and owner.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import re
from typing import Any, Callable, Mapping, Sequence

from evals.live_ops import date_forms
from evals.oracle.keys import FM_PO, FM_PO_ITEM, PO, PO_LINE
from evals.oracle.source import decimal_of, instant, key_text

CUSTOMER = "public.app_customer"
PRODUCT = "public.app_product"
QUOTE = "public.app_quote"
FM_RECEIPT_PACK = "public.receipt_packaging"
SAGE_CUSTOMER = "sage.ar_customer"
SAGE_ORDER = "sage.oe_order_header"
SAGE_SHIPMENT = "sage.oe_shipment_header"
OPEN_PORTAL_SO = frozenset({"Open", "OPEN", "READY_TO_SHIP"})
ACCEPTED_QUOTE = frozenset({"ACCEPTED", "Accepted", "WON"})
RECENT_DAYS = 365
_NAME = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,40}$")
SO_TOKEN = r"S-ORD\d{3,8}|SO\d{3,8}|SO-\d{3,8}"
TASK_TOKEN = r"TASK-\d{1,6}"


def _system_of(handle: str) -> str:
    return handle.partition("://")[0]


# --------------------------------------------------------------------------- FileMaker

def fm_po_people(world: lk.World) -> list[lk.Candidate]:
    """Who created a FileMaker-era PO and who last edited it, and when."""
    out = []
    for item in world.rows(FM_PO):
        row = item["row"]
        number, creator, editor = (str(row.get(k) or "").strip()
                                   for k in ("ponumber", "auditcreator", "auditeditor"))
        touched = instant(row.get("audittimestampmodified"))
        if not number or touched is None or not _NAME.match(creator) \
                or not _NAME.match(editor) or (world.as_of - touched.date()).days > RECENT_DAYS:
            continue
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            lk._fact("text", "created_by", [item["citation"]], ("api",), value=creator),
            lk._fact("text", "last_edited_by", [item["citation"]], ("api",), value=editor),
            lk._fact("date", "last_edited", [item["citation"]], ("api",),
                     value=touched.date().isoformat())],
            ["portal mirror generated_purchase_order: auditcreator, auditeditor, "
             "audittimestampmodified"],
            f"the FileMaker PO's creator and last editor, edited within {RECENT_DAYS} days"))
    return out


def fm_quarantine(world: lk.World) -> list[lk.Candidate]:
    """Raw lots FileMaker QC holds in quarantine (distinct lot numbers)."""
    lots: dict[str, list[str]] = defaultdict(list)
    for item in world.rows(lk.RAW_LOT):
        lot = str(item["row"].get("lotnoqc") or "").strip()
        if lot and item["row"].get("status_c") == "Quarantine":
            lots[lot].append(item["citation"])
    if not lots:
        return []
    return [lk.Candidate("live-ops:quarantine", "Lot", {}, [
        lk._fact("number", "quarantined", [c for cs in lots.values() for c in cs], ("api",),
                 value=str(len(lots)), label=["quarantine", "quarantined", "lots", "lot",
                                              "blocked", "hold"], tolerance="0")],
        ["portal mirror receipt_raw_material: distinct lotnoqc with status_c Quarantine"],
        "count of distinct raw lots in quarantine")]


# --------------------------------------------------------------------------- BC

def bc_inventory(world: lk.World) -> list[lk.Candidate]:
    out = []
    for record in world.bc_set("items"):
        fields = record["fields"]
        stock = decimal_of(fields.get("inventory"))
        if fields.get("type") != "Inventory" or not stock or stock <= 0 \
                or not fields.get("number"):
            continue
        name = re.split(r"[\r\n]| Hint:", str(fields.get("displayName") or ""))[0].strip()
        name = name if len(name) <= 40 else name[:40].rsplit(" ", 1)[0]
        code = str(fields["number"])
        out.append(lk.Candidate(code, "Product", {"item": f"{code} ({name})"}, [
            lk._fact("number", "on_hand", [record["citation"]], ("bc",), value=str(stock),
                     unit=str(fields.get("baseUnitOfMeasureCode") or "") or None,
                     label=["on hand", "inventory", "stock", "have", "available", "qty",
                            "quantity"], tolerance="0.01", tolerance_pct=lk.MONEY_PCT)],
            ["BC items.inventory (type Inventory, above 0)"], "the item's inventory in BC"))
    return out


def _trend(prices: Sequence[tuple[str, Decimal, str]]) -> tuple[Decimal, Decimal, str] | None:
    """(last, previous, up/down) over (day, price, citation), one price per day."""
    by_day: dict[str, set[Decimal]] = defaultdict(set)
    for day, price, _c in prices:
        by_day[day].add(price)
    days = sorted(d for d in by_day if d)
    if len(days) < 2 or len(by_day[days[-1]]) != 1 or len(by_day[days[-2]]) != 1:
        return None
    last, before = next(iter(by_day[days[-1]])), next(iter(by_day[days[-2]]))
    if last == before:
        return None
    return last, before, "up" if last > before else "down"


def _trend_facts(code: str, prices: Sequence[tuple[str, Decimal, str]], system: str,
                 markings: Sequence[str]) -> list[dict[str, Any]] | None:
    found = _trend(prices)
    if found is None:
        return None
    last, before, way = found
    cites = [c for _d, _p, c in prices]
    return [lk._fact("claim", "direction", cites, (system,), value=way,
                     accept=(["went up", "increased", "higher", "rising"] if way == "up"
                             else ["went down", "decreased", "lower", "dropping"]),
                     contradicts=(["went down", "decreased", "dropped", "lower"] if way == "up"
                                  else ["went up", "increased", "rose", "higher"])),
            lk._fact("number", "last_price", cites, (system,), value=_plain(last),
                     label=lk.LABELS["last_price"],
                     tolerance="0.01", tolerance_pct=lk.MONEY_PCT, markings=list(markings))]


def bc_price_trend(world: lk.World) -> list[lk.Candidate]:
    """The direction of an item's posted receipt unit cost (BC), last against the one
    before."""
    receipts = {str(r["fields"].get("id")): r for r in world.bc_set("purchaseReceipts")}
    prices: dict[str, list[tuple[str, Decimal, str]]] = defaultdict(list)
    for line in world.bc_set("purchaseReceiptLines"):
        fields = line["fields"]
        head = receipts.get(str(fields.get("documentId")))
        cost = decimal_of(fields.get("unitCost"))
        if fields.get("lineType") != "Item" or head is None or not cost or cost <= 0:
            continue
        prices[str(fields.get("lineObjectNumber"))].append(
            (str(head["fields"].get("postingDate") or ""), cost, line["citation"]))
    out = []
    for code, rows in prices.items():
        facts = _trend_facts(code, rows, "bc", ["purchasing_prices"])
        if facts:
            out.append(lk.Candidate(code, "Product", {"item": code}, facts,
                                    ["BC purchaseReceiptLines.unitCost by lineObjectNumber, "
                                     "dated by purchaseReceipts.postingDate"],
                                    "the last posted receipt cost against the one before"))
    return out


def bc_open_sos(world: lk.World) -> list[lk.Candidate]:
    by_customer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in world.bc_set("salesOrders"):
        fields = record["fields"]
        if str(fields.get("fullyShipped")).lower() == "false" and fields.get("customerName"):
            by_customer[str(fields["customerName"])].append(record)
    return [lk.Candidate(name, "Customer", {"customer": name}, [
        lk._fact("set", "open_orders", [r["citation"] for r in rows], ("bc",),
                 members=[[str(r["fields"]["number"])] for r in rows], pattern=SO_TOKEN)],
        ["BC salesOrders where customerName = customer and fullyShipped = false"],
        "the customer's BC sales orders not fully shipped")
        for name, rows in by_customer.items() if 1 <= len(rows) <= lk.LIST_MAX]


def bc_receipt_vs_po(world: lk.World) -> list[lk.Candidate]:
    """Received (BC posted receipt lines of the order) against ordered (its BC order lines),
    per item, where an item's received total differs from its ordered quantity."""
    orders = {str(r["fields"].get("number")): r for r in world.bc_set("purchaseOrders")}
    lines: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for line in world.bc_set("purchaseOrderLines"):
        lines[str(line["fields"].get("documentId")).lower()].append(line)
    receipt_lines: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for line in world.bc_set("purchaseReceiptLines"):
        receipt_lines[str(line["fields"].get("documentId")).lower()].append(line)
    received: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for receipt in world.bc_set("purchaseReceipts"):
        received[str(receipt["fields"].get("orderNumber") or "")].extend(
            [receipt, *receipt_lines[str(receipt["fields"].get("id")).lower()]])
    out = []
    for number, rows in received.items():
        order = orders.get(number)
        if order is None:
            continue
        ordered: dict[str, Decimal] = defaultdict(Decimal)
        for line in lines[str(order["fields"].get("id")).lower()]:
            if line["fields"].get("lineType") == "Item":
                ordered[str(line["fields"].get("lineObjectNumber") or "")] += (
                    decimal_of(line["fields"].get("quantity")) or Decimal(0))
        got: dict[str, Decimal] = defaultdict(Decimal)
        for line in rows:
            if line["entity_set"] == "purchaseReceiptLines" and \
                    line["fields"].get("lineType") == "Item":
                got[str(line["fields"].get("lineObjectNumber") or "")] += (
                    decimal_of(line["fields"].get("quantity")) or Decimal(0))
        off = sorted(i for i in set(ordered) | set(got) if i and ordered[i] != got[i])
        if len(off) != 1 or not ordered.get(off[0]):
            continue   # one item differing: one figure pair to ask about
        item = off[0]
        cites = [order["citation"], *(l["citation"] for l in lines[
            str(order["fields"].get("id")).lower()]), *(r["citation"] for r in rows)]
        way = "short" if got[item] < ordered[item] else "over"
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number, "item": item}, [
            lk._fact("number", "ordered", cites, ("bc",), value=_plain(ordered[item]),
                     label=["ordered", "order", "ordering"], tolerance="0"),
            lk._fact("number", "received", cites, ("bc",), value=_plain(got[item]),
                     label=["received", "receiving", "got", "came", "receipt", "receipts"],
                     tolerance="0"),
            lk._fact("claim", way, cites, ("bc",), value=way,
                     accept=(["less than ordered", "under", "shortfall", "not all"]
                             if way == "short" else ["more than ordered", "overage"]),
                     contradicts=["received in full", "matches the order", "all received"])],
            ["BC purchaseReceipts.orderNumber -> purchaseReceiptLines.quantity by "
             "lineObjectNumber, against the BC order's purchaseOrderLines.quantity"],
            "the one item whose posted receipt total differs from its ordered quantity"))
    return out


def po_eta_vs_due(world: lk.World) -> list[lk.Candidate]:
    """Our due date (FileMaker datearrive, else Portal expected_date) against the date on a
    released BC order's lines (one date), where they differ."""
    fm = {i["citation"]: i for i in world.rows(FM_PO)}
    out = []
    for group in world.lateness()["groups"]:
        heads = {s: [h["citation"] for h in group["headers"] if h["system"] == s]
                 for s in ("portal", "fm", "bc")}
        due = None
        for cite in heads["fm"]:
            day = instant((fm.get(cite) or {}).get("row", {}).get("datearrive"))
            if day is not None:
                due = (day.date(), cite)
        for cite in heads["portal"]:
            day = instant(((world.record(cite) or {}).get("row") or {}).get("expected_date"))
            if due is None and day is not None:
                due = (day.date(), cite)
        released = [world.record(c) for c in heads["bc"]]
        released = [r for r in released if r and str(r["fields"].get("status")) == "Open"]
        if due is None or len(released) != 1:
            continue
        order_id = str(released[0]["fields"].get("id")).lower()
        lines = [l for l in world.bc_set("purchaseOrderLines")
                 if str(l["fields"].get("documentId")).lower() == order_id]
        days = {instant(l["fields"].get("expectedReceiptDate")) for l in lines} - {None}
        if len(days) != 1:
            continue
        eta = next(iter(days)).date()
        if eta == due[0]:
            continue
        number = lk._asked_number(group)
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            lk._fact("date", "our_due", [due[1]], ("api",), value=due[0].isoformat()),
            lk._fact("date", "bc_eta", [l["citation"] for l in lines], ("bc",),
                     value=eta.isoformat())],
            ["FileMaker generated_purchase_order.datearrive, else app_purchase_order."
             "expected_date; BC purchaseOrderLines.expectedReceiptDate of the released (Open) "
             "BC order (one date on every line)"],
            "our requested date against the released BC order's expected receipt date"))
    return out


# --------------------------------------------------------------------------- Portal

def portal_open_sos(world: lk.World) -> list[lk.Candidate]:
    names = {key_text(i["row"].get("id")): str(i["row"].get("company_name") or "").strip()
             for i in world.rows(CUSTOMER)}
    by_customer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.rows(lk.PORTAL_SO):
        row = item["row"]
        name = names.get(key_text(row.get("customer_id")))
        if row.get("status") in OPEN_PORTAL_SO and name and row.get("order_number"):
            by_customer[name].append(item)
    return [lk.Candidate(name, "Customer", {"customer": name}, [
        lk._fact("set", "open_orders", [i["citation"] for i in rows], ("api",),
                 members=[[str(i["row"]["order_number"])] for i in rows])],
        ["portal mirror app_sales_order (status Open or READY_TO_SHIP) by customer_id, "
         "app_customer.company_name"], "the customer's open Portal sales orders")
        for name, rows in by_customer.items() if 1 <= len(rows) <= lk.LIST_MAX]


def portal_price_trend(world: lk.World) -> list[lk.Candidate]:
    """The direction of a product's Portal PO unit price, last order against the one
    before (dated by the order's date_generated; cancelled orders left out)."""
    orders = {key_text(i["row"].get("id")): i for i in world.rows(PO)
              if str(i["row"].get("status") or "").upper() not in ("CANCEL", "DRAFT")}
    codes = {key_text(i["row"].get("id")): str(i["row"].get("code") or "")
             for i in world.rows(PRODUCT)}
    prices: dict[str, list[tuple[str, Decimal, str]]] = defaultdict(list)
    for line in world.rows(PO_LINE):
        row = line["row"]
        order = orders.get(key_text(row.get("purchase_order_id")))
        price = decimal_of(row.get("unit_price"))
        code = codes.get(key_text(row.get("product_id")))
        if order is None or not code or not price or price <= 0:
            continue
        prices[code].append((str(order["row"].get("date_generated") or "")[:10], price,
                             line["citation"]))
    out = []
    for code, rows in prices.items():
        facts = _trend_facts(code, rows, "api", ["purchasing_prices"])
        if facts:
            out.append(lk.Candidate(code, "Product", {"item": code}, facts,
                                    ["portal mirror app_purchase_order_line.unit_price by "
                                     "product (app_product.code), dated by app_purchase_"
                                     "order.date_generated"],
                                    "the last Portal PO price against the one before"))
    return out


def quotes_without_order(world: lk.World) -> list[lk.Candidate]:
    """Accepted quotes with no order: none is the key when no quote is accepted at all."""
    accepted = [i for i in world.rows(QUOTE) if i["row"].get("status") in ACCEPTED_QUOTE]
    accepted += [r for r in world.bc_set("salesQuotes")
                 if r["fields"].get("status") in ACCEPTED_QUOTE]
    if accepted:
        return []   # a list needs the quote-to-order link: not keyed here
    return [lk.Candidate("live-ops:quotes-no-order", "Quote", {}, [
        lk._fact("absent", "no_accepted_quote", [], (), value="accepted",
                 accept=["no accepted quote", "no quotes accepted", "none accepted",
                         "no quote has been accepted", "none are accepted", "all draft",
                         "still draft", "only draft"],
                 contradicts=["quote was accepted", "was accepted", "were accepted"])],
        ["portal mirror app_quote.status", "BC salesQuotes.status"],
        "no Portal or BC quote is accepted, so none waits for an order")]


# --------------------------------------------------------------------------- Sage

def sage_balance(world: lk.World) -> list[lk.Candidate]:
    out = []
    for item in world.rows(SAGE_CUSTOMER):
        row = item["row"]
        name = str(row.get("namecust") or "").strip()
        due = decimal_of(row.get("amtbaldueh"))
        if not name or due is None or due <= 0 or decimal_of(row.get("amtbalduet")) != due:
            continue   # Sage's home and customer-currency balances disagree: no one balance
        out.append(lk.Candidate(name, "Customer", {"customer": name}, [
            lk._fact("number", "sage_balance", [item["citation"]], ("api",), value=_plain(due),
                     label=lk.LABELS["open_ar"], tolerance="1", tolerance_pct=lk.MONEY_PCT,
                     markings=["finance"])],
            ["portal mirror sage.ar_customer.amtbaldueh (home currency), equal to "
             "amtbalduet (customer currency)"],
            "the customer's balance due in Sage"))
    return out


def sage_order_shipped(world: lk.World) -> list[lk.Candidate]:
    """When a completed Sage order shipped: the dates of its shipments
    (``oe_shipment_header.shidate``). The order header's ``shipdate`` is not a ship date:
    on the live mirror it equals the order's invoice and completion date, days after the
    shipment it names (read-only check, 2026-11-02: 5 of 6 completed orders differ)."""
    shipments: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for item in world.rows(SAGE_SHIPMENT):
        row = item["row"]
        shipped = instant(str(row.get("shidate") or "").strip())
        if shipped is not None:
            shipments[str(row.get("ordnumber") or "").strip()].append(
                (shipped.date().isoformat(), item["citation"]))
    out = []
    for item in world.rows(SAGE_ORDER):
        row = item["row"]
        number = str(row.get("ordnumber") or "").strip()
        found = shipments.get(number)
        if not number.isdigit() or not found or str(row.get("complete")).strip() != "5":
            continue
        days = sorted({day for day, _ in found})
        records = [item["citation"], *(citation for _, citation in found)]
        fact = (lk._fact("date", "shipped", records, ("api",), value=days[0], label="ship")
                if len(days) == 1 else
                lk._fact("set", "shipped", records, ("api",),
                         members=[date_forms(day) for day in days]))
        out.append(lk.Candidate(number, "SalesOrder", {"so": number}, [fact],
            ["portal mirror sage.oe_order_header: ordnumber, complete = 5; "
             "sage.oe_shipment_header: ordnumber, shidate"],
            "the Sage order's shipment dates (a completed order; every date when it "
            "shipped more than once)"))
    return out


# --------------------------------------------------------------------------- mail, Teams,
# SharePoint, Mission Control

def _latest(messages: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    dated = [m for m in messages if m.get("at") and not lk.AUTOMATIC.match(
        str(m.get("subject") or ""))]
    if not dated:
        return None
    last = max(dated, key=lambda m: (m["at"], m["handle"]))
    return None if sum(m["at"] == last["at"] for m in dated) > 1 else last


def customer_last_mail(world: lk.World) -> list[lk.Candidate]:
    """The latest message to or from a customer's own mail domain (BC customers.email)."""
    by_customer: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in world.customer_mail:
        by_customer[str(message["customer"])].append(message)
    out = []
    for customer, messages in by_customer.items():
        last = _latest(messages)
        if last is None or (world.as_of - instant(last["at"]).date()).days > RECENT_DAYS:
            continue
        out.append(lk.Candidate(customer, "Customer", {"customer": customer}, [
            lk._fact("message", "last_message", [last["handle"]], ("graph-mail",),
                     message_id=last["message_id"], quote=str(last.get("subject"))[:300],
                     mail=True, **lk.FORMS["last_vendor_message"])],
            ["mail capture: current messages whose sender or a recipient is at the domain of "
             "the customer's BC customers.email; the latest received_at"],
            "the latest message with the customer (no automatic reply)"))
    return out


def teams_po_discussion(world: lk.World) -> list[lk.Candidate]:
    """The latest Teams message (chat or channel) naming a Portal PO number."""
    numbers = {re.sub(r"\D", "", str(i["row"].get("po_number") or "")):
               str(i["row"].get("po_number")) for i in world.rows(PO)}
    by_po: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in world.teams:
        for digits in lk._ids_named(str(message.get("text") or "")):
            if digits in numbers:
                by_po[numbers[digits]].append(message)
    out = []
    for number, messages in by_po.items():
        last = _latest(messages)
        if last is None:
            continue
        out.append(lk.Candidate(number, "PurchaseOrder", {"po": number}, [
            lk._fact("document", "teams_message", [last["handle"]],
                     (_system_of(last["handle"]),), quote=str(last.get("text"))[:300],
                     mail=True)],
            ["teams capture: current chat and channel messages whose text names the PO "
             "number; the latest created_at"],
            "the latest Teams message naming the PO"))
    return out


def sharepoint_lot_file(world: lk.World) -> list[lk.Candidate]:
    """The one SharePoint file whose name holds a released lot's number (a COA, a
    receiving document)."""
    lots = {lot for lot, _day, _c in lk._released_lots(world)
            if len(lot) >= 6 and not re.fullmatch(r"(?:19|20)\d{6}", lot)}
    by_lot: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in world.sharepoint:
        for token in re.findall(r"[A-Za-z0-9-]{6,}", str(item.get("name") or "")):
            if token in lots:
                by_lot[token].append(item)
    return [lk.Candidate(lot, "Lot", {"lot": lot}, [
        lk._fact("document", "lot_file", [files[0]["handle"]], ("sharepoint",),
                 quote=str(files[0].get("name"))[:300], mail=True)],
        ["sharepoint capture: current items whose file name holds the lot number as a "
         "token; released lots of receipt_raw_material"], "the one file named for the lot")
        for lot, files in by_lot.items() if len(files) == 1]


def mc_task(world: lk.World) -> list[lk.Candidate]:
    out = []
    for task in world.mc:
        row = task["row"]
        task_id, stage = str(row.get("id") or ""), str(row.get("stage") or "")
        owner = str(row.get("accountableOwner") or row.get("assignee") or "")
        if not re.fullmatch(TASK_TOKEN, task_id) or not stage or not owner:
            continue
        out.append(lk.Candidate(task_id, "MCTask", {"task": task_id}, [
            lk._fact("text", "stage", [task["handle"]], ("mc",), value=stage),
            lk._fact("text", "owner", [task["handle"]], ("mc",), value=owner)],
            ["mission control capture live/tasks: stage, accountableOwner (else assignee)"],
            "the task's stage and accountable owner"))
    return out


def _plain(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


FAMILIES: dict[str, Callable[[lk.World], list[lk.Candidate]]] = {
    "fm_po_people": fm_po_people,
    "fm_quarantine": fm_quarantine, "bc_inventory": bc_inventory,
    "bc_receipt_vs_po": bc_receipt_vs_po,
    "bc_price_trend": bc_price_trend, "bc_open_sos": bc_open_sos,
    "po_eta_vs_due": po_eta_vs_due, "portal_open_sos": portal_open_sos,
    "portal_price_trend": portal_price_trend, "quotes_without_order": quotes_without_order,
    "sage_balance": sage_balance, "sage_order_shipped": sage_order_shipped,
    "customer_last_mail": customer_last_mail, "teams_po_discussion": teams_po_discussion,
    "sharepoint_lot_file": sharepoint_lot_file, "mc_task": mc_task,
}
# The system each family's key is read from (report counts).
SYSTEMS = {"fm_po_people": "FileMaker",
           "fm_quarantine": "FileMaker", "bc_inventory": "BC", "bc_price_trend": "BC",
           "bc_receipt_vs_po": "BC",
           "bc_open_sos": "BC", "po_eta_vs_due": "BC+Portal", "portal_open_sos": "Portal",
           "portal_price_trend": "Portal", "quotes_without_order": "Portal+BC",
           "sage_balance": "Sage", "sage_order_shipped": "Sage", "customer_last_mail": "mail",
           "teams_po_discussion": "Teams", "sharepoint_lot_file": "SharePoint",
           "mc_task": "MC"}
PORTAL_RELATIONS = (CUSTOMER, PRODUCT, QUOTE, FM_PO_ITEM, FM_RECEIPT_PACK, SAGE_CUSTOMER,
                    SAGE_ORDER, SAGE_SHIPMENT)
BC_SETS = ("purchaseReceiptLines", "salesQuotes")
# Only these columns of these wide relations are read (``scripts.eval_live_ops.NARROW``).
NARROW = {CUSTOMER: ("id", "company_name"), PRODUCT: ("id", "code", "name"),
          SAGE_CUSTOMER: ("idcust", "namecust", "amtbaldueh", "amtbalduet"),
          SAGE_ORDER: ("orduniq", "ordnumber", "customer", "complete"),
          SAGE_SHIPMENT: ("shiuniq", "ordnumber", "shidate")}

# Imported last: ``evals.live_ops_keys`` merges ``FAMILIES`` from here when it loads, so
# either module may be imported first (the families use it only when called).
from evals import live_ops_keys as lk  # noqa: E402
