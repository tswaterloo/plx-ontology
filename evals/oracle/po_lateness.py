"""Raw-capture oracle for purchase-order lateness: the open PO lines past their governing date.

It reads the captured Portal mirror rows and Business Central records directly (never the
product's projection, links or tools) and applies the lateness model of
``ontology/schema/process/po_delivery_window.json`` by its own code:

* open: a Portal status ``SUBMITTED`` or ``PARTIAL``, or a BC purchase order with
  ``fullyReceived`` false; closed by a Portal status ``DRAFT``, ``RECEIVED`` or a cancel
  value, a BC ``fullyReceived`` true or a BC status ``Draft``. An order is open when one
  of its records says open and none says closed (FileMaker and Sage decide nothing);
* an open line: ordered quantity above zero and received (none read as 0) below it;
* the governing date, by the model's authority precedence: FileMaker ``datearrive``
  first, then Portal ``expected_date``, then BC (the line's ``expectedReceiptDate``, else
  the header's ``requestedReceiptDate``; a Portal line takes the BC date only when the BC
  order holds exactly one). A year-1 BC date is no date;
* late: an open line whose governing date is before ``as_of``. An open order holding no
  line is judged on its header date (``line`` false).

Records join one order only on stored identifiers, never on a number text match (a lead):
Portal ``custom_fields.fmGeneratedPoId`` = FileMaker ``id``, and Portal
``custom_fields.renumberedTo`` = the BC ``number`` when exactly one BC order holds it and
exactly one Portal order claims it (else ``ambiguous``, no join). Sage is not read.

Input: ``portal`` maps a relation name (``evals.oracle.keys``) to rows
``{"row", "citation"}``; ``bc`` is a list of ``{"company_id", "entity_set", "fields",
"citation"}``. Output: every order group (its numbers and records) and the late ones with
their late lines, governing dates (with the record each date is read from) and receipts.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Mapping, Sequence

from .keys import FM_PO, PO, PO_LINE, RECEIPT
from .source import decimal_of, field_value, instant, key_text

PORTAL_OPEN = frozenset({"SUBMITTED", "PARTIAL"})
PORTAL_CLOSED = frozenset({"DRAFT", "RECEIVED", "CANCELLED", "CANCELED", "CANCEL"})
BC_ORDERS, BC_LINES, BC_RECEIPTS = "purchaseOrders", "purchaseOrderLines", "purchaseReceipts"
ZERO = decimal_of("0")


def _day(value: Any) -> date | None:
    moment = instant(value)
    return moment.date() if moment is not None else None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper() if isinstance(value, str) else ""
    return {"TRUE": True, "FALSE": False}.get(text)


def _decides(group: Mapping[str, Any]) -> bool:
    says_open = says_closed = False
    for item in group["portal"]:
        status = item["row"].get("status")
        says_open |= status in PORTAL_OPEN
        says_closed |= status in PORTAL_CLOSED
    for item in group["bc"]:
        received = _bool(item["fields"].get("fullyReceived"))
        says_open |= received is False
        says_closed |= received is True
        says_closed |= str(item["fields"].get("status") or "").strip().upper() == "DRAFT"
    return says_open and not says_closed


def _open_quantity(ordered: Any, received: Any) -> bool:
    want = decimal_of(ordered)
    got = decimal_of(received) if received is not None else ZERO
    return want is not None and got is not None and want > ZERO and got < want


def late_orders(portal: Mapping[str, Sequence[Mapping[str, Any]]],
                bc: Sequence[Mapping[str, Any]], as_of: date) -> dict[str, Any]:
    """The order groups and the late ones (module doc)."""
    pos = list(portal.get(PO) or ())
    fm_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in portal.get(FM_PO) or ():
        key = key_text(item["row"].get("id"))
        if key is not None:
            fm_by_id[key].append(item)
    bc_orders = [r for r in bc if r["entity_set"] == BC_ORDERS]
    bc_by_number: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in bc_orders:
        number = key_text(record["fields"].get("number"))
        if number is not None:
            bc_by_number[number].append(record)
    fm_claims: dict[str, int] = defaultdict(int)
    bc_claims: dict[str, int] = defaultdict(int)
    for item in pos:
        for key, claims in ((key_text(field_value(item["row"], "custom_fields.fmGeneratedPoId")),
                             fm_claims),
                            (key_text(field_value(item["row"], "custom_fields.renumberedTo")),
                             bc_claims)):
            if key is not None:
                claims[key] += 1
    ambiguous = 0
    groups: list[dict[str, Any]] = []
    joined_bc: set[int] = set()
    for item in pos:
        group: dict[str, Any] = {"portal": [item], "fm": [], "bc": []}
        fm_key = key_text(field_value(item["row"], "custom_fields.fmGeneratedPoId"))
        if fm_key is not None and fm_key in fm_by_id:
            if fm_claims[fm_key] == 1 and len(fm_by_id[fm_key]) == 1:
                group["fm"] = list(fm_by_id[fm_key])
            else:
                ambiguous += 1
        bc_key = key_text(field_value(item["row"], "custom_fields.renumberedTo"))
        if bc_key is not None and bc_by_number.get(bc_key):
            if bc_claims[bc_key] == 1 and len(bc_by_number[bc_key]) == 1:
                group["bc"] = list(bc_by_number[bc_key])
                joined_bc.add(id(bc_by_number[bc_key][0]))
            else:
                ambiguous += 1
        groups.append(group)
    groups += [{"portal": [], "fm": [], "bc": [record]} for record in bc_orders
               if id(record) not in joined_bc]

    portal_lines: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in portal.get(PO_LINE) or ():
        portal_lines[key_text(item["row"].get("purchase_order_id")) or ""].append(item)
    portal_receipts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in portal.get(RECEIPT) or ():
        portal_receipts[key_text(item["row"].get("purchase_order_id")) or ""].append(item)
    bc_lines: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    bc_receipts: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in bc:
        company = str(record.get("company_id") or "").lower()
        if record["entity_set"] == BC_LINES:
            bc_lines[(company, str(record["fields"].get("documentId") or "").lower())
                     ].append(record)
        elif record["entity_set"] == BC_RECEIPTS:
            bc_receipts[(company, str(record["fields"].get("orderNumber") or ""))
                        ].append(record)

    out_groups, late, opened, undated = [], [], 0, 0
    for group in groups:
        numbers = sorted({n for n in (
            [key_text(i["row"].get("po_number")) for i in group["portal"]]
            + [key_text(i["row"].get("ponumber")) for i in group["fm"]]
            + [key_text(r["fields"].get("number")) for r in group["bc"]]) if n})
        headers = ([{"system": "portal", "citation": i["citation"]} for i in group["portal"]]
                   + [{"system": "fm", "citation": i["citation"]} for i in group["fm"]]
                   + [{"system": "bc", "citation": r["citation"]} for r in group["bc"]])
        lines = ([("portal", i["citation"], i["row"].get("quantity"),
                   i["row"].get("quantity_received"), None)
                  for p in group["portal"]
                  for i in portal_lines.get(key_text(p["row"].get("id")) or "", ())]
                 + [("bc", r["citation"], r["fields"].get("quantity"),
                     r["fields"].get("receivedQuantity"),
                     _day(r["fields"].get("expectedReceiptDate")))
                    for h in group["bc"]
                    for r in bc_lines.get((str(h.get("company_id") or "").lower(),
                                           str(h["fields"].get("id") or "").lower()), ())])
        receipts = ([i["citation"] for p in group["portal"]
                     for i in portal_receipts.get(key_text(p["row"].get("id")) or "", ())]
                    + [r["citation"] for h in group["bc"]
                       for r in bc_receipts.get((str(h.get("company_id") or "").lower(),
                                                 str(h["fields"].get("number") or "")), ())])
        record = {"numbers": numbers, "headers": headers, "bc": bool(group["bc"]),
                  "records": sorted({h["citation"] for h in headers}
                                    | {line[1] for line in lines} | set(receipts))}
        out_groups.append(record)
        if not _decides(group):
            continue
        opened += 1
        # The governing header date by precedence, with the record it is read from.
        header_date: tuple[date, str] | None = None
        for item, field in ([(i, "datearrive") for i in group["fm"]]
                            + [(i, "expected_date") for i in group["portal"]]):
            day = _day(item["row"].get(field))
            if day is not None:
                header_date = (day, item["citation"])
                break
        bc_header = next(((d, r["citation"]) for r in group["bc"]
                          for d in [_day(r["fields"].get("requestedReceiptDate"))]
                          if d is not None), None)
        bc_dates = {(line[4], line[1]) for line in lines if line[0] == "bc" and line[4]}
        if not bc_dates and bc_header is not None:
            bc_dates = {bc_header}
        single_bc = next(iter(bc_dates)) if len({d for d, _ in bc_dates}) == 1 else None
        open_lines = [line for line in lines if _open_quantity(line[2], line[3])]
        units = []
        for origin, citation, _q, _r, own in open_lines:
            due = header_date or ((own, citation) if own is not None else
                                  bc_header if origin == "bc" else single_bc)
            units.append((origin, citation, due))
        if not lines:   # an open order holding no line: judged on its header date
            units = [(h["system"], h["citation"], header_date or bc_header) for h in headers]
        if not any(due for *_x, due in units):
            undated += 1
        late_units = [{"system": origin, "citation": citation, "due": due[0].isoformat(),
                       "date_source": due[1], "line": bool(lines)}
                      for origin, citation, due in units if due and due[0] < as_of]
        if late_units:
            late.append({**record, "late_lines": late_units, "receipts": sorted(receipts),
                         "governing_dates": sorted({u["due"] for u in late_units})})
    return {"groups": out_groups, "late": late,
            "counts": {"groups": len(out_groups), "open": opened, "late": len(late),
                       "undated": undated, "ambiguous": ambiguous}}
