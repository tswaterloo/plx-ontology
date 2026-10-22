"""Reconciliation rule REC-10: BC "External Document No." against the customer PO.

Why this rule exists (REC-10, ``operating/process_stage_map.json``
weak links, 02 section 1.7 #6). Portal sends the customer PO (``po_reference``) to the
BC sales order as ``externalDocumentNumber`` at creation, but the ship-only bridge then
writes the Portal shipment number into the same Sales Header field, and the entity-map
column ``bc_external_document_number`` is never written. So a BC sales order's External
Document No. is one of: the customer PO, a shipment number (overwritten), the Portal
order number, blank, or something else. This module classifies it. It never infers a
customer PO from BC: BC is only ever the thing being compared.

Upstream behaviour mirrored (plx-customer-portal, read-only, staging @4243499 (bridge @1c5dddb), re-read
2026-10-17; the bytes at the earlier commits named below are unchanged where they are cited):

* ``portal/src/lib/business-central/serializers.ts@4243499:1221-1222`` (``serializeSalesOrder``):
  ``externalDocumentNumber = clean(po_reference).slice(0, 35)``, no PO-shape check, so a
  person's name in ``po_reference`` is sent as the "PO". ``:1193`` puts it in the header.
* ``portal/src/lib/business-central/serializers.ts@4243499:1113`` (``serializeSalesInvoice``):
  HISTORICAL: an invoice the Portal posted before TASK-2271 had External Document No. = the
  Portal ``order_number`` (``SO-<id>`` fallback), capped at 35. Since TASK-2271
  (``outbox-producer.ts@4243499:546-555``, ``produceSalesInvoices`` throws) and
  ``outbox-drain.ts@4243499:92-93`` (``ship-invoice`` removed) BC alone raises and posts the
  invoices that bill a sales order, from the SO; the Portal no longer enqueues them. Those
  invoices still exist in BC, so the carrier stays a lookup lead. A BC-raised invoice is
  BC-authored: its External Document No. comes from the SO header (customer PO, or the
  shipment No. after the ship-only post), never from the Portal serializer, and the Portal
  writes no ``app_bc_entity_map`` ``salesInvoice`` row for it.
* The one Portal-raised sales invoice since then (TASK-2318,
  ``three-pl-invoice.ts@4243499:184-231``): a FINAL 3PL billing run becomes an unposted BC
  draft (header and lines POSTed, never posted; BC staff post it) whose External Document No.
  is the run number, with no ``orderNumber``, and the drain writes its ``salesInvoice``
  entity-map row right after the header. So a Portal-authored invoice after the cutover
  carries a map row (``portal_possible``), and having no order number it is never joined to
  a Portal order (``bc_document_without_order_number``). Return credit memos (TASK-803,
  ``credit-memo.ts@4243499:148-206``: draft only, External Document No. = the return ref) are
  ``salesCreditMemos``, outside ``ENTITY_SETS``.
* ``bc-extensions/plx-receipt-bridge/src/PlxSalesBridge.Codeunit.al@8802bee:162-164``
  (``PostSalesShipOnly``, called from ``outbox-drain.ts@4243499:674`` with
  ``shipmentNo: payload.shipmentNumber``): ``SalesHeader.Validate("External Document No.",
  CopyStr(ShipmentNo, 1, MaxStrLen(...)))`` before posting. This is the overwrite (the same
  lines at 4243499). ``CloseSalesOrder`` (TASK-2660, ``@4243499:231-305``) short-closes the
  unshipped remainder and stamps "PLX Closed"; it does not touch the field.
* ``PlxSalesBridge.Codeunit.al@1c5dddb:185-186``: the overwrite is GONE for shipments posted
  from this version on: ``shipmentNo`` goes to the dedicated "PLX Shipment No." (and
  ``releaseNo`` to "PLX Release No."), and the customer PO in External Document No. is
  preserved. The drain still sends ``shipmentNo`` (``outbox-drain.ts@1c5dddb:674``). Orders
  shipped before that deploy keep the overwritten value in BC, so ``matches_shipment_number``
  stays a valid historical class; this module cannot tell when an order shipped, so a
  ``matches_shipment_number`` finding is a lead about past data, never proof the bridge still
  overwrites. Nothing in the classification changes.
* ``serializers.ts@4243499:1412``: ``shipmentNumber = clean(shipment_number) ?? "SHP-<id>"``
  (capped at 35); ``outbox-producer.ts@4243499:874`` reads it as ``app_shipment.shipment_code``.
* ``PlxSalesOrderRef.Codeunit.al@8802bee:16``: the MO-code stamp does not touch the field.

Classes (``CLASSES``), decided in this order, all conservative:

* ``blank``: BC value empty or absent.
* ``matches_customer_po``: equals (trim, case-insensitive, first 35 characters, since BC
  caps the field) a Portal customer-PO candidate that is PO-like
  (``customer_po_coverage.po_like``). Never a person's name, never the order number.
* ``matches_shipment_number``: equals a Portal shipment number of the same order. This is
  the ship-bridge overwrite; the finding says whether a PO-like customer PO is known in
  Portal and therefore no longer on the BC order. Equal to both a PO and a shipment
  number is ``unknown`` (``matches_po_and_shipment``).
* ``matches_order_number``: equals the Portal order number (an invoice's by-design
  value; on a sales order the Portal bridge's custpocode fallback). Not a PO.
* ``unrelated``: non-blank, matches nothing, and the comparison was complete: at least one
  PO-like Portal candidate AND the order's shipment numbers were read.
* ``unknown``: everything else, with a ``reason``: BC document not found or not unique,
  no PO-like Portal candidate (the BC value could be the true PO, which this rule never
  asserts), shipment numbers not read, BC equals a not-PO-like ``po_reference`` (the
  person-name trap: Portal sent it, it is not a PO), a candidate field not inspectable,
  or both PO and shipment match.

Invoices (``salesInvoices``) are first classified by authorship (``invoice_authorship``):

* ``portal_possible``: the Portal may have produced or linked it: an ``app_bc_entity_map``
  ``salesInvoice`` row (proves a link, not that the serializer wrote the value: ship-invoice
  mode wrote that row too) or an invoice date before ``PORTAL_INVOICING_CUTOVER`` (before it the
  default was ship-only, ship-invoice was opt-in and BC users could always raise invoices, so
  the date proves nothing about the writer). Lenient: a match on the order number, customer
  PO or shipment number is accepted and its class names the carrier; a no-match is
  ``unknown`` / ``unmatched_portal_possible``, never ``unrelated``. No class claims the
  serializer wrote the value.
* ``bc_native``: raised by BC: no entity-map row (the map was read) AND an invoice date on or
  after the cutover. Judged like a sales order (customer PO, shipment number), a mismatch
  is ``unrelated``.
* ``unknown``: anything else (map not read, or no usable date), class ``unknown``, reason
  ``invoice_authorship_unknown``, never a mismatch. The cutover is the commit date of
  TASK-2271 (``69bd4f7`` on staging, 2026-10-03T08:08:15-07:00): a LOWER bound for when
  the Portal stopped producing invoices (deployment is later), so a date after it alone
  never proves BC authorship; the Portal deploy date is unevidenced.

A ``SHP-<digits>`` value with no shipment list is only a lead (``looks_like_ship_fallback``).
Findings (``plx-reconciliation-finding/v1``) carry citations (BC ``bc://``, Portal
``api://``), the upstream code citations, ``identity_asserted: false`` and the reader's
evidence label (``injected-transport`` or ``live-read``, never ``live-verified``). A
not-PO-like value is never echoed.

Hook for ``operating/customer_po_coverage.py`` (describe only; that module is not edited
here): after ``classify_order(row, columns)`` yields ``status == "absent_in_this_source"``
or ``po_field_not_po_like``, a caller with a ``BusinessCentralReader`` calls
``reconcile_order(reader, actor, company_id, order_number=..., candidates=..., ...)``
and files ``finding["class"]`` and ``finding["citations"]`` beside the order. A
``matches_shipment_number`` finding means BC lost a PO that Portal may still hold; a
``matches_customer_po`` finding is a cited second source for the PO, but only
Portal/FM-held values ever become the proposal. The case runtime may record the finding
as evidence with ``finding["citations"]`` so permission checks apply at read time.

Bounded run (``reconcile_recent_orders``) samples BC first. The reporting API serves a
replica of ``plx-postgres-staging`` while the staging Portal app drains BC Production from
``plx-postgres-uat`` (``docs/reference/portal-code-map.md`` "BC drain, UAT prefix"), and the
producer sends only the newest orders (``outbox-producer.ts@4243499:442-480``; since TASK-2396
never a closed-out order, ``BC_LINE_SYNC_FROZEN_SQL``), so the most
recent replica orders are, as a rule, orders BC never received: sampling them found no BC
document for any of them (live 2026-10-08, ``none_found`` 10/10). The run therefore lists
recent BC documents per company, newest first (sales orders by ``orderDate desc``,
invoices by ``invoiceDate desc``). Classes that need BC alone are reported first, for
every sampled document, before any replica read: a blank External Document No. is
``blank``, and on a sales order a value equal to the document's own ``number`` is
``matches_order_number`` / ``equals_bc_document_number`` (Portal and BC carry the same
order number, ``align-portal-so-number.ts@d07f912:1-12``). ``bc_sample.by_class`` counts
the documents per BC-only class, ``portal_dependent`` and ``unknown``. Only the
``portal_dependent`` ones are joined to the replica by the BC document's own order number
(sales order ``number``, invoice ``orderNumber``; the drain makes the Portal
``order_number`` equal to it, ``align-portal-so-number.ts@549f6f0:7-12``), never by the
External Document No. under test. A BC document with no order number is ``unknown`` /
``bc_document_without_order_number``, one whose number fails the BC literal grammar
``order_number_invalid``. A BC order number the replica does not hold is ``unknown`` /
``not_in_served_replica``. For a held one, which companies hold the number comes
from one bounded listing of the order-number field per company (``_number_index``), not
from one lookup per order and company; a company whose listing is incomplete never proves
absence and gets the per-number lookup. The one holder then gets the lookup that
classifies. On a sales order the Portal ``order_date`` and BC ``orderDate`` are compared
as calendar days, each in its own source's zone (``_order_date_check``): the serializer
writes ``orderDate`` as the UTC day of the Portal instant (``toIsoDate``,
``serializers.ts@4243499:1100-1105``, ``:1205``), so a zoned Portal timestamp is turned
into its UTC day and a BC value must be a plain date. One without a zone is read as UTC
(``customer_po_coverage.parse_date_value``), and that is the column's real zone:
``app_sales_order.order_date`` is a Prisma ``DateTime`` without ``@db.Timestamptz``
(``portal/prisma/schema.prisma@a8d54b1:3972``, ``SalesOrder.orderDate``), so Postgres
``timestamp(3) without time zone`` holding the UTC instant Prisma writes. Another day is ``unknown`` / ``order_date_conflict`` (two
databases can hand out the same number to different orders); anything else is ``unknown`` /
``order_date_not_comparable``, never a guessed day. ``summary.portal_compared`` counts the
findings classified against a served Portal order (``basis`` ``portal``) and
``summary.bc_only`` those BC alone settled (``basis`` ``bc_only``), never a Portal comparison;
when every BC order number the run needed the replica for is absent from it,
``all_not_in_served_replica`` is true and the CLI reports ``blocked`` with that cause. When
no sampled document needed the Portal at all (every finding ``bc_only``, every company read
and nothing withheld), ``no_portal_dependent_document`` is true and the CLI reports
``blocked`` ``no_portal_dependent_document``: no Portal comparison was possible, so the run
neither passes nor fails. A run with Portal-dependent documents that compares none, and not
because the replica lacks them all, still fails.

CLI (counts only, for ``verify/live-checks/case.rec10_external_doc.json``)::

    python3 -m operating.reconciliation_rules --state-dir D --audience A --principal P

Shipment-line lots (TASK-3248, staging @c09de5d, re-read 2026-10-20 at @0323e19c), mirrored by
``shipment_line_bc_lots`` so a BC posted-shipment lot can be compared with the Portal line
it came from. ``serializers.ts@c09de5d:1433-1482`` sends BC the BASE lot
(``lotNumberBase``, ``wms/lot-numbers.ts@c09de5d:24-27``: the text before ``@``, trimmed),
never the WMS ``@BIN`` suffix. The lots that fed a line are its confirmed picks
(``outbox-producer.ts@0323e19c:766-819``; none, or picks that no longer attribute: the
stamped ``lot_number`` and the lot's ``expiry_date``, ``:997-1016``). Bins of one base lot
merge (quantities add, the earliest expiry wins); a pick with no base lot or no positive
quantity is dropped. Two or more base lots go as ``lots[]`` (no ``lotNumber``), one as
``lotNumber`` + ``expiryDate`` for the whole line quantity; the drain maps them to
``lots[{lotNo, quantity, expiryDate}]`` / ``lotNo`` (``outbox-drain.ts@c09de5d:660-676``). Bridge 0.9.18.0
(``PlxSalesBridge.Codeunit.al@c09de5d:395-494``) makes one tracking entry per lot with
"Lot No." (older bridges set only "New Lot No.", which BC drops on posting, and ignore
``lots``) and does NOT post the expiry: BC takes it from the lot's inbound entries. Unlike
upstream, an unparseable expiry or quantity is refused here, never read as today or 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

from integrations.live_source_grammar import bc_number_ok
from ontology.core import Actor

from .customer_po_coverage import (CANDIDATE_FIELDS, _field_value, classify_field,
                                   order_identifiers, parse_date_value)

RULE_ID = "rec10.bc_external_document_number"
SCHEMA = "plx-reconciliation-finding/v1"
CLASSES = ("matches_customer_po", "matches_shipment_number", "matches_order_number",
           "blank", "unrelated", "unknown")
ENTITY_SETS = ("salesOrders", "salesInvoices")
BC_FIELD_MAX = 35  # Sales Header "External Document No." Text[35]
# plx-customer-portal 69bd4f7 (TASK-2271) committed on staging 2026-10-03T08:08:15-07:00.
# A lower bound: the Portal may have produced invoices until its deploy, so the date alone
# only proves Portal authorship before it, never BC authorship after it.
PORTAL_INVOICING_CUTOVER = "2026-10-03"
AUTHORSHIP = ("portal_possible", "bc_native", "unknown")
_SHIP_FALLBACK = re.compile(r"^SHP-[A-Za-z0-9]+$")

UPSTREAM_CITATIONS = (
    {"path": "portal/src/lib/business-central/serializers.ts", "commit": "4243499",
     "lines": "1221-1222", "fact": "sales order External Document No. = clean(po_reference)[:35]"},
    {"path": "portal/src/lib/business-central/serializers.ts", "commit": "4243499",
     "lines": "1113", "fact": "historical (before TASK-2271) Portal-posted sales invoice External Document No. = order_number[:35]"},
    {"path": "portal/src/lib/business-central/outbox-producer.ts", "commit": "4243499",
     "lines": "546-555", "fact": "produceSalesInvoices throws since TASK-2271: BC alone raises sales invoices for a sales order"},
    {"path": "portal/src/lib/business-central/three-pl-invoice.ts", "commit": "4243499",
     "lines": "184-231", "fact": "the one Portal-raised sales invoice (TASK-2318): an unposted 3PL billing draft, External Document No. = run number, no order number, entity-map row written"},
    {"path": "portal/src/lib/business-central/outbox-drain.ts", "commit": "4243499",
     "lines": "92-93", "fact": "ship-invoice mode removed since TASK-2271; the drain only posts ship-only"},
    {"path": "bc-extensions/plx-receipt-bridge/src/PlxSalesBridge.Codeunit.al",
     "commit": "8802bee", "lines": "162-164",
     "fact": "ship-only bridge overwrites Sales Header External Document No. with shipmentNo"},
    {"path": "bc-extensions/plx-receipt-bridge/src/PlxSalesBridge.Codeunit.al",
     "commit": "1c5dddb", "lines": "185-186",
     "fact": "bridge no longer overwrites External Document No.: shipmentNo goes to PLX Shipment No. (orders shipped before this keep the overwritten value)"},
    {"path": "bc-extensions/plx-receipt-bridge/src/PlxSalesBridge.Codeunit.al",
     "commit": "1c5dddb", "lines": "289-294",
     "fact": "bridged invoice copies the posted shipment's External Document No. (:291)"},
    {"path": "portal/src/lib/business-central/outbox-drain.ts", "commit": "4243499",
     "lines": "674", "fact": "drain passes shipmentNo = payload.shipmentNumber to the bridge"},
    {"path": "portal/src/lib/business-central/align-portal-so-number.ts", "commit": "d07f912",
     "lines": "1-12", "fact": "Portal order_number and the BC sales order No. are the same number"},
)


SHIPMENT_LOT_BRIDGE_VERSION = "0.9.18.0"
SHIPMENT_LOT_CITATIONS = (
    {"path": "portal/src/lib/wms/lot-numbers.ts", "commit": "c09de5d", "lines": "24-27",
     "fact": "base lot = the text before '@', trimmed (no WMS bin suffix)"},
    {"path": "portal/src/lib/business-central/serializers.ts", "commit": "c09de5d",
     "lines": "1433-1482", "fact": "a shipment line sends its base lots: two or more as lots[] (bins merged, earliest expiry), one as lotNumber + expiryDate, none: the stamped base lot"},
    {"path": "portal/src/lib/business-central/outbox-producer.ts", "commit": "0323e19c",
     "lines": "766-819", "fact": "a line's lots are its confirmed picks; no picks or no attribution: the stamped lot"},
    {"path": "portal/src/lib/business-central/outbox-drain.ts", "commit": "c09de5d",
     "lines": "660-676", "fact": "drain sends lots[{lotNo, quantity, expiryDate}] (bridge 0.9.18.0; older bridges ignore lots)"},
    {"path": "bc-extensions/plx-receipt-bridge/src/PlxSalesBridge.Codeunit.al",
     "commit": "c09de5d", "lines": "395-494",
     "fact": "bridge 0.9.18.0: one tracking entry per lot with Lot No.; the expiry is not posted"},
)


def _lot_quantity(value: Any, what: str) -> Decimal | None:
    """``None`` for an absent value (upstream ``num``: null, undefined, ""); else a finite
    Decimal. Anything else is refused, never read as 0."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{what}: quantity is not a number")
    try:
        quantity = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"{what}: quantity is not a number") from None
    if not quantity.is_finite():
        raise ValueError(f"{what}: quantity is not finite")
    return quantity


