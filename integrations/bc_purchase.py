"""Business Central purchase documents for one PO number, with line-level citations.

Reads, for every company the actor may read (or the given ones), through
``integrations.bc_read`` (Production, GET only, closed request grammar,
audience checked before any request):

* ``purchaseOrders`` whose No. equals the number, and their ``purchaseOrderLines``;
* posted ``purchaseReceipts`` whose ``orderNumber`` equals the number, and their
  ``purchaseReceiptLines``;
* ``purchaseInvoices`` whose ``orderNumber`` equals the number, plus those whose
  ``vendorInvoiceNumber`` equals a vendor invoice No. the case supplies (Portal
  ``app_receipt.vendor_invoice_number``), and their ``purchaseInvoiceLines``;
* with ``scoped_completeness``, also ``purchaseInvoices`` whose ``orderId`` is a found
  order's id (the key ``PurchaseInvoice.invoices`` joins on) and the ``items`` whose No.
  is an Item line's ``lineObjectNumber`` (the key of the lines' ``for_item`` links; a
  Comment, Account, Charge, Resource or Fixed Asset line names no item and is not looked
  up, and the process graph does not count it on a three-way side).

``scope_claims`` turns one read into scoped completeness claims (source ``bc``, scope
``po=<number>``, ``read_at``): per entity set and found order, the anchors whose
related records were read to the end (no remaining ``nextLink``, no error, no record
cap, nothing withheld) and the fields that join them. A claim names the records it
covers; it is never a claim about a whole entity set (``ontology_tools_process``).

Field mapping mirrors Portal (plx-customer-portal staging @5724575):

* PO line cost is ``directUnitCost``; purchase-invoice line cost is ``unitCost``
  (``business-central/serializers.ts:1409,1546``; our live probe found
  ``directUnitCost`` absent on ``purchaseInvoiceLines``, 7cecf4c).
* Posted receipts are read back by ``orderNumber eq '<BC PO No.>'``
  (``outbox-drain.ts:1224``; ``bc_receipt_snapshot.order_number``). Unverified
  by our live probe until ``bc.purchase_reader`` / ``bc.case_entities`` pass.
* Purchase invoices on this tenant often lack ``orderNumber``; Portal also
  matches the vendor invoice Nos. recorded on its receipts for the PO
  (``document-finance-queries.ts:290-300``). An invoice found only by vendor
  invoice No. is kept, but marked when its vendor differs from the order's.
* Invoice header fields as Portal's inbound projection
  (``inbound.ts:375 projectPurchaseInvoice``).

Nothing here decides identity or arithmetic: every document is a cited source
record (``bc://`` citation, etag, content hash) found by a number match, which
is a reference, not identity (``bc_case_evidence``); ``operating.bc_purchase_case``
builds the case view. Every read is reported with its status (``read``,
``not_permitted`` before any I/O, ``unavailable`` on a source error) and
completeness, so silence is never read as absence. Quantities and money stay
the exact decimal text BC sent; an absent field is ``None``, never ``0``. BC's
empty date ``0001-01-01`` is reported as absent.

Probe (credentialed live check; counts, statuses and field-presence only, no
values): ``python3 -m integrations.bc_purchase probe``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from ontology.core import Actor

from . import live_source_grammar as grammar
from .bc_read import (BusinessCentralReader, build_bc, load_company_books,
                      load_company_scope, select_pinned)
from .capture_store import bc_blank_date
from .live_source_guard import (BoundedBackoff, LiveSourceAudience, NotVisible,
                                ReadOnlyGuardError, SourceUnavailable, failure_diagnostics)


SCHEMA = "plx-bc-purchase-evidence/v1"

# $select per entity set: only allowlisted names (live_source_grammar.BC_QUERY_SPECS),
# so the read carries no address or contact fields.
SELECT: dict[str, tuple[str, ...]] = {
    "purchaseOrders": ("number", "orderDate", "postingDate", "requestedReceiptDate",
                       "vendorId", "vendorNumber", "vendorName", "currencyCode",
                       "pricesIncludeTax", "status", "fullyReceived", "purchaser",
                       "totalAmountExcludingTax", "totalTaxAmount", "totalAmountIncludingTax"),
    "purchaseOrderLines": ("sequence", "itemId", "lineType", "lineObjectNumber",
                           "description", "unitOfMeasureCode", "quantity", "directUnitCost",
                           "discountPercent", "amountExcludingTax", "expectedReceiptDate",
                           "receivedQuantity", "invoicedQuantity"),
    "purchaseReceipts": ("number", "orderNumber", "postingDate", "vendorNumber", "vendorName",
                         "currencyCode"),
    "purchaseReceiptLines": ("sequence", "lineType", "lineObjectNumber", "description",
                             "unitOfMeasureCode", "quantity", "unitCost",
                             "expectedReceiptDate"),
    "purchaseInvoices": ("number", "vendorInvoiceNumber", "orderId", "orderNumber",
                         "invoiceDate", "postingDate", "dueDate", "vendorId", "vendorNumber",
                         "vendorName", "currencyCode", "pricesIncludeTax", "status",
                         "totalAmountExcludingTax", "totalTaxAmount", "totalAmountIncludingTax"),
    "purchaseInvoiceLines": ("sequence", "itemId", "lineType", "lineObjectNumber",
                             "description", "unitOfMeasureCode", "quantity", "unitCost",
                             "discountPercent", "amountExcludingTax"),
    "items": ("number", "displayName", "type", "baseUnitOfMeasureCode", "blocked"),
}

# Per line set: (field, role, quantity type or None). Roles are what the field
# counts; ``quantity_type`` is an ``operating.quantity_types`` member when one
# exists ("invoiced" is not one: it is never combined with ordered/received).
QUANTITY_FIELDS: dict[str, tuple[tuple[str, str, str | None], ...]] = {
    "purchaseOrderLines": (("quantity", "ordered", "ordered"),
                           ("receivedQuantity", "received", "received"),
                           ("invoicedQuantity", "invoiced", None)),
    "purchaseReceiptLines": (("quantity", "received", "received"),),
    "purchaseInvoiceLines": (("quantity", "invoiced", None),),
}
COST_FIELD = {"purchaseOrderLines": "directUnitCost", "purchaseReceiptLines": "unitCost",
              "purchaseInvoiceLines": "unitCost"}
DATE_FIELDS = ("orderDate", "postingDate", "requestedReceiptDate", "invoiceDate", "dueDate",
               "expectedReceiptDate")
KIND = {"purchaseOrders": "purchase_order", "purchaseReceipts": "purchase_receipt",
        "purchaseInvoices": "purchase_invoice", "items": "item"}
# find_by_number statuses of a filtered read that returned every match
_READ_IN_FULL = frozenset({"none_found", "single_candidate", "ambiguous"})

CAVEATS: dict[str, str] = {
    "number_match_is_reference": (
        "Documents were found by number equality: a reference, not identity. Portal's "
        "app_bc_entity_map is the identity link between a Portal PO and a BC document."),
    "your_reference_not_exposed": (
        "The FM PO number is written to BC 'Your Reference' (codeunit 50115), which API "
        "v2.0 purchaseOrders does not expose; an FM number cannot be checked here."),
    "external_document_no_not_customer_po": (
        "BC External Document No. is never read as a customer PO: the ship-only bridge "
        "overwrites the sales header's External Document No. with the shipment No. "
        "(REC-10)."),
    "invoice_order_number_often_blank": (
        "Purchase invoices on this tenant often lack orderNumber (Portal "
        "document-finance-queries.ts:290); invoices are also searched by the vendor "
        "invoice Nos. the case supplies."),
    "orders_deleted_when_complete": (
        "BC deletes a purchase order once it is fully received and invoiced; a missing "
        "order is not a missing purchase."),
}


def date_value(value: Any) -> str | None:
    """BC sends ``0001-01-01`` for an empty date: absent, not a date."""
    if not isinstance(value, str) or not value.strip() or bc_blank_date(value):
        return None
    return value


def _exact(fields: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = fields.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"field": name, "value": None, "status": "absent"}
    if isinstance(value, bool):
        return {"field": name, "value": None, "status": "invalid"}
    return {"field": name, "value": str(value), "status": "read"}


def _line(record: Mapping[str, Any]) -> dict[str, Any]:
    fields = record["fields"]
    line_set = record["entity_set"]
    line_type = fields.get("lineType")
    quantities = [{"role": role, "quantity_type": qtype, **_exact(fields, name)}
                  for name, role, qtype in QUANTITY_FIELDS[line_set]]
    return {"citation": record["citation"], "id": record["id"], "etag": record["etag"],
            "content_sha256": record["content_sha256"], "entity_set": line_set,
            "sequence": fields.get("sequence"),
            "line_type": line_type if isinstance(line_type, str) else None,
            "item_number": fields.get("lineObjectNumber") or None,
            "item_id": fields.get("itemId") or None,
            "description": fields.get("description"),
            "unit_of_measure_code": fields.get("unitOfMeasureCode") or None,
            "quantities": quantities,
            "unit_cost": _exact(fields, COST_FIELD[line_set]),
            "amount_excluding_tax": _exact(fields, "amountExcludingTax"),
            "expected_receipt_date": date_value(fields.get("expectedReceiptDate"))}


def _document(record: Mapping[str, Any], matched_by: dict[str, Any]) -> dict[str, Any]:
    fields = record["fields"]
    header = {k: fields.get(k) for k in SELECT[record["entity_set"]]
              if k in fields and k not in DATE_FIELDS}
    dates = {k: date_value(fields.get(k)) for k in DATE_FIELDS if k in fields}
    currency = fields.get("currencyCode")
    return {"kind": KIND[record["entity_set"]], "citation": record["citation"],
            "company_id": record["company_id"], "entity_set": record["entity_set"],
            "id": record["id"], "etag": record["etag"],
            "content_sha256": record["content_sha256"],
            "last_modified": record.get("last_modified"),
            "number": fields.get("number"), "header": header, "dates": dates,
            "currency_code": currency if isinstance(currency, str) and currency.strip()
            else None,
            "evidence_role": "reference", "identity_asserted": False,
            "matched_by": [matched_by], "lines": None}


def _read_status(found: Mapping[str, Any]) -> str:
    return found["status"]


def read_purchase_evidence(reader: BusinessCentralReader, actor: Actor, po_number: str, *,
                           company_ids: Sequence[str] | None = None,
                           vendor_invoice_numbers: Sequence[str] = (),
                           max_documents: int = 20, line_page_size: int = 100,
                           line_max_pages: int = 5, line_max_items: int = 500,
                           scoped_completeness: bool = False,
                           clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """BC purchase order, receipts and invoices for ``po_number`` (see module docstring).

    ``company_ids`` defaults to every company the actor may list. A read the
    audience refuses is ``not_permitted`` with no I/O; a source failure is
    ``unavailable``; a capped listing is ``truncated``. ``complete`` is true only
    when every read was performed in full. ``scoped_completeness`` adds the reads
    ``scope_claims`` needs to cover every link of the PO's three-way sides (invoices by
    ``orderId``, item masters); ``read_at`` is when the read started."""
    if not grammar.bc_number_ok(po_number):
        raise ValueError("po_number invalid")
    if isinstance(vendor_invoice_numbers, (str, bytes)):
        raise ValueError("vendor_invoice_numbers must be a sequence")
    started = (clock or (lambda: datetime.now(timezone.utc)))()
    report: dict[str, Any] = {"schema": SCHEMA, "source": "business-central",
                              "environment": reader.environment, "po_number": po_number,
                              "read_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "scoped_completeness": bool(scoped_completeness),
                              "evidence_role": "reference", "identity_asserted": False,
                              "caveats": [CAVEATS[k] for k in CAVEATS],
                              "vendor_invoice_numbers": [], "companies": []}
    vins: list[str] = []
    for vin in dict.fromkeys(vendor_invoice_numbers):
        ok = grammar.bc_number_ok(vin)
        report["vendor_invoice_numbers"].append({"value": vin, "searched": ok,
                                                 **({} if ok else {"reason": "not_searchable"})})
        if ok:
            vins.append(vin)
    incomplete = False
    if company_ids is None:
        try:
            listing = reader.companies(actor)
        except NotVisible:
            report.update({"companies_status": "not_permitted", "complete": False})
            return report
        except SourceUnavailable as exc:
            report.update({"companies_status": "unavailable", "error": str(exc),
                           "complete": False})
            return report
        company_ids = [c["id"] for c in listing["items"] if isinstance(c.get("id"), str)]
        report["companies_status"] = "read"
        report["companies_completeness"] = listing["completeness"]
        incomplete = bool(listing["completeness"].get("truncated"))
    for company_id in company_ids:
        company, company_complete = _read_company(
            reader, actor, str(company_id), po_number, vins, max_documents=max_documents,
            line_page_size=line_page_size, line_max_pages=line_max_pages,
            line_max_items=line_max_items, scoped=bool(scoped_completeness))
        report["companies"].append(company)
        incomplete = incomplete or not company_complete
    report["complete"] = not incomplete
    return report


def _read_company(reader: BusinessCentralReader, actor: Actor, company_id: str,
                  po_number: str, vins: Sequence[str], *, max_documents: int,
                  line_page_size: int, line_max_pages: int, line_max_items: int,
                  scoped: bool = False) -> tuple[dict[str, Any], bool]:
    company: dict[str, Any] = {"company_id": company_id.lower(), "reads": [],
                               "orders": [], "receipts": [], "invoices": [], "items": []}
    complete = True
    seen: dict[tuple[str, str], dict[str, Any]] = {}

    def search(entity_set: str, field: str, value: str, bucket: str) -> None:
        nonlocal complete
        read: dict[str, Any] = {"kind": "search", "entity_set": entity_set, "field": field,
                                "value": value}
        company["reads"].append(read)
        if field == "number" and not grammar.bc_number_ok(value):
            read["status"] = "not_searchable"  # a line's No. no BC filter can carry
            complete = False
            return
        try:
            found = reader.find_by_number(actor, company_id, entity_set, value, field=field,
                                          max_matches=max_documents,
                                          select=SELECT[entity_set])
        except NotVisible:
            read["status"] = "not_permitted"
            complete = False
            return
        except SourceUnavailable as exc:
            read.update({"status": "unavailable", "error": str(exc)})
            complete = False
            return
        read.update({"status": _read_status(found), "match_count": found["match_count"],
                     "completeness": found["completeness"]})
        if found["status"] in ("truncated", "partially_withheld"):
            complete = False
        for match in found["matches"]:
            record = match["record"]
            matched = {"field": field, "value": record["fields"].get(field),
                       "case_exact": match["case_exact"]}
            key = (record["entity_set"], record["id"])
            if key in seen:
                seen[key]["matched_by"].append(matched)
                continue
            document = _document(record, matched)
            seen[key] = document
            company[bucket].append(document)

    search("purchaseOrders", "number", po_number, "orders")
    search("purchaseReceipts", "orderNumber", po_number, "receipts")
    search("purchaseInvoices", "orderNumber", po_number, "invoices")
    for vin in vins:
        search("purchaseInvoices", "vendorInvoiceNumber", vin, "invoices")
    if scoped:
        for order in list(company["orders"]):
            search("purchaseInvoices", "orderId", order["id"], "invoices")
    for bucket in ("orders", "receipts", "invoices"):
        for document in company[bucket]:
            document["lines"] = _read_lines(reader, actor, company_id, document,
                                            page_size=line_page_size, max_pages=line_max_pages,
                                            max_items=line_max_items)
            if not document["lines"]["complete"]:
                complete = False
    if scoped:
        for number in sorted({line["item_number"] for bucket in ("orders", "receipts",
                                                                 "invoices")
                              for document in company[bucket]
                              for line in document["lines"]["items"] if _names_item(line)}):
            search("items", "number", number, "items")
    company["complete"] = complete
    return company, complete


def _names_item(line: Mapping[str, Any]) -> bool:
    """Whether a line's ``lineObjectNumber`` may be a BC item No.: an Item line, or one
    whose type was not read. A Comment, Account (G/L), Charge, Resource or Fixed Asset
    line names another kind of record; its number is never looked up as an item."""
    kind = line.get("line_type")
    return bool(line["item_number"]) and (kind is None or kind.strip().casefold() == "item")


def _read_in_full(read: Mapping[str, Any]) -> bool:
    return (read.get("status") in _READ_IN_FULL
            and not read.get("completeness", {}).get("provenance_dropped"))


def scope_claims(evidence: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Scoped completeness claims of one ``read_purchase_evidence`` report, per BC entity
    set (a capture source lists them as ``describe()["relations"][<set>]["scopes"]``).

    Each claim is ``{"scope": "po=<number>", "company_id", "anchors", "by", "complete",
    "read_at"}``: the records of the set whose ``by`` field joins one of ``anchors`` (BC
    record ids of this company) were all read, ``complete`` when every such read ended
    with no remaining page, error, record cap or withheld record. Per found order: its
    lines, receipts (``orderNumber``) and invoices (``orderNumber``, plus ``orderId``
    when read); per company: the lines of every receipt and invoice found and, when item
    masters were read, the items of every line (``number``). Nothing is claimed for a
    company where no order was found (no root) or for a set the read did not filter."""
    scope = f"po={evidence['po_number']}"
    out: dict[str, list[dict[str, Any]]] = {}
    for company in evidence.get("companies", ()):
        if not company["orders"]:
            continue

        def claim(entity_set: str, anchors: Sequence[str], by: Sequence[str],
                  complete: bool) -> None:
            out.setdefault(entity_set, []).append({
                "scope": scope, "company_id": company["company_id"],
                "anchors": sorted(anchors), "by": list(by), "complete": bool(complete),
                "read_at": evidence["read_at"]})

        def searched(entity_set: str, field: str, value: str | None = None) -> list:
            return [r for r in company["reads"] if r["entity_set"] == entity_set
                    and r["field"] == field and (value is None or r["value"] == value)]

        def lines_in_full(documents: Sequence[Mapping[str, Any]]) -> bool:
            return all((d["lines"] or {}).get("complete") for d in documents)

        receipts_read = searched("purchaseReceipts", "orderNumber")
        by_number = searched("purchaseInvoices", "orderNumber")
        for order in company["orders"]:
            claim("purchaseOrderLines", [order["id"]], ["documentId"], lines_in_full([order]))
            claim("purchaseReceipts", [order["id"]], ["orderNumber"],
                  all(_read_in_full(r) for r in receipts_read))
            by_id = searched("purchaseInvoices", "orderId", order["id"])
            claim("purchaseInvoices", [order["id"]],
                  ["orderNumber"] + (["orderId"] if by_id else []),
                  all(_read_in_full(r) for r in by_number + by_id))
        for bucket, line_set in (("receipts", "purchaseReceiptLines"),
                                 ("invoices", "purchaseInvoiceLines")):
            if company[bucket]:
                claim(line_set, [d["id"] for d in company[bucket]], ["documentId"],
                      lines_in_full(company[bucket]))
        if evidence.get("scoped_completeness"):
            lines = [line for bucket in ("orders", "receipts", "invoices")
                     for document in company[bucket]
                     for line in (document["lines"] or {}).get("items", ())
                     if line["item_number"] is None or _names_item(line)]
            reads = [searched("items", "number", line["item_number"])
                     for line in lines if line["item_number"]]
            claim("items", [line["id"] for line in lines], ["number"],
                  all(found and all(_read_in_full(r) for r in found) for found in reads))
    return out


