"""BC candidates for a Portal order / PO number: cited LEADS, never identity.

Given a number a case holds (a Portal sales-order number, a Portal or FM PO
number, a customer PO), this searches Business Central (Production, GET only,
through ``integrations.bc_read``) with exact-equality lookups and returns
every match as a cited candidate with the caveats that make a number match
weak evidence. It never picks a candidate, never links records and never
writes anything; identity between a Portal record and a BC document comes
from Portal's ``app_bc_entity_map`` (schema binding
``ontology/schema/domains/bc_entity_map.json``),
not from here.

Why numbers are weak (§1.7 #2/#6, §2.3;
03 §3.2-3.4):

* Portal ``app_purchase_order.po_number`` is rewritten to the BC No. once
  linked (and may carry an ``-FM<id>`` suffix on clashes); the FM PO number
  is often in BC "Your Reference" (a map reading of codeunit 50115; the link
  is judged on data evidence),
  which the standard API v2.0
  ``purchaseOrders`` set does not expose to this reader (ODataV4 Page 50
  ``PurchaseOrder`` does; only the BC capture reads it, field page
  ``purchaseOrderReferences``).
* Portal ``app_sales_order.order_number`` is rewritten to the BC document No.
  after drain (``alignPortalSoNumberToBc``); an older Portal number may match
  nothing, or a different document that happens to reuse it.
* BC sales-order "External Document No." is sent as ``po_reference``, but the
  ship-only bridge overwrites it with the shipment No.; ``po_reference`` can
  also hold a person's name.
* BC sales-invoice "External Document No." is sent as the Portal
  ``order_number`` (``serializers.ts:1049``); Portal's own invoice -> order
  resolution uses the entity map first and this number only as a fallback.
* BC equality on Code fields may ignore case: ``case_exact`` reports it.

Output (``plx-bc-case-leads/v1``): every search performed or skipped (with the
reason: ``not_permitted`` before any I/O, ``unavailable`` on a source error),
every candidate with its ``bc://`` citation and a few exact-text summary
fields, ``ambiguous`` whenever more than one distinct candidate exists or any
search was truncated, withheld, skipped or failed (so silence is never read as
"exactly one").
"""

from __future__ import annotations

from typing import Any, Sequence

from ontology.core import Actor

from . import live_source_grammar as grammar
from .bc_read import BusinessCentralReader
from .live_source_guard import NotVisible, SourceUnavailable


SCHEMA = "plx-bc-case-leads/v1"

CAVEATS: dict[str, str] = {
    "lead_not_identity": (
        "A BC number match is a lead, not identity: Portal's app_bc_entity_map is the "
        "identity link between a Portal record and a BC document."),
    "po_number_rewritten": (
        "Portal app_purchase_order.po_number is rewritten to the BC No. once linked and "
        "may carry an -FM<id> suffix; the FM PO number is often in BC 'Your Reference', "
        "which the API v2.0 purchaseOrders set does not expose here (ODataV4 Page 50 does; "
        "only the BC capture reads it)."),
    "order_number_rewritten": (
        "Portal app_sales_order.order_number is rewritten to the BC document No. after "
        "drain; a pre-rewrite Portal number may match nothing or another document."),
    "external_doc_no_overwritten": (
        "BC sales-order External Document No. starts as Portal po_reference but the "
        "ship-only bridge overwrites it with the shipment No.; po_reference may also "
        "hold a person's name. A match says what the field holds now, not the "
        "customer PO."),
    "invoice_external_doc_is_order_number": (
        "BC sales-invoice External Document No. is sent as the Portal order_number "
        "(pre- or post-rewrite); Portal resolves invoice -> order by the entity map "
        "first and by this number only as a fallback."),
    "invoice_order_number": (
        "A posted invoice's order No. names the BC order it was posted from; it does "
        "not by itself identify the Portal order."),
    "field_unverified_live": (
        "This filter field is from the API v2.0 schema and not yet confirmed by the "
        "live probe (field_names); an HTTP 400 means the field is absent."),
    "case_insensitive_match": (
        "BC matched this value ignoring case; the stored text differs in case."),
}

# (entity set, filter field, relation, caveat keys). Sales and purchase sides.
LOOKUPS: dict[str, tuple[tuple[str, str, str, tuple[str, ...]], ...]] = {
    "purchase": (
        ("purchaseOrders", "number", "bc_purchase_order_number",
         ("po_number_rewritten",)),
        ("purchaseInvoices", "orderNumber", "bc_purchase_invoice_order_number",
         ("invoice_order_number", "po_number_rewritten", "field_unverified_live")),
        ("purchaseInvoices", "number", "bc_purchase_invoice_number", ()),
    ),
    "sales": (
        ("salesOrders", "number", "bc_sales_order_number", ("order_number_rewritten",)),
        ("salesOrders", "externalDocumentNumber", "bc_sales_order_external_document_number",
         ("external_doc_no_overwritten",)),
        ("salesInvoices", "orderNumber", "bc_sales_invoice_order_number",
         ("invoice_order_number", "order_number_rewritten", "field_unverified_live")),
        ("salesInvoices", "externalDocumentNumber",
         "bc_sales_invoice_external_document_number",
         ("invoice_external_doc_is_order_number", "order_number_rewritten")),
        ("salesInvoices", "number", "bc_sales_invoice_number", ()),
    ),
}