def _lot_expiry(value: Any, what: str) -> str | None:
    """The UTC day of an expiry (upstream ``toIsoDate``), ``None`` when absent. Upstream
    sends today for an unparseable value; refused here."""
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, (date, datetime)) else parse_date_value(value)
    if isinstance(parsed, datetime):
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    if isinstance(parsed, date):
        return parsed.isoformat()
    raise ValueError(f"{what}: expiry date is not a date")


def _lot_text(value: Any, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what}: lot number is not text")
    return value


def shipment_line_bc_lots(line: Mapping[str, Any]) -> dict[str, Any]:
    """The lots a Portal sales shipment line sends BC (TASK-3248, module docstring).

    ``line``: ``quantity``, ``lot_number``, ``expiry_date`` and optional ``lots`` (the picks
    that fed it: ``lot_number``, ``quantity``, ``expiry_date``). Returns ``shape`` (``lots``,
    ``lot`` or ``none``), ``source`` (``picks`` or ``stamped``), ``lots`` (``lot_number``,
    ``quantity`` as Decimal, ``expiry_date`` YYYY-MM-DD or None) and ``skipped`` (dropped
    picks with a reason). A line with lots names the bridge version that tracks them and
    that the expiry is sent but not posted. Unknown shapes raise ``ValueError``.
    """
    if not isinstance(line, Mapping):
        raise ValueError("shipment line is not a row")
    quantity = _lot_quantity(line.get("quantity"), "shipment line") or Decimal(0)
    picks = line.get("lots")
    if picks is not None and not isinstance(picks, (list, tuple)):
        raise ValueError("shipment line: lots is not a list")
    merged: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for index, pick in enumerate(picks or ()):
        what = f"shipment line lot {index}"
        if not isinstance(pick, Mapping):
            raise ValueError(f"{what} is not a row")
        raw = _lot_text(pick.get("lot_number"), what)
        base = raw.split("@")[0].strip()
        picked = _lot_quantity(pick.get("quantity"), what) or Decimal(0)
        expiry = _lot_expiry(pick.get("expiry_date"), what)
        if not base or picked <= 0:
            skipped.append({"lot_number": raw.strip() or None,
                            "reason": "no_base_lot" if not base else "quantity_not_positive"})
            continue
        entry = merged.setdefault(base, {"lot_number": base, "quantity": Decimal(0),
                                         "expiry_date": None})
        entry["quantity"] += picked
        if expiry and (entry["expiry_date"] is None or expiry < entry["expiry_date"]):
            entry["expiry_date"] = expiry
    lots = [merged[base] for base in sorted(merged)]
    source = "picks"
    if len(lots) == 1:
        lots = [dict(lots[0], quantity=quantity)]   # one lot tracks the whole line quantity
    elif not lots:
        source = "stamped"
        stamped = _lot_text(line.get("lot_number"), "shipment line").split("@")[0].strip()
        expiry = _lot_expiry(line.get("expiry_date"), "shipment line")
        lots = [{"lot_number": stamped, "quantity": quantity, "expiry_date": expiry}] if stamped else []
        if not stamped:
            out = {"shape": "none", "source": source, "lots": [], "skipped": skipped}
            if expiry:
                out["unlotted_expiry_date"] = expiry   # upstream still sends expiryDate
            return out
    return {"shape": "lots" if len(lots) > 1 else "lot", "source": source, "lots": lots,
            "skipped": skipped, "bridge_min_version": SHIPMENT_LOT_BRIDGE_VERSION,
            "expiry_posted": False}