def _read_lines(reader: BusinessCentralReader, actor: Actor, company_id: str,
                document: Mapping[str, Any], *, page_size: int, max_pages: int,
                max_items: int) -> dict[str, Any]:
    line_set = grammar.BC_DOCUMENT_LINES[document["entity_set"]]
    try:
        listing = reader.list_lines(actor, company_id, document["entity_set"], document["id"],
                                    select=SELECT[line_set], page_size=page_size,
                                    max_pages=max_pages, max_items=max_items)
    except NotVisible:
        return {"entity_set": line_set, "status": "not_permitted", "items": [],
                "complete": False}
    except SourceUnavailable as exc:
        return {"entity_set": line_set, "status": "unavailable", "error": str(exc),
                "items": [], "complete": False}
    completeness = listing["completeness"]
    complete = (not completeness.get("truncated") and not completeness.get("withheld_count")
                and not completeness.get("provenance_dropped"))
    return {"entity_set": line_set, "status": "read", "complete": complete,
            "completeness": completeness, "items": [_line(r) for r in listing["items"]]}


# --------------------------------------------------------------------- probe

def _cost(line: Mapping[str, Any]) -> Decimal | None:
    read = line["unit_cost"]
    if read["status"] != "read":
        return None
    try:
        return Decimal(read["value"])
    except (InvalidOperation, TypeError):
        return None