SUMMARY_FIELDS = ("number", "externalDocumentNumber", "orderNumber", "vendorInvoiceNumber",
                  "vendorNumber", "customerNumber", "status", "orderDate", "invoiceDate",
                  "postingDate", "currencyCode", "totalAmountIncludingTax")


def bc_leads_for_number(reader: BusinessCentralReader, actor: Actor, number: str, *,
                        side: str = "both", company_ids: Sequence[str] | None = None,
                        max_matches: int = 20) -> dict[str, Any]:
    """All BC candidates for ``number`` as cited leads (see module docstring).

    ``side`` is ``purchase``, ``sales`` or ``both``. ``company_ids`` defaults
    to every company the actor may list (``bc:<env>:companies``); searches
    the audience does not grant are reported ``not_permitted`` with no I/O.
    """
    if not grammar.bc_number_ok(number):
        raise ValueError("number invalid")
    if side not in ("purchase", "sales", "both"):
        raise ValueError("side must be purchase, sales or both")
    lookups = [lookup for name in (("purchase", "sales") if side == "both" else (side,))
               for lookup in LOOKUPS[name]]
    report: dict[str, Any] = {"schema": SCHEMA, "source": "business-central",
                              "environment": reader.environment, "number": number,
                              "side": side, "evidence_role": "lead",
                              "identity_asserted": False,
                              "caveats": [CAVEATS["lead_not_identity"]],
                              "searches": [], "candidates": []}
    if company_ids is None:
        try:
            listing = reader.companies(actor)
        except (NotVisible, SourceUnavailable) as exc:
            # The company list unread is every search skipped: never a crash of the caller
            # (a token refused or a throttled listing is a source error like any search's).
            report.update({"companies_status": "not_permitted" if isinstance(exc, NotVisible)
                           else "unavailable", "candidate_count": 0, "complete": False,
                           "ambiguous": True, "status": "none_found_incomplete"})
            return report
        company_ids = [c["id"] for c in listing["items"] if isinstance(c.get("id"), str)]
        report["companies_completeness"] = listing["completeness"]
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    incomplete = bool(report.get("companies_completeness", {}).get("truncated"))
    for company_id in company_ids:
        for entity_set, field, relation, caveat_keys in lookups:
            search: dict[str, Any] = {"company_id": str(company_id).lower(),
                                      "entity_set": entity_set, "field": field,
                                      "relation": relation}
            report["searches"].append(search)
            try:
                found = reader.find_by_number(actor, company_id, entity_set, number,
                                              field=field, max_matches=max_matches)
            except NotVisible:
                search["status"] = "not_permitted"
                incomplete = True
                continue
            except SourceUnavailable as exc:
                search["status"] = "unavailable"
                search["error"] = str(exc)
                incomplete = True
                continue
            search.update({"status": found["status"], "match_count": found["match_count"],
                           "completeness": found["completeness"]})
            if found["status"] in ("truncated", "partially_withheld"):
                incomplete = True
            for match in found["matches"]:
                record = match["record"]
                key = (record["company_id"], record["entity_set"], record["id"])
                keys = list(caveat_keys) + ([] if match["case_exact"]
                                            else ["case_insensitive_match"])
                matched = {"relation": relation, "field": field,
                           "value": record["fields"].get(field),
                           "case_exact": match["case_exact"],
                           "caveats": [CAVEATS[k] for k in keys]}
                if key in seen:
                    seen[key]["matched_by"].append(matched)
                    continue
                candidate = {"citation": record["citation"],
                             "company_id": record["company_id"],
                             "entity_set": record["entity_set"], "id": record["id"],
                             "etag": record["etag"], "content_sha256": record["content_sha256"],
                             "summary": {k: record["fields"][k] for k in SUMMARY_FIELDS
                                         if k in record["fields"]},
                             "evidence_role": "lead", "identity_asserted": False,
                             "matched_by": [matched]}
                seen[key] = candidate
                report["candidates"].append(candidate)
    report["candidate_count"] = len(report["candidates"])
    report["complete"] = not incomplete
    report["ambiguous"] = len(report["candidates"]) > 1 or incomplete
    if not report["candidates"]:
        report["status"] = "none_found" if not incomplete else "none_found_incomplete"
    elif len(report["candidates"]) == 1 and not incomplete:
        report["status"] = "single_candidate"
    else:
        report["status"] = "ambiguous"
    return report


__all__ = ["CAVEATS", "LOOKUPS", "SCHEMA", "bc_leads_for_number"]