def _text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _same(left: str, right: str) -> bool:
    """BC caps the field at 35 characters and may compare Code text case-insensitively."""
    return left[:BC_FIELD_MAX].casefold() == right[:BC_FIELD_MAX].casefold()


def _day(value: Any) -> str | None:
    text = _text(value)
    return text[:10] if text and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else None


def _order_date_check(portal_value: Any, bc_value: Any) -> str | None:
    """``None`` (same day, or a side holds no date), ``"order_date_conflict"`` or
    ``"order_date_not_comparable"``. The Portal day is the UTC day of its instant (the zone
    ``toIsoDate`` writes BC ``orderDate`` in; a zoneless timestamp reads as UTC); the BC
    value must be a plain date (``Edm.Date``), taken as written."""
    if _text(portal_value) is None or _text(bc_value) is None:
        return None
    portal = parse_date_value(portal_value)
    bc_day = parse_date_value(bc_value)
    if portal is None or bc_day is None or isinstance(bc_day, datetime):
        return "order_date_not_comparable"
    portal_day = portal.astimezone(timezone.utc).date() if isinstance(portal, datetime) else portal
    return None if portal_day == bc_day else "order_date_conflict"


def invoice_authorship(*, entity_map_row: bool | None, invoice_date: Any) -> str:
    """Who produced a BC sales invoice. ``entity_map_row``: a Portal ``salesInvoice`` map
    row names it (``None``: the map was not read). ``invoice_date``: ISO date or datetime."""
    day = _day(invoice_date)
    if entity_map_row:
        return "portal_possible"
    if day is not None and day < PORTAL_INVOICING_CUTOVER:
        return "portal_possible"
    if entity_map_row is False and day is not None:
        return "bc_native"
    return "unknown"


