"""Case view of a Petra PO in Business Central: order, receipts, invoices, gaps.

Input is the cited evidence of ``integrations.bc_purchase.read_purchase_evidence``
(no I/O here). Output (``plx-bc-purchase-case/v1``) is what a PO case shows:

* every BC purchase order candidate (one per company that holds the No.; never
  picked between), with per line the **ordered**, **received** and **invoiced**
  quantities as distinct roles (``operating.quantity_types``: ``ordered`` and
  ``received`` are different quantities; no outstanding or difference figure is
  computed, only a comparison status), each value with its ``bc://`` line
  citation and the BC field it came from;
* received two ways, never merged: BC's own ``receivedQuantity`` on the order line
  and the sum of posted receipt lines (``purchaseReceiptLines``) for the same item
  No. and unit of measure. A disagreement is a conflict. The Portal's own shadow
  view (``purchase-receipt-reconcile.ts@1f2d42d:1-21,118-149``, TASK-384-line,
  3f76aae) matches per PO line by item No. across units and never writes BC values
  back; neither side records a PO-line reference, so with several PO lines for one
  item it allocates receipt lines in document/line order to the first line with
  enough ordered quantity left (``allocated``). That is an order rule, not a recorded
  link, so this view keeps each unit apart and leaves such an item a gap
  (``item_on_several_order_lines``) instead of allocating;
* unit cost with its field and currency: order ``directUnitCost``, invoice line
  ``unitCost`` (Portal ``serializers.ts@4243499:1473,1610``; 7cecf4c), receipt
  line ``unitCost``; invoice G/L lines (freight 41130 / duty 41131, ERP-D-015)
  are charges, never item cost. A misc AP bill the Portal posts (TASK-805,
  ``ap-misc.ts@4243499``) carries G/L Account lines only, so it never adds an
  invoiced item quantity;
* gaps, conflicts and the Portal/FM link.

Rules that keep it honest:

* **Missing receipts are a gap, not zero.** No posted receipt naming the order
  says only that BC holds none: BC receipts are go-forward only (ERP-D-009, PO10/
  DPO10 series through the receive-only bridge); FM/Portal receipts are not
  replayed. A received figure comes only from a field BC sent.
* An incomplete read (not permitted, unavailable, truncated, withheld) leaves the
  dependent figure unknown and is reported; a partial sum is never shown as the
  total.
* Invoices count as invoiced only when posted (status Open/Paid/Closed), as Portal
  ``classifyInvoice`` (``document-finance.ts@1f2d42d:60-116``): canceled invoices are
  dropped (listed), drafts and "In Review" are shown and cited but never summed, a
  blank or unknown status leaves invoiced-from-invoices unknown (a gap). An invoice
  found only by vendor invoice No. is attributed only when its vendor equals the
  order's and its orderNumber is blank or this order; one found by ``orderId`` (the
  order's own id) is attributed as one found by ``orderNumber``.
* A blank currency code is unknown (it may mean the company's local currency);
  it is never assumed.
* Identity: a BC number match is a reference. Only a Portal ``app_bc_entity_map``
  row (entity ``purchaseOrder``, ``LINKED``, naming this BC document's id) makes
  the Portal PO and the BC order one identity (Portal
  ``document-finance-queries.ts@4243499:211 resolveBcPoNumbers``). The FM PO No.
  is often in BC 'Your Reference', which API v2.0 does not expose (ODataV4 Page 50
  does; only the BC capture reads it, ``integrations.bc_read``). BC External
  Document No. is never read as a customer PO (REC-10).

``integrations.bc_purchase`` supplies the GET-only reads (its ``probe`` is the live
check). On the case host BC purchase records are read through ``plx.find`` and
``plx.object`` over the projection (the kind-specific tool retired,
``operating/ontology_tools.HOST_RETIREMENTS``).
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

SCHEMA = "plx-bc-purchase-case/v1"
ADAPTER_STATUS = "live-unverified"
EVIDENCE_LABELS = frozenset({"isolated-fixture", "injected-transport", "live-read"})
_INCOMPLETE = frozenset({"truncated", "partially_withheld", "not_permitted", "unavailable",
                         "not_searchable"})


def _dec(quantity: Mapping[str, Any] | None) -> Decimal | None:
    if not quantity or quantity.get("status") != "read":
        return None
    try:
        value = Decimal(str(quantity["value"]))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value == value.to_integral_value() \
        else format(value, "f")


def _key(line: Mapping[str, Any]) -> tuple[str, str] | None:
    item = line.get("item_number")
    if not isinstance(item, str) or not item.strip():
        return None
    uom = line.get("unit_of_measure_code")
    return item.strip().casefold(), (uom.strip().casefold() if isinstance(uom, str) else "")


def _is_item(line: Mapping[str, Any]) -> bool:
    return line.get("line_type") in ("Item", None) and _key(line) is not None


def _quantity(line: Mapping[str, Any], role: str) -> dict[str, Any]:
    for quantity in line["quantities"]:
        if quantity["role"] == role:
            return {**quantity, "citation": line["citation"]}
    return {"role": role, "field": None, "value": None, "status": "absent",
            "citation": line["citation"]}


def _compare(ordered: Decimal | None, received: Decimal | None) -> str:
    """Comparison only (no difference figure: ordered and received are distinct roles)."""
    if ordered is None or received is None:
        return "unknown"
    if received == 0:
        return "none_received"
    if received < ordered:
        return "partially_received"
    if received == ordered:
        return "received_in_full"
    return "over_received"


def invoice_class(document: Mapping[str, Any]) -> str:
    """BC purchase invoice status as Portal ``classifyInvoice``
    (``business-central/document-finance.ts@1f2d42d:60-71``): canceled, paid, draft,
    open; stricter in two ways: "In Review" (an unposted invoice in approval) is a
    draft, and a blank or unrecognised status is ``unknown`` (never counted), where
    Portal defaults to open."""
    status = str(document["header"].get("status") or "").strip().lower()
    if "cancel" in status:
        return "canceled"
    if status in ("paid", "closed"):
        return "paid"
    if status in ("draft", "in review", "inreview", "in_review"):
        return "draft"
    if status == "open":
        return "open"
    return "unknown"


def _issue(kind: str, code: str, detail: str, citations: Sequence[str] = (),
           company_id: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "code": code, "detail": detail,
            "citations": sorted(set(citations)), "company_id": company_id}


def _search(company: Mapping[str, Any], entity_set: str, field: str) -> list[Mapping[str, Any]]:
    return [r for r in company["reads"] if r["entity_set"] == entity_set and r["field"] == field]


def _documents_complete(searches: Sequence[Mapping[str, Any]],
                        documents: Sequence[Mapping[str, Any]]) -> bool:
    return (bool(searches) and all(s.get("status") not in _INCOMPLETE for s in searches)
            and all(d["lines"] and d["lines"]["complete"] for d in documents))


def _aggregate(documents: Sequence[Mapping[str, Any]], role: str,
               keys: Mapping[tuple[str, str], list]) -> tuple[dict, list]:
    """Sum of ``role`` per (item No., unit) over the item lines of ``documents``.
    A key whose any line value is unreadable is marked unknown (never partially summed)."""
    sums: dict[tuple[str, str], dict[str, Any]] = {}
    unmatched: list[Mapping[str, Any]] = []
    for document in documents:
        for line in (document["lines"] or {}).get("items", []):
            if not _is_item(line):
                continue
            key = _key(line)
            if key not in keys:
                unmatched.append(line)
                continue
            entry = sums.setdefault(key, {"value": Decimal(0), "known": True, "citations": [],
                                          "lines": []})
            value = _dec(_quantity(line, role))
            entry["citations"].append(line["citation"])
            entry["lines"].append({"citation": line["citation"],
                                   "document_citation": document["citation"],
                                   "value": _quantity(line, role)["value"],
                                   "unit_cost": line["unit_cost"]["value"],
                                   "currency_code": document["currency_code"]})
            if value is None:
                entry["known"] = False
            else:
                entry["value"] += value
    return sums, unmatched


def _documents_role(sums: Mapping, key: tuple[str, str] | None, shared: bool,
                    complete: bool, found_any: bool, role: str,
                    blocked: str | None = None) -> dict[str, Any]:
    base = {"role": role, "quantity_type": "received" if role == "received" else None}
    if key is None:
        return {**base, "value": None, "status": "not_an_item_line", "citations": []}
    entry = sums.get(key)
    if not complete:
        return {**base, "value": None, "status": "incomplete_read",
                "citations": entry["citations"] if entry else []}
    if blocked:
        return {**base, "value": None, "status": blocked,
                "citations": entry["citations"] if entry else []}
    if not found_any:
        return {**base, "value": None, "status": "no_document_found", "citations": []}
    if shared:
        return {**base, "value": None, "status": "item_on_several_order_lines",
                "citations": entry["citations"] if entry else []}
    if entry is None:
        return {**base, "value": None, "status": "no_line_for_item", "citations": []}
    if not entry["known"]:
        return {**base, "value": None, "status": "line_value_unreadable",
                "citations": entry["citations"]}
    return {**base, "value": _text(entry["value"]), "status": "read",
            "citations": entry["citations"], "lines": entry["lines"]}


def portal_po_link(order: Mapping[str, Any], portal_po: Mapping[str, Any] | None,
                   searched: str) -> dict[str, Any]:
    """How a BC order relates to the case's Portal PO row (``app_purchase_order``).

    Identity only from a Portal ``app_bc_entity_map`` row naming this BC document;
    everything else is a reference with its basis."""
    if not portal_po:
        return {"link": "reference", "identity_asserted": False,
                "basis": ["bc_number_equals_searched_number"],
                "caveats": ["No Portal PO row was supplied; the BC order matched the "
                            "searched number only."]}
    custom = portal_po.get("custom_fields")
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except ValueError:
            custom = None
    custom = custom if isinstance(custom, Mapping) else {}
    number = str(order.get("number") or "")
    basis: list[str] = []
    caveats: list[str] = []
    entity_map = portal_po.get("bc_entity_map")
    if isinstance(entity_map, Mapping):
        same_row = (entity_map.get("entity_type") == "purchaseOrder"
                    and entity_map.get("source_system") == "portal"
                    and str(entity_map.get("source_id")) == str(portal_po.get("id")))
        names_this = str(entity_map.get("bc_system_id") or "").lower() == order["id"].lower()
        if same_row and entity_map.get("status") == "LINKED" and names_this:
            return {"link": "identity", "identity_asserted": True,
                    "basis": ["portal_app_bc_entity_map"], "caveats": []}
        if same_row and entity_map.get("status") == "LINKED":
            return {"link": "conflict", "identity_asserted": False,
                    "basis": ["portal_app_bc_entity_map_names_other_document"],
                    "caveats": ["Portal's entity map links this Portal PO to another BC "
                                "document; the number match is not this PO."]}
    po_number = str(portal_po.get("po_number") or "").strip()
    if po_number and po_number.casefold() == number.casefold():
        basis.append("portal_po_number_equals_bc_number")
        caveats.append("Portal rewrites po_number to the BC No. once linked; equal numbers "
                       "are a reference (the BC company is not recorded on the Portal PO).")
    if str(custom.get("renumberedTo") or "").strip().casefold() == number.casefold() \
            and custom.get("renumberSource") == "bc-drain-align" and number:
        basis.append("portal_recorded_renumber_to_bc_number")
    fm_number = str(custom.get("fmPoNumber") or "").strip()
    if fm_number and fm_number.casefold() == searched.casefold() \
            and fm_number.casefold() != number.casefold():
        basis.append("searched_fm_po_number")
        caveats.append("The FM PO No. is often in BC 'Your Reference', not exposed by API v2.0; "
                       "the BC order matched by another field.")
    if po_number.upper().rsplit("-FM", 1)[0] != po_number.upper():
        caveats.append("Portal po_number carries an -FM<id> clash suffix.")
    return {"link": "reference" if basis else "none", "identity_asserted": False,
            "basis": basis or ["no_link_evidence"], "caveats": caveats}


def _order_view(company: Mapping[str, Any], order: Mapping[str, Any], *,
                portal_po: Mapping[str, Any] | None, searched: str,
                issues: list[dict[str, Any]]) -> dict[str, Any]:
    cid = company["company_id"]
    lines_block = order["lines"] or {"items": [], "complete": False, "status": "unavailable"}
    po_lines = lines_block["items"]
    keys: dict[tuple[str, str], list] = {}
    for line in po_lines:
        if _is_item(line):
            keys.setdefault(_key(line), []).append(line)
    receipt_searches = _search(company, "purchaseReceipts", "orderNumber")
    receipts = company["receipts"]
    receipts_complete = _documents_complete(receipt_searches, receipts)
    vendor = order["header"].get("vendorNumber")
    invoices: list[Mapping[str, Any]] = []
    number = str(order.get("number") or "").strip().casefold()
    for invoice in company["invoices"]:
        other = invoice["header"].get("vendorNumber")
        named = str(invoice["header"].get("orderNumber") or "").strip()
        by_order = any(m["field"] in ("orderNumber", "orderId") for m in invoice["matched_by"])
        cites = [invoice["citation"], order["citation"]]
        if by_order:
            invoices.append(invoice)
        elif named and named.casefold() != number:
            issues.append(_issue("conflict", "BC_INVOICE_NAMES_OTHER_ORDER",
                                 "An invoice found by vendor invoice No. names another BC order "
                                 "in its orderNumber; it is not reconciled against this order.",
                                 cites, cid))
        elif not (isinstance(vendor, str) and vendor.strip()
                  and isinstance(other, str) and other.strip()):
            issues.append(_issue("gap", "BC_INVOICE_VENDOR_UNVERIFIABLE",
                                 "An invoice found by vendor invoice No. cannot be checked "
                                 "against the order's vendor (a vendor No. is blank); it is not "
                                 "attributed to this order.", cites, cid))
        elif other.strip() != vendor.strip():
            issues.append(_issue("conflict", "BC_INVOICE_VENDOR_DIFFERS",
                                 "An invoice found by vendor invoice No. names another vendor "
                                 "than the order; it is not reconciled against this order.",
                                 cites, cid))
        else:
            invoices.append(invoice)
    invoice_searches = [*_search(company, "purchaseInvoices", "orderNumber"),
                        *_search(company, "purchaseInvoices", "orderId"),
                        *_search(company, "purchaseInvoices", "vendorInvoiceNumber")]
    by_class: dict[str, list[Mapping[str, Any]]] = {}
    for invoice in invoices:
        by_class.setdefault(invoice_class(invoice), []).append(invoice)
    attributed = invoices
    invoices = [*by_class.get("open", []), *by_class.get("paid", [])]  # posted: counted
    drafts, unknown = by_class.get("draft", []), by_class.get("unknown", [])
    invoices_complete = _documents_complete(invoice_searches, invoices)
    for invoice in drafts:
        issues.append(_issue("gap", "BC_INVOICE_DRAFT_NOT_COUNTED",
                             "A draft (unposted) purchase invoice names this order; it is shown "
                             "but not counted as invoiced (Portal classifyInvoice).",
                             [invoice["citation"]], cid))
    for invoice in unknown:
        issues.append(_issue("gap", "BC_INVOICE_STATUS_UNKNOWN",
                             "A purchase invoice has a blank or unrecognised status; whether it "
                             "is posted is unknown, so invoiced figures from invoices are "
                             "unknown.", [invoice["citation"]], cid))
    received_sums, receipt_unmatched = _aggregate(receipts, "received", keys)
    invoiced_sums, invoice_unmatched = _aggregate(invoices, "invoiced", keys)
    for line in receipt_unmatched:
        issues.append(_issue("conflict", "BC_RECEIPT_LINE_NOT_ON_ORDER",
                             "A posted receipt line names an item No./unit that no line of the "
                             "order has.", [line["citation"], order["citation"]], cid))
    for line in invoice_unmatched:
        issues.append(_issue("conflict", "BC_INVOICE_LINE_NOT_ON_ORDER",
                             "An invoice item line names an item No./unit that no line of the "
                             "order has.", [line["citation"], order["citation"]], cid))
    if not lines_block["complete"]:
        issues.append(_issue("gap", "BC_ORDER_LINES_INCOMPLETE",
                             f"The order's lines were not read in full ({lines_block['status']}); "
                             "line figures may be missing.", [order["citation"]], cid))
    order_currency = order["currency_code"]
    if order_currency is None:
        issues.append(_issue("gap", "BC_CURRENCY_BLANK",
                             "The order carries no currency code; a blank may mean the "
                             "company's local currency and is not assumed.",
                             [order["citation"]], cid))
    for document in [*receipts, *invoices, *drafts, *unknown]:
        code = document["currency_code"]
        if code is None:
            issues.append(_issue("gap", "BC_CURRENCY_BLANK",
                                 f"A {document['kind'].replace('_', ' ')} carries no currency "
                                 "code; it is not assumed.", [document["citation"]], cid))
        elif order_currency and code.casefold() != order_currency.casefold():
            issues.append(_issue("conflict", "BC_CURRENCY_DIFFERS",
                                 f"The {document['kind'].replace('_', ' ')} currency differs "
                                 "from the order's.",
                                 [document["citation"], order["citation"]], cid))
    if receipts_complete and not receipts:
        issues.append(_issue("gap", "BC_RECEIPTS_NOT_FOUND",
                             "BC holds no posted receipt naming this order. This is a gap, not "
                             "zero received: BC receipts are go-forward only (ERP-D-009, PO10/"
                             "DPO10 series via the receive-only bridge), and FM/Portal receipts "
                             "are not replayed.", [order["citation"]], cid))
    if not receipts_complete:
        issues.append(_issue("gap", "BC_RECEIPTS_READ_INCOMPLETE",
                             "Receipts for this order were not read in full; the received sum "
                             "from receipts is unknown.", [order["citation"]], cid))
    if invoices_complete and not invoices and not unknown and not drafts:
        issues.append(_issue("gap", "BC_INVOICES_NOT_FOUND",
                             "No purchase invoice names this order (or a supplied vendor "
                             "invoice No.). Invoices on this tenant often lack orderNumber; "
                             "supply the vendor invoice Nos. from Portal receipts.",
                             [order["citation"]], cid))
    if not invoices_complete:
        issues.append(_issue("gap", "BC_INVOICES_READ_INCOMPLETE",
                             "Invoices for this order were not read in full; invoiced sums "
                             "from invoices are unknown.", [order["citation"]], cid))
    lines_out, charges = [], []
    for line in po_lines:
        key = _key(line)
        item = _is_item(line)
        ordered = _quantity(line, "ordered")
        on_line = _quantity(line, "received")
        invoiced_on_line = _quantity(line, "invoiced")
        shared = item and len(keys.get(key, [])) > 1
        from_receipts = _documents_role(received_sums, key if item else None, shared,
                                        receipts_complete, bool(receipts), "received")
        from_invoices = _documents_role(invoiced_sums, key if item else None, shared,
                                        invoices_complete, bool(invoices), "invoiced",
                                        "invoice_status_unknown" if unknown else None)
        status_line = _compare(_dec(ordered), _dec(on_line))
        status_receipts = _compare(_dec(ordered),
                                   _dec(from_receipts) if from_receipts["status"] == "read"
                                   else None)
        if from_receipts["status"] == "read" and _dec(on_line) is not None \
                and _dec(on_line) != _dec(from_receipts):
            issues.append(_issue("conflict", "BC_RECEIVED_DIFFERS_FROM_RECEIPTS",
                                 "The order line's receivedQuantity and the sum of its posted "
                                 "receipt lines differ; both are shown, neither is chosen.",
                                 [line["citation"], *from_receipts["citations"]], cid))
        if from_receipts["status"] == "no_document_found" and (_dec(on_line) or 0) > 0:
            issues.append(_issue("conflict", "BC_RECEIVED_WITHOUT_RECEIPT_DOCUMENT",
                                 "The order line reports a received quantity but no posted "
                                 "receipt naming the order was found.", [line["citation"]], cid))
        if shared:
            issues.append(_issue("gap", "BC_ITEM_ON_SEVERAL_ORDER_LINES",
                                 "Several order lines carry this item No. and unit; receipt and "
                                 "invoice lines cannot be attributed to one of them.",
                                 [line["citation"]], cid))
        cost = {"order": {**line["unit_cost"], "citation": line["citation"],
                          "currency_code": order_currency,
                          "unit_of_measure_code": line["unit_of_measure_code"]},
                "invoices": from_invoices.get("lines", []),
                "receipts": from_receipts.get("lines", [])}
        order_cost = _dec(line["unit_cost"])
        for invoiced in cost["invoices"]:
            invoice_cost = _dec({"status": "read" if invoiced["unit_cost"] is not None
                                 else "absent", "value": invoiced["unit_cost"]})
            same_currency = (invoiced["currency_code"] or "").casefold() == \
                (order_currency or "").casefold() and order_currency is not None
            if order_cost is not None and invoice_cost is not None and same_currency \
                    and invoice_cost != order_cost:
                issues.append(_issue("conflict", "BC_INVOICE_UNIT_COST_DIFFERS",
                                     "The invoice line unitCost differs from the order line "
                                     "directUnitCost (same item, unit and currency).",
                                     [line["citation"], invoiced["citation"]], cid))
        lines_out.append({
            "citation": line["citation"], "sequence": line["sequence"],
            "line_type": line["line_type"], "item_number": line["item_number"],
            "description": line["description"],
            "unit_of_measure_code": line["unit_of_measure_code"],
            "expected_receipt_date": line["expected_receipt_date"],
            "ordered": ordered, "received_per_order_line": on_line,
            "invoiced_per_order_line": invoiced_on_line,
            "received_per_receipts": {k: v for k, v in from_receipts.items() if k != "lines"},
            "invoiced_per_invoices": {k: v for k, v in from_invoices.items() if k != "lines"},
            "receipt_status": {"per_order_line": status_line, "per_receipts": status_receipts,
                               "comparison_only": True},
            "unit_cost": cost})
    for invoice in invoices:
        for line in (invoice["lines"] or {}).get("items", []):
            if line["line_type"] not in ("Item", None, "Comment"):
                charges.append({"citation": line["citation"],
                                "invoice_citation": invoice["citation"],
                                "line_type": line["line_type"],
                                "account_or_charge_number": line["item_number"],
                                "description": line["description"],
                                "amount_excluding_tax": line["amount_excluding_tax"]["value"],
                                "currency_code": invoice["currency_code"],
                                "note": "Non-item invoice line (e.g. freight 41130 / duty 41131 "
                                        "expensed to G/L, ERP-D-015): not item unit cost."})
    header = order["header"]
    return {"citation": order["citation"], "company_id": cid, "id": order["id"],
            "number": order["number"], "status": header.get("status"),
            "vendor": {"number": header.get("vendorNumber"), "name": header.get("vendorName")},
            "currency_code": order_currency, "dates": order["dates"],
            "totals": {k: header.get(k) for k in ("totalAmountExcludingTax", "totalTaxAmount",
                                                   "totalAmountIncludingTax")
                       if k in header},
            "prices_include_tax": header.get("pricesIncludeTax"),
            "matched_by": order["matched_by"],
            "portal_link": portal_po_link(order, portal_po, searched),
            "lines_status": lines_block["status"], "lines_complete": lines_block["complete"],
            "lines": lines_out, "invoice_charges": charges,
            "receipts": [d["citation"] for d in receipts],
            "invoices": [d["citation"] for d in invoices],
            "draft_invoices": [d["citation"] for d in drafts],
            "status_unknown_invoices": [d["citation"] for d in unknown],
            "canceled_invoices": [d["citation"] for d in by_class.get("canceled", [])],
            "attributed_invoices": [d["citation"] for d in attributed]}


def _summary(document: Mapping[str, Any]) -> dict[str, Any]:
    return {"citation": document["citation"], "kind": document["kind"],
            "company_id": document["company_id"], "number": document["number"],
            "header": document["header"], "dates": document["dates"],
            "currency_code": document["currency_code"], "matched_by": document["matched_by"],
            **({"invoice_class": invoice_class(document)}
               if document["kind"] == "purchase_invoice" else {}),
            "lines_status": (document["lines"] or {}).get("status"),
            "lines_complete": (document["lines"] or {}).get("complete", False),
            "line_count": len((document["lines"] or {}).get("items", []))}


def build_purchase_case(evidence: Mapping[str, Any], *, evidence_label: str,
                        portal_po: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The case view of one PO number's BC evidence (module docstring)."""
    if evidence_label not in EVIDENCE_LABELS:
        raise ValueError("evidence_label must be one of " + ", ".join(sorted(EVIDENCE_LABELS)))
    if not isinstance(evidence, Mapping) or evidence.get("schema") != \
            "plx-bc-purchase-evidence/v1":
        raise ValueError("evidence must be plx-bc-purchase-evidence/v1")
    searched = evidence["po_number"]
    issues: list[dict[str, Any]] = []
    orders, receipts, invoices = [], [], []
    for company in evidence.get("companies", []):
        cid = company["company_id"]
        for read in company["reads"]:
            if read.get("status") in _INCOMPLETE:
                issues.append(_issue("gap", "BC_READ_INCOMPLETE",
                                     f"BC {read['entity_set']} by {read['field']} was "
                                     f"{read['status']}; nothing is concluded from it.",
                                     company_id=cid))
        for order in company["orders"]:
            orders.append(_order_view(company, order, portal_po=portal_po, searched=searched,
                                      issues=issues))
        receipts.extend(_summary(d) for d in company["receipts"])
        invoices.extend(_summary(d) for d in company["invoices"])
        if not company["orders"] and (company["receipts"] or company["invoices"]):
            issues.append(_issue("gap", "BC_ORDER_NOT_FOUND_DOCUMENTS_EXIST",
                                 "Receipts or invoices name this order but no BC purchase order "
                                 "has the No.; BC deletes an order once fully received and "
                                 "invoiced, so the ordered quantity is unknown from BC.",
                                 [d["citation"] for d in (*company["receipts"],
                                                          *company["invoices"])], cid))
    if evidence.get("companies_status") in ("not_permitted", "unavailable"):
        issues.append(_issue("gap", "BC_READ_INCOMPLETE",
                             f"The BC company list was {evidence['companies_status']}; no "
                             "company was searched."))
    complete = bool(evidence.get("complete"))
    if not orders and not receipts and not invoices:
        status = "no_bc_documents" if complete else "not_found_incomplete"
        if complete:
            issues.append(_issue("gap", "BC_ORDER_NOT_FOUND",
                                 "No BC purchase order, posted receipt or invoice names this "
                                 "number in any searched company. Portal pushes a PO only when "
                                 "its vendor and items are linked (not DISTRIBUTE), and BC "
                                 "deletes completed orders: absence is not proof there was no "
                                 "purchase."))
    elif not orders:
        status = "documents_without_order"
    elif len(orders) == 1:
        status = "single_order" if complete else "single_order_incomplete"
    else:
        status = "ambiguous_orders"
    if len(orders) > 1:
        issues.append(_issue("gap", "BC_SEVERAL_ORDERS_FOR_NUMBER",
                             "More than one BC purchase order has this No. (several companies "
                             "or case-insensitive matches); none is picked.",
                             [o["citation"] for o in orders]))
    gaps = [i for i in issues if i["kind"] == "gap"]
    conflicts = [i for i in issues if i["kind"] == "conflict"]
    return {"schema": SCHEMA, "source": "business-central",
            "environment": evidence.get("environment"), "po_number": searched,
            "evidence_label": evidence_label, "adapter_status": ADAPTER_STATUS,
            "authority": "advisory: cited BC Production reads (GET only); never action "
                         "authority, never a write.",
            "status": status, "complete": complete,
            "identity_asserted": any(o["portal_link"]["identity_asserted"] for o in orders),
            "orders": orders, "receipts": receipts, "invoices": invoices,
            "gaps": gaps, "conflicts": conflicts,
            "caveats": list(evidence.get("caveats", []))}


__all__ = ["ADAPTER_STATUS", "SCHEMA", "build_purchase_case", "portal_po_link"]