def _price_pairs(company: Mapping[str, Any]) -> dict[str, int]:
    """Counts only: whether a PO line's ``directUnitCost`` and its invoice line's
    ``unitCost`` differ (docs/decisions/2026-10-08-bc-three-way-price-basis.md, what
    would reverse it). An invoice Item line pairs with the one PO Item line of the same
    item No. and unit of measure code (none or several: unpaired); a pair whose costs are
    not both read is ``cost_absent``; the rest are ``equal`` or ``differ`` as Decimals."""
    orders: dict[tuple[Any, Any], int] = {}
    costs: dict[tuple[Any, Any], Decimal | None] = {}
    for document in company["orders"]:
        for line in document["lines"]["items"]:
            if line["line_type"] == "Item":
                key = (line["item_number"], line["unit_of_measure_code"])
                orders[key] = orders.get(key, 0) + 1
                costs[key] = _cost(line)
    out = dict.fromkeys(("invoice_lines", "paired", "equal", "differ", "unpaired",
                         "cost_absent"), 0)
    for document in company["invoices"]:
        for line in document["lines"]["items"]:
            if line["line_type"] != "Item":
                continue
            out["invoice_lines"] += 1
            key = (line["item_number"], line["unit_of_measure_code"])
            if orders.get(key) != 1:
                out["unpaired"] += 1
                continue
            out["paired"] += 1
            ordered, invoiced = costs[key], _cost(line)
            out["cost_absent" if ordered is None or invoiced is None else
                "equal" if ordered == invoiced else "differ"] += 1
    return out