def classify_external_document(bc_value: Any, *, candidates: Sequence[Mapping[str, Any]],
                               order_numbers: Mapping[str, str] | None = None,
                               shipment_numbers: Sequence[Any] | None = None,
                               authorship: str | None = None) -> dict[str, Any]:
    """Pure rule. ``candidates``: ``{"source", "value", optional "exists"}`` Portal/FM
    customer-PO fields; ``order_numbers``: the order's own numbers (a PO field that
    equals one is not a PO); ``shipment_numbers``: the order's Portal shipment numbers,
    or ``None`` when they were not read. ``authorship`` is ``None`` for a sales order and
    ``invoice_authorship``'s answer for an invoice."""
    if authorship is not None and authorship not in AUTHORSHIP:
        raise ValueError("authorship must be one of " + ", ".join(AUTHORSHIP))
    order_numbers = dict(order_numbers or {})
    po_values: list[str] = []
    nonpo_values: list[str] = []
    uninspectable: list[str] = []
    candidate_status: dict[str, str] = {}
    for item in candidates:
        exists = item.get("exists", True)
        status = classify_field(exists, item.get("value"), identifiers=order_numbers)
        candidate_status[str(item.get("source"))] = status["status"]
        if status["status"] == "present_po_like":
            po_values.append(status["value"])
        elif status["status"] == "present_not_po_like":
            text = _text(item.get("value"))
            if text:
                nonpo_values.append(text)
        elif status["status"] == "not_inspectable":
            uninspectable.append(str(item.get("source")))
    ships = None if shipment_numbers is None else [
        t for t in (_text(v) for v in shipment_numbers) if t]
    result: dict[str, Any] = {
        "candidate_status": candidate_status,
        "portal_po_like_known": bool(po_values),
        "shipment_numbers_read": ships is not None,
        "shipment_count": None if ships is None else len(ships)}
    value = _text(bc_value)
    if value is None:
        return {**result, "class": "blank", "reason": "bc_value_empty"}
    if authorship is not None:
        result["authorship"] = authorship
    if authorship == "unknown":
        return {**result, "class": "unknown", "reason": "invoice_authorship_unknown"}
    po_hit = any(_same(value, po) for po in po_values)
    ship_hit = ships is not None and any(_same(value, s) for s in ships)
    order_hit = any(_same(value, n) for n in order_numbers.values())
    nonpo_hit = any(_same(value, n) for n in nonpo_values)
    result["case_exact"] = any(
        value[:BC_FIELD_MAX] == known[:BC_FIELD_MAX]
        for known in [*po_values, *(ships or []), *order_numbers.values()])
    if po_hit and ship_hit:
        return {**result, "class": "unknown", "reason": "matches_po_and_shipment"}
    if po_hit:
        return {**result, "class": "matches_customer_po", "reason": "equals_portal_po_like_field"}
    if ship_hit:
        return {**result, "class": "matches_shipment_number",
                "reason": "ship_only_bridge_overwrite",
                "customer_po_lost_in_bc": bool(po_values)}
    if order_hit:
        return {**result, "class": "matches_order_number", "reason": "equals_portal_order_number"}
    if nonpo_hit:
        return {**result, "class": "unknown", "reason": "equals_not_po_like_portal_field"}
    if uninspectable:
        return {**result, "class": "unknown", "reason": "portal_field_not_inspectable"}
    if ships is None:
        if _SHIP_FALLBACK.fullmatch(value):
            result["looks_like_ship_fallback"] = True
        return {**result, "class": "unknown", "reason": "shipment_numbers_not_read"}
    if not po_values:
        return {**result, "class": "unknown", "reason": "no_po_like_portal_candidate"}
    if authorship == "portal_possible":
        return {**result, "class": "unknown", "reason": "unmatched_portal_possible"}
    return {**result, "class": "unrelated", "reason": "matches_no_known_value"}


def _citations(record: Mapping[str, Any] | None, extra: Sequence[str]) -> list[str]:
    found = [c for c in extra if isinstance(c, str) and c]
    if record and record.get("citation"):
        found.append(record["citation"])
    return found


def _finding(subject: Mapping[str, Any], classified: Mapping[str, Any],
             record: Mapping[str, Any] | None, citations: Sequence[str],
             label: str, bc_value: Any = None) -> dict[str, Any]:
    cls = classified["class"]
    finding = {"schema": SCHEMA, "rule": RULE_ID, **classified,
               "subject": dict(subject), "evidence_label": label,
               "identity_asserted": False, "advisory": True,
               "citations": _citations(record, citations),
               "upstream_citations": [dict(c) for c in UPSTREAM_CITATIONS]}
    # Echo the BC text only when it equals a value Portal already holds.
    if cls in ("matches_customer_po", "matches_shipment_number", "matches_order_number"):
        finding["bc_external_document_number"] = _text(bc_value)
    elif _text(bc_value) is not None:
        finding["bc_value_withheld"] = True
        finding["bc_value_length"] = len(_text(bc_value) or "")
    return finding


def _unknown(subject: Mapping[str, Any], reason: str, label: str,
             citations: Sequence[str]) -> dict[str, Any]:
    return _finding(subject, {"class": "unknown", "reason": reason}, None, citations, label)


def reconcile_order(reader: Any, actor: Actor, company_id: str, *, order_number: str,
                    candidates: Sequence[Mapping[str, Any]],
                    order_numbers: Mapping[str, str] | None = None,
                    shipment_numbers: Sequence[Any] | None = None,
                    entity_set: str = "salesOrders",
                    portal_citations: Sequence[str] = (),
                    invoice_map_ids: Collection[str] | None = None,
                    order_date: Any = None) -> list[dict[str, Any]]:
    """Read the BC document(s) for ``order_number`` (a lead lookup, GET only) and classify
    each one's External Document No. One finding per BC record; a lookup that is not
    exactly one sales order yields one ``unknown`` finding. Invoices are looked up by
    ``orderNumber`` and may be several. ``invoice_map_ids``: BC ids of invoices that have a
    Portal ``salesInvoice`` entity-map row (``None``: the map was not read). ``order_date``:
    the Portal order date; a sales order whose BC ``orderDate`` is another calendar day is
    not the same order (``order_date_conflict``), and days that cannot be compared are
    ``order_date_not_comparable`` (``_order_date_check``)."""
    from integrations.live_source_guard import NotVisible, SourceUnavailable

    if entity_set not in ENTITY_SETS:
        raise ValueError("entity_set must be salesOrders or salesInvoices")
    label = getattr(reader, "evidence_label", "injected-transport")
    subject = {"order_number": order_number, "company_id": company_id,
               "entity_set": entity_set}
    field = "number" if entity_set == "salesOrders" else "orderNumber"
    try:
        lookup = reader.find_by_number(actor, company_id, entity_set, order_number,
                                       field=field, select=["number", "externalDocumentNumber", "invoiceDate", "postingDate"]
                                       if entity_set == "salesInvoices"
                                       else ["number", "externalDocumentNumber", "orderDate"])
    except NotVisible:
        return [_unknown(subject, "bc_not_permitted", label, portal_citations)]
    except (SourceUnavailable, ValueError) as exc:
        return [_unknown(subject, "bc_read_failed:" + type(exc).__name__, label,
                         portal_citations)]
    matches = lookup["matches"]
    if not matches:
        return [_unknown(subject, "bc_document_not_found", label, portal_citations)]
    hidden = lookup["status"] in ("truncated", "partially_withheld")
    if hidden or (entity_set == "salesOrders" and len(matches) != 1):
        return [_unknown(subject, "bc_document_not_unique_or_withheld", label,
                         portal_citations)]
    findings = []
    for match in matches:
        record = match["record"]
        date_reason = (_order_date_check(order_date, record["fields"].get("orderDate"))
                       if entity_set == "salesOrders" else None)
        if date_reason is not None:
            return [_finding({**subject, "bc_id": record["id"]},
                             {"class": "unknown", "reason": date_reason}, record,
                             portal_citations, label)]
        value = record["fields"].get("externalDocumentNumber")
        authorship = None
        if entity_set == "salesInvoices":
            fields = record["fields"]
            authorship = invoice_authorship(
                entity_map_row=None if invoice_map_ids is None else record["id"] in invoice_map_ids,
                invoice_date=fields.get("invoiceDate") or fields.get("postingDate"))
        classified = classify_external_document(value, candidates=candidates,
                                                order_numbers=order_numbers,
                                                shipment_numbers=shipment_numbers,
                                                authorship=authorship)
        findings.append(_finding({**subject, "bc_id": record["id"]},
                                 {**classified, "basis": "portal"}, record,
                                 portal_citations, label, value))
    return findings