def _shape(company: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    """One company's reads: counts, statuses, field presence and finding codes; no values."""
    lines: dict[str, Any] = {}
    absent: set[str] = set()
    roles: set[str] = set()
    for bucket in ("orders", "receipts", "invoices"):
        for document in company[bucket]:
            block = document["lines"]
            entry = lines.setdefault(block["entity_set"], {"read": 0, "unavailable": 0,
                                                           "not_permitted": 0, "lines": 0})
            entry[block["status"]] += 1
            entry["lines"] += len(block["items"])
            for item in block["items"]:
                if item["line_type"] not in ("Item", None):
                    continue
                for quantity in item["quantities"]:
                    if quantity["status"] == "read":
                        roles.add(f"{block['entity_set']}.{quantity['role']}")
                    else:
                        absent.add(f"{block['entity_set']}.{quantity['field']}")
                if item["unit_cost"]["status"] != "read":
                    absent.add(f"{block['entity_set']}.{item['unit_cost']['field']}")
    return {"complete": company.get("complete"),
            "reads": [{"entity_set": r["entity_set"], "field": r["field"],
                       "status": r["status"]} for r in company["reads"]],
            "documents": {b: len(company[b]) for b in ("orders", "receipts", "invoices")},
            "lines": lines,
            "unavailable_reads": [f"{r['entity_set']}.{r['field']}" for r in company["reads"]
                                  if r["status"] == "unavailable"]
            + [f"{k}.lines" for k, v in sorted(lines.items()) if v["unavailable"]],
            "quantity_roles_read": sorted(roles), "line_fields_absent": sorted(absent),
            "currency_present": [d["currency_code"] is not None
                                 for b in ("orders", "receipts", "invoices")
                                 for d in company[b]],
            "price_pairs": _price_pairs(company),
            "case_status": case.get("status"),
            "gap_codes": sorted({g["code"] for g in case.get("gaps", [])}),
            "conflict_codes": sorted({c["code"] for c in case.get("conflicts", [])})}


def _seed_failure(reader: BusinessCentralReader, actor: Actor, company_id: str,
                  entity_set: str, select: Sequence[str], exc: Exception) -> dict[str, Any]:
    """Which seed read failed and how (``failure_diagnostics``: class, HTTP status,
    error-code class, detail token; never a message or value). After a source
    failure the same one-record read is sent once more without ``$select`` (the
    shape ``bc_read probe --case-entities`` reads with), so a live result tells a
    refused projection (``without_select.status`` ``read``) from an outage."""
    failure = {"read": entity_set, "select": list(select), **failure_diagnostics(exc)}
    if isinstance(exc, SourceUnavailable):
        try:
            reader.list_entities(actor, company_id, entity_set, top=1, page_size=1,
                                 max_pages=1, max_items=1)
            failure["without_select"] = {"status": "read"}
        except (SourceUnavailable, NotVisible) as again:
            failure["without_select"] = {"status": "unavailable", **failure_diagnostics(again)}
    return failure


def probe(environ: Mapping[str, str] | None = None, *, sender=None,
          retry: BoundedBackoff | None = None,
          books: Mapping[str, Mapping[str, str]] | None = None,
          names: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Per company (by index): seed a PO number from the newest-listed posted
    receipt's ``orderNumber`` (a PO with receipts), else from the first purchase
    order, read the full purchase evidence for it in that company and build the
    case view. Reports shapes only; the seed number is never printed.

    A failed seed read is ``receipt_seed_error`` / ``order_seed_error``
    (``_seed_failure``). A company without a seed has ``seed_from`` ``none`` and a
    ``seed_reason``: ``no_purchase_documents`` (both reads succeeded and were
    empty: an empty book), ``no_order_number`` (documents listed, none with a
    valid number) or ``read_failed`` (a seed read failed, so absence is unknown).

    Only the companies ``bc_company_books.json`` pins for the environment are read (Production:
    My Company; ``books`` / ``names`` override the shipped pins). Every other listed company is
    counted (``out_of_scope_companies``) and never read; a pinned GUID listed under another
    name is counted (``pinned_name_mismatch``) and not read."""
    from operating.bc_purchase_case import build_purchase_case  # case view (no I/O)

    api, transport, tenant_id, environment = build_bc(environ, sender=sender, retry=retry)
    env = environment.lower()
    audience = LiveSourceAudience({f"bc:{env}:*": ["probe:operator"]}, tenant="probe")
    actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
    reader = BusinessCentralReader(api, audience, tenant_id=tenant_id, environment=environment)
    companies = reader.companies(actor)
    only_pinned, shipped_names = (load_company_scope() if books is None
                                  else (frozenset({env}), {}))
    targets, counts = select_pinned(
        companies["items"], env, (load_company_books() if books is None else books).get(env, {}),
        only_pinned=env in only_pinned,
        names=(shipped_names if names is None else names).get(env))
    chosen = {company_id for company_id, _book in targets}
    report: dict[str, Any] = {"source": "business-central", "evidence_label": "live-read",
                              "adapter_status": "live-unverified",
                              "read_only_guard": "GET-only; token POST only",
                              "environment_is_production": env == "production",
                              "companies_listed": len(companies["items"]),
                              "companies_count": len(targets),
                              "unpinned_companies": counts["unpinned"],
                              "out_of_scope_companies": counts["out_of_scope"],
                              "pinned_name_mismatch": counts["name_mismatch"],
                              "companies": []}
    receipt_select, order_select = ("number", "orderNumber"), ("number",)
    for index, company in enumerate(c for c in companies["items"]
                                    if str(c.get("id", "")).lower() in chosen):
        entry: dict[str, Any] = {"company_index": index}
        report["companies"].append(entry)
        company_id = company.get("id")
        if not isinstance(company_id, str):
            entry["error"] = "company id missing"
            continue
        seed, seed_from, listed, failed = None, "none", False, False
        try:
            receipts = reader.list_entities(actor, company_id, "purchaseReceipts", top=1,
                                            select=receipt_select, page_size=1,
                                            max_pages=1, max_items=1)
            listed = bool(receipts["items"])
            entry["receipt_order_number_present"] = (
                listed
                and grammar.bc_number_ok(receipts["items"][0]["fields"].get("orderNumber")))
            if entry["receipt_order_number_present"]:
                seed, seed_from = receipts["items"][0]["fields"]["orderNumber"], "receipt"
        except (SourceUnavailable, NotVisible) as exc:
            failed = True
            entry["receipt_seed_error"] = _seed_failure(reader, actor, company_id,
                                                        "purchaseReceipts", receipt_select, exc)
        if seed is None:
            try:
                orders = reader.list_entities(actor, company_id, "purchaseOrders", top=1,
                                              select=order_select, page_size=1, max_pages=1,
                                              max_items=1)
                listed = listed or bool(orders["items"])
                number = orders["items"][0]["fields"].get("number") if orders["items"] else None
                if grammar.bc_number_ok(number):
                    seed, seed_from = number, "purchase_order"
            except (SourceUnavailable, NotVisible) as exc:
                failed = True
                entry["order_seed_error"] = _seed_failure(reader, actor, company_id,
                                                          "purchaseOrders", order_select, exc)
        entry["seed_from"] = seed_from
        if seed is None:
            entry["seed_reason"] = ("read_failed" if failed else
                                    "no_order_number" if listed else "no_purchase_documents")
            continue
        evidence = read_purchase_evidence(reader, actor, seed, company_ids=[company_id],
                                          max_documents=5, line_page_size=50,
                                          line_max_pages=2, line_max_items=100,
                                          scoped_completeness=True)
        case = build_purchase_case(evidence, evidence_label="live-read")
        entry.update(_shape(evidence["companies"][0], case))
    report["requests_sent"] = transport.requests_sent
    report["get_retries"] = api.retry.retries if api.retry is not None else 0
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender=None, retry: BoundedBackoff | None = None,
         books: Mapping[str, Mapping[str, str]] | None = None,
         names: Mapping[str, Mapping[str, str]] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.bc_purchase",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="read-only shapes of the purchase reader (no values)")
    parser.parse_args(argv)
    try:
        result = probe(environ, sender=sender, retry=retry, books=books, names=names)
    except ReadOnlyGuardError as exc:
        print(json.dumps({"error": "read_only_guard", "message": str(exc)}), file=sys.stderr)
        return 2
    except (SourceUnavailable, NotVisible, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


__all__ = ["CAVEATS", "QUANTITY_FIELDS", "SCHEMA", "SELECT", "date_value", "main", "probe",
           "read_purchase_evidence", "scope_claims"]


if __name__ == "__main__":
    raise SystemExit(main())