def summarize(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts per class, per unknown reason and per invoice authorship (no values).
    ``portal_compared`` counts the findings classified (a class other than ``unknown`` or
    ``blank``) against a served Portal order (``basis`` ``portal``); ``bc_only`` the
    findings BC alone settled (``basis`` ``bc_only``), which compare nothing with the
    Portal. A run with no Portal comparison never passes the live check (``_run_split``
    says when it is blocked instead of failed)."""
    by_class = {name: 0 for name in CLASSES}
    reasons: dict[str, int] = {}
    by_authorship: dict[str, int] = {}
    overwrites_with_po_lost = 0
    for item in findings:
        by_class[item["class"]] += 1
        if item.get("authorship"):
            by_authorship[item["authorship"]] = by_authorship.get(item["authorship"], 0) + 1
        if item["class"] == "unknown":
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        if item.get("customer_po_lost_in_bc"):
            overwrites_with_po_lost += 1
    portal_compared = sum(1 for item in findings if item.get("basis") == "portal"
                          and item["class"] not in ("unknown", "blank"))
    bc_only = sum(1 for item in findings if item.get("basis") == "bc_only")
    return {"findings": len(findings), "portal_compared": portal_compared, "bc_only": bc_only,
            "by_class": by_class, "unknown_reasons": reasons,
            "invoice_authorship": by_authorship,
            "shipment_overwrite_with_portal_po_known": overwrites_with_po_lost}


def portal_candidates(row: Mapping[str, Any], columns: Sequence[str]) -> list[dict[str, Any]]:
    """The mirror row's customer-PO fields, in the shape ``reconcile_order`` takes. FM
    ``order_entry.custpocode`` and Sage ``ponumber`` are not read here."""
    result = []
    for path, column, key in CANDIDATE_FIELDS:
        exists, value = _field_value(row, columns, column, key)
        result.append({"source": path, "exists": exists, "value": value})
    return result


MIRROR_PAGE = 500       # SourceMirror.rows allows limit 1..500
SHIPMENT_ID_CHUNK = 200  # the mirror's "in" takes 1..200 values
SHIPMENT_MAX_PAGES = 10
_NOT_HELD = "bc_document_not_found"


def _read_shipments(mirror: Any, actor: Actor, ids: Sequence[str],
                    max_staleness: float | None
                    ) -> tuple[dict[str, list[str]] | None, dict[str, list[str]], str | None]:
    """Shipment codes and citations per Portal order id, read through the mirror in
    pages of at most 500. ``None`` (unread) on a mirror refusal or when any page bound
    is hit: the rule then never says ``unrelated`` or ``matches_shipment_number``."""
    codes: dict[str, list[str]] = {i: [] for i in ids}
    cites: dict[str, list[str]] = {}
    for start in range(0, len(ids), SHIPMENT_ID_CHUNK):
        chunk = list(ids[start:start + SHIPMENT_ID_CHUNK])
        offset = 0
        for _ in range(SHIPMENT_MAX_PAGES):
            try:
                page = mirror.rows(actor, "public.app_shipment", columns=None,
                                   where=[["sales_order_id", "in", chunk]],
                                   limit=MIRROR_PAGE, offset=offset,
                                   max_staleness=max_staleness)
            except Exception as exc:
                if type(exc).__name__ != "MirrorError":
                    raise
                return None, {}, "mirror_refused:" + str(getattr(exc, "code", "error"))
            for item in page["rows"]:
                sid = str(item["row"].get("sales_order_id"))
                code = item["row"].get("shipment_code")
                if sid in codes and code:
                    codes[sid].append(code)
                    cites.setdefault(sid, []).append(item["citation"])
            if page.get("next_offset") is None:
                break
            offset = page["next_offset"]
        else:
            return None, {}, "shipment_page_bound_reached"
    return codes, cites, None


def _one_company(order_number: str, per_company: Sequence[list[dict[str, Any]] | None],
                 subject: Mapping[str, Any], label: str,
                 citations: Sequence[str]) -> list[dict[str, Any]]:
    """Classify only when exactly one BC company holds the number. ``None``: the
    company's complete number index does not hold it (no lookup was sent)."""
    def kind(found: list[dict[str, Any]] | None) -> str:
        if found is None:
            return "not_held"
        reason = found[0]["reason"] if len(found) == 1 and found[0]["class"] == "unknown" else ""
        if reason == _NOT_HELD:
            return "not_held"
        if reason == "bc_not_permitted" or reason.startswith("bc_read_failed"):
            return "failed"
        return "holder"

    kinds = [kind(f) for f in per_company]
    holders = [f for f, k in zip(per_company, kinds) if k == "holder"]
    failed = [f for f, k in zip(per_company, kinds) if k == "failed"]
    if len(holders) > 1:
        return [_unknown(subject, "ambiguous_across_companies", label, citations)]
    if failed:
        return [_unknown(subject, "company_read_incomplete", label, citations)]
    if not holders:
        return [_unknown(subject, "none_found", label, citations)]
    return holders[0]


# The BC field that holds a document's own order number (the join key to the replica).
ORDER_NUMBER_FIELD = {"salesOrders": "number", "salesInvoices": "orderNumber"}
SAMPLE_SELECT = {"salesOrders": ["number", "externalDocumentNumber", "orderDate"],
                 "salesInvoices": ["number", "orderNumber", "externalDocumentNumber"]}
SAMPLE_ORDERBY = {"salesOrders": "orderDate desc", "salesInvoices": "invoiceDate desc"}
INDEX_PAGE = 1000      # BC's largest page (live_source_grammar.BC_MAX_TOP)
INDEX_MAX_PAGES = 5


def _sample_bc(reader: Any, actor: Actor, company_ids: Sequence[str], entity_set: str,
               limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Up to ``limit`` BC documents per company, newest first (GET only; ``$orderby`` is a
    read-only query option from the entity set's allowlist)."""
    from integrations.live_source_guard import NotVisible, SourceUnavailable

    docs: list[dict[str, Any]] = []
    stats = {"companies_unread": 0, "withheld": 0}
    for company in company_ids:
        try:
            listing = reader.list_entities(actor, company, entity_set,
                                           select=SAMPLE_SELECT[entity_set], top=limit,
                                           orderby=SAMPLE_ORDERBY[entity_set], page_size=limit,
                                           max_pages=1, max_items=limit)
        except (NotVisible, SourceUnavailable, ValueError):
            stats["companies_unread"] += 1
            continue
        stats["withheld"] += int(listing["completeness"].get("withheld_count") or 0)
        docs.extend(listing["items"])
    return docs, stats


def _number_index(reader: Any, actor: Actor, company: str, entity_set: str
                  ) -> set[str] | None:
    """Every order number ``company`` holds in ``entity_set``, casefolded (BC may compare
    Code fields case-insensitively), from one listing of that field alone in pages of
    ``INDEX_PAGE`` (at most ``INDEX_MAX_PAGES`` GETs). ``None`` when the listing is
    refused, truncated or withholds a record: then it proves no number absent."""
    from integrations.live_source_guard import NotVisible, SourceUnavailable

    field = ORDER_NUMBER_FIELD[entity_set]
    try:
        listing = reader.list_entities(actor, company, entity_set, select=[field],
                                       page_size=INDEX_PAGE, max_pages=INDEX_MAX_PAGES,
                                       max_items=INDEX_PAGE * INDEX_MAX_PAGES)
    except (NotVisible, SourceUnavailable, ValueError):
        return None
    completeness = listing["completeness"]
    if completeness.get("truncated") or completeness.get("withheld_count"):
        return None
    return {t.casefold() for t in (_text(r["fields"].get(field)) for r in listing["items"])
            if t}


def _bc_only_class(record: Mapping[str, Any], number: str, entity_set: str
                   ) -> dict[str, Any] | None:
    """The class BC alone settles for a sampled document, or ``None`` when it needs the
    Portal order: a blank value, or a sales order's value equal to its own ``number``
    (Portal and BC carry the same order number, ``align-portal-so-number.ts``)."""
    value = _text(record["fields"].get("externalDocumentNumber"))
    if value is None:
        return {"class": "blank", "reason": "bc_value_empty", "basis": "bc_only"}
    if entity_set == "salesOrders" and _same(value, number):
        return {"class": "matches_order_number", "reason": "equals_bc_document_number",
                "basis": "bc_only"}
    return None


def _portal_orders(mirror: Any, actor: Actor, numbers: Sequence[str],
                   max_staleness: float | None
                   ) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """Replica orders whose ``order_number`` is one of ``numbers`` (exact), paged. A mirror
    refusal raises: without the Portal side nothing may be classified."""
    columns: list[str] = []
    found: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(numbers), SHIPMENT_ID_CHUNK):
        chunk = list(numbers[start:start + SHIPMENT_ID_CHUNK])
        offset = 0
        for _ in range(SHIPMENT_MAX_PAGES):
            page = mirror.rows(actor, "public.app_sales_order", columns=None,
                               where=[["order_number", "in", chunk]], limit=MIRROR_PAGE,
                               offset=offset, max_staleness=max_staleness)
            columns = list(page["columns"])
            for item in page["rows"]:
                found.setdefault(str(item["row"].get("order_number")), []).append(item)
            if page.get("next_offset") is None:
                break
            offset = page["next_offset"]
        else:
            raise ValueError("app_sales_order page bound reached")
    return columns, found


def reconcile_recent_orders(mirror: Any, reader: Any, actor: Actor, company_ids: Sequence[str],
                            *, limit: int = 10, entity_set: str = "salesOrders",
                            max_staleness: float | None = None) -> dict[str, Any]:
    """Bounded run over up to ``limit`` BC documents per company, BC first (module
    docstring): a document whose class BC alone settles (``_bc_only_class``) yields its
    finding before any replica read; each other distinct BC order number yields findings
    from the one BC company that holds it when the replica holds exactly one order with
    that number, else one
    ``unknown`` (``not_in_served_replica``, ``portal_order_not_unique``,
    ``ambiguous_across_companies``, ``bc_document_without_order_number``,
    ``order_number_invalid``). ``all_not_in_served_replica``: at least one number needed
    the replica and none is in it (no Portal comparison was possible).
    ``no_portal_dependent_document``: at least one document was sampled, every finding is
    ``bc_only`` and no company was unread or withheld a record. BC GETs: per
    company one sample and at most ``INDEX_MAX_PAGES`` index pages, then one lookup per
    held number and per company whose index is incomplete (``bc_sample.bc_requests``,
    token requests included)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be 1..50")
    if entity_set not in ENTITY_SETS:
        raise ValueError("entity_set must be salesOrders or salesInvoices")
    label = getattr(reader, "evidence_label", "injected-transport")
    sent_before = getattr(reader, "requests_sent", 0)
    docs, stats = _sample_bc(reader, actor, company_ids, entity_set, limit)
    findings: list[dict[str, Any]] = []
    by_number: dict[str, list[dict[str, Any]]] = {}
    sampled_numbers: set[str] = set()
    by_class = {"blank": 0, "matches_order_number": 0, "portal_dependent": 0, "unknown": 0}
    for record in docs:
        number = _text(record["fields"].get(ORDER_NUMBER_FIELD[entity_set]))
        if number is None or not bc_number_ok(number):
            by_class["unknown"] += 1
            findings.append(_finding({"entity_set": entity_set,
                                      "company_id": record.get("company_id"),
                                      "bc_id": record["id"]},
                                     {"class": "unknown",
                                      "reason": "bc_document_without_order_number"
                                      if number is None else "order_number_invalid"},
                                     record, [], label))
            continue
        sampled_numbers.add(number)
        bc_only = _bc_only_class(record, number, entity_set)
        if bc_only is not None:
            by_class[bc_only["class"]] += 1
            findings.append(_finding({"order_number": number, "entity_set": entity_set,
                                      "company_id": record.get("company_id"),
                                      "bc_id": record["id"]}, bc_only, record, [], label,
                                     record["fields"].get("externalDocumentNumber")))
            continue
        by_class["portal_dependent"] += 1
        by_number.setdefault(number, []).append(record)
    numbers = list(by_number)
    columns, portal = (_portal_orders(mirror, actor, numbers, max_staleness)
                       if numbers else ([], {}))
    held = {n: portal[n][0] for n in numbers if len(portal.get(n, [])) == 1}
    ids = [str(item["row"].get("id")) for item in held.values()
           if item["row"].get("id") is not None]
    shipments, cites, unread = (_read_shipments(mirror, actor, ids, max_staleness)
                                if ids else ({}, {}, None))
    index = {c: _number_index(reader, actor, c, entity_set) for c in company_ids} if held else {}
    for number in numbers:
        subject = {"order_number": number, "entity_set": entity_set}
        bc_cites = [r["citation"] for r in by_number[number] if r.get("citation")]
        if number not in held:
            reason = "portal_order_not_unique" if portal.get(number) else "not_in_served_replica"
            findings.append(_unknown(subject, reason, label,
                                     bc_cites + [i["citation"] for i in portal.get(number, [])]))
            continue
        item = held[number]
        row = item["row"]
        oid = str(row.get("id"))
        portal_cites = [item["citation"], *cites.get(oid, [])]
        sampled = {str(r.get("company_id")).lower() for r in by_number[number]}
        holders = [c for c in company_ids if c.lower() in sampled or (
            index[c] is not None and number.casefold() in index[c])]
        if len(holders) > 1:
            findings.append(_unknown(subject, "ambiguous_across_companies", label,
                                     portal_cites))
            continue
        per_company = [None if c not in holders and index[c] is not None else reconcile_order(
            reader, actor, c, order_number=number,
            candidates=portal_candidates(row, columns),
            order_numbers=order_identifiers(row, columns),
            shipment_numbers=None if shipments is None else shipments.get(oid, []),
            entity_set=entity_set, portal_citations=portal_cites,
            order_date=row.get("order_date")) for c in company_ids]
        findings.extend(_one_company(number, per_company, subject, label, portal_cites))
    sample = {"documents": len(docs), "order_numbers": len(sampled_numbers),
              "by_class": by_class,
              "in_served_replica": len(held),
              "not_in_served_replica": sum(1 for n in numbers if not portal.get(n)),
              "external_document_number_present": sum(
                  1 for r in docs if _text(r["fields"].get("externalDocumentNumber"))),
              "index_incomplete": sum(1 for v in index.values() if v is None),
              "bc_requests": getattr(reader, "requests_sent", 0) - sent_before,
              **stats}
    return {"orders_considered": len(sampled_numbers), "companies": len(company_ids),
            "all_not_in_served_replica": bool(numbers) and not any(portal.get(n) for n in numbers),
            "no_portal_dependent_document": bool(findings) and all(
                f.get("basis") == "bc_only" for f in findings)
            and not stats["companies_unread"] and not stats["withheld"],
            "shipments_read": shipments is not None, "shipments_unread_reason": unread,
            "entity_set": entity_set, "bc_sample": sample, "summary": summarize(findings),
            "findings": findings}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender=None) -> int:
    from integrations import bc_read
    from integrations.live_source_guard import LiveSourceAudience, NotVisible, SourceUnavailable
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from .case_runtime import CaseRuntime

    parser = argparse.ArgumentParser(
        prog="python3 -m operating.reconciliation_rules",
        description="REC-10 over a bounded sample of BC sales documents joined to the "
                    "Portal replica by order number; prints counts only.")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--audience", required=True, type=Path)
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--entity-set", default="salesOrders", choices=ENTITY_SETS)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    base_url, key = env.get("PLX_WORKBENCH_API_URL"), env.get("PLX_WORKBENCH_API_KEY")
    if not base_url or not key:
        parser.error("PLX_WORKBENCH_API_URL and PLX_WORKBENCH_API_KEY must be set (read-only)")
    actor = Actor(args.tenant, frozenset(args.principal))
    try:
        api, _, tenant_id, environment = bc_read.build_bc(env, sender=sender)
        bc_actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
        audience = LiveSourceAudience({f"bc:{environment.lower()}:*": ["probe:operator"]},
                                      tenant="probe")
        reader = bc_read.BusinessCentralReader(api, audience, tenant_id=tenant_id,
                                               environment=environment)
        companies = [c["id"] for c in reader.companies(bc_actor)["items"]
                     if isinstance(c.get("id"), str)]
    except (SourceUnavailable, NotVisible, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    runtime = CaseRuntime(args.state_dir, client=WorkbenchReadApiClient(base_url, key),
                          audience=args.audience, tenant=args.tenant)
    try:
        # The Portal side uses the caller's actor; BC uses the declared probe audience.
        result = _run_split(runtime.mirror, reader, actor, bc_actor, companies, args)
    finally:
        runtime.close()
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


def _run_split(mirror: Any, reader: Any, portal_actor: Actor, bc_actor: Actor,
               companies: Sequence[str], args: argparse.Namespace) -> dict[str, Any]:
    """Mirror reads run as ``portal_actor``; BC reads as ``bc_actor``. Counts only.
    ``blocked`` (the live check's ``blocked_when``) names why no Portal comparison was
    possible: ``not_in_served_replica`` (the served replica held none of the orders the run
    needed it for) or ``no_portal_dependent_document`` (no sampled document needed the
    Portal); else ``None``."""
    class _SplitReader:
        evidence_label = reader.evidence_label

        @property
        def requests_sent(self) -> int:
            return reader.requests_sent

        def find_by_number(self, _actor, *a, **k):
            return reader.find_by_number(bc_actor, *a, **k)

        def list_entities(self, _actor, *a, **k):
            return reader.list_entities(bc_actor, *a, **k)

    result = reconcile_recent_orders(mirror, _SplitReader(), portal_actor, companies,
                                     limit=args.limit, entity_set=args.entity_set)
    return {"evidence_label": reader.evidence_label, "label": "live-unverified",
            "orders_considered": result["orders_considered"],
            "all_not_in_served_replica": result["all_not_in_served_replica"],
            "no_portal_dependent_document": result["no_portal_dependent_document"],
            "blocked": ("not_in_served_replica" if result["all_not_in_served_replica"]
                        else "no_portal_dependent_document"
                        if result["no_portal_dependent_document"] else None),
            "companies": result["companies"], "shipments_read": result["shipments_read"],
            "shipments_unread_reason": result["shipments_unread_reason"],
            "entity_set": result["entity_set"], "bc_sample": result["bc_sample"],
            "summary": result["summary"]}


__all__ = ["AUTHORSHIP", "CLASSES", "ENTITY_SETS", "PORTAL_INVOICING_CUTOVER", "RULE_ID", "SCHEMA", "UPSTREAM_CITATIONS",
           "SHIPMENT_LOT_BRIDGE_VERSION", "SHIPMENT_LOT_CITATIONS",
           "classify_external_document", "invoice_authorship", "main", "portal_candidates", "reconcile_order",
           "reconcile_recent_orders", "shipment_line_bc_lots", "summarize"]


if __name__ == "__main__":
    raise SystemExit(main())
