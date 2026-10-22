"""How the Portal's SharePoint document-type columns read for a Workbench document kind.

The playbook kinds (``KINDS``), the columns that record a document's type as the Portal
writes them (``KIND_COLUMNS``, each cited in ``PORTAL_SOURCES`` at ``PORTAL_COMMIT``) and
``column_reading``: a recorded value ``establishes`` a kind, is ``consistent`` with some,
carries ``no_information``, names ``another_document`` class or is ``unrecognised``.
Read-only data and pure functions over the Portal taxonomy mirror
(``operating.doc_metadata_taxonomy``): ``operating.playbook_originals`` evaluates originals
with them and ``integrations.graph_read.listitem_probe`` counts live readings, so the
SharePoint adapter reads them without importing the playbook evaluator (which reaches back
into integrations). Stdlib only; no I/O.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import doc_metadata_taxonomy as taxonomy

KINDS = ("purchase_order_pdf", "customer_purchase_order", "proforma_invoice", "freight_quote",
         "shipping_document", "ao_scan", "qc_record")
_ALL = frozenset(KINDS)

# --------------------------------------------------------------------------- Portal mirror
# The SharePoint columns that record a document's type, as the Portal writes them
# (plx-customer-portal@19f1d94; first read at 878705b; 8b08c9e TASK-2503 and, at 19f1d94,
# TASK-2296 and TASK-1364 only moved the order and AO pages' categories). Since 8f9009d
# (TASK-2399) a ship release's request files carry DocumentCategory CUSTOMER_EMAIL /
# PICKUP_SHEET / CASE_LABELS. Since e6dc559 (TASK-2215) the generic /api/attachments doors
# write a short choice of the picked category, since e677b49 (TASK-2594) the column choice of its
# document-types row (doc_metadata_taxonomy.DOCUMENT_TYPES): SIGNED_PO, CUSTOMER_PO,
# CUSTOMER_EMAIL reach the column as OTHER, which names no kind, while FILL_RECORD and ESIGN
# reach it as BATCH_RECORD, COC and ISSUED_COA as COA, ISSUED_COMMERCIAL_INVOICE as
# COMMERCIAL_INVOICE and PACKAGING_QUOTE as QUOTE, so a short choice reads as every category
# that writes it. Files written before keep the picked category, and issued records keep it
# (buildAttachmentMetadata). The categories added since (ARTWORK_SAMPLE, ARTWORK_TEMPLATE,
# the TASK-2684 requirement evidence, INGREDIENT_LIST) write ARTWORK or OTHER: none names a
# kind. Every source below cites PORTAL_COMMIT:
PORTAL_COMMIT = "19f1d94"
PORTAL_SOURCES = {
    "DocumentCategory": [
        # written by setDocumentMetadata from buildAttachmentMetadata's documentCategory =
        # the category the user picked on the entity page, or its canonical choice
        # (buildCompleteAttachmentMetadata, the generic upload doors)
        "portal/src/lib/services/document-governance.ts@19f1d94:133",
        "portal/src/lib/sharepoint/attachment-metadata.ts@19f1d94:79-106",
        "portal/src/lib/sharepoint/attachment-metadata.ts@19f1d94:113-126",
        "portal/src/lib/documents/storage-category.ts@19f1d94:51-55 (resolveDocumentCategory)",
        "portal/src/lib/documents/document-types.ts@19f1d94:31-168 (DOCUMENT_TYPES column)",
        "portal/src/lib/attachments/ship-release.ts@19f1d94:21 (release request categories)",
        "portal/src/lib/storage/folder-taxonomy.ts@19f1d94:61 (CATEGORY_ROUTES)",
        "portal/src/app/mrp/purchase-orders/[id]/purchase-order-detail.tsx@19f1d94:788-795",
        "portal/src/app/mrp/orders/[id]/order-detail.tsx@19f1d94:888-893",
        "portal/src/app/mrp/assembly/[id]/assembly-detail.tsx@19f1d94:1458-1463"],
    "DocumentType": [
        "portal/src/lib/services/document-governance.ts@19f1d94:30 (DocumentType union), :124",
        "portal/src/lib/documents/tag-taxonomy.ts@19f1d94:48 (DOCTYPE_TO_SP: QUOTE and "
        "INVOICE are written as OTHER)"],
    "ContentType": ["portal/src/lib/sharepoint/sharepoint-metadata.ts@19f1d94:11 "
                    "(CONTENT_TYPES); 'Document' is SharePoint's built-in default, not Portal"],
}
KIND_COLUMNS = tuple(PORTAL_SOURCES)
# Values that NAME one kind (a fill source).
_ESTABLISHES: dict[str, dict[str, str]] = {
    "DocumentCategory": {
        "SIGNED_PO": "purchase_order_pdf",          # PO page "Signed PO"
        "CUSTOMER_PO": "customer_purchase_order",   # order page "Customer PO"
        "BOL": "shipping_document", "PACKING_LIST": "shipping_document",
        "COMMERCIAL_INVOICE": "shipping_document",
        "SHIPPING_DOC": "shipping_document",        # order page "Shipping Document"
        # COA is also the column choice of COC and ISSUED_COA (TASK-2594): all quality records
        "COA": "qc_record", "COC": "qc_record", "QC_SIGNOFF": "qc_record",
        "QC_RESULT": "qc_record", "IN_PROCESS_QC": "qc_record",
        # The Portal's own issued renders (doc_metadata_taxonomy.ISSUED_RECORD_CATEGORIES).
        "ISSUED_PO": "purchase_order_pdf", "ISSUED_COA": "qc_record",
        "ISSUED_PACKING_LIST": "shipping_document",
        "ISSUED_COMMERCIAL_INVOICE": "shipping_document"},
    "DocumentType": {"COA": "qc_record"},
    "ContentType": {"PLX CoA": "qc_record"},
}
# Values that name no single kind but are consistent with these (never a fill source). The
# Portal has no category for a proforma invoice, a forwarder's freight quote or an AO scan:
# QUOTE is the PO page's "Quote" (a goods quotation or a forwarder's), ORDER_CONFIRMATION may
# be a vendor PI, FILL_RECORD / BATCH_RECORD / "PLX Production Order" may be an AO scan. Those
# kinds are therefore filled only by a human confirmation. OTHER / the generic content types
# carry no information.
_CONSISTENT: dict[str, dict[str, frozenset[str]]] = {
    "DocumentCategory": {
        "QUOTE": frozenset({"freight_quote", "proforma_invoice"}),
        "ORDER_CONFIRMATION": frozenset({"proforma_invoice"}),
        "CUSTOMS": frozenset({"shipping_document"}),
        "ISSUED_CUSTOMS": frozenset({"purchase_order_pdf", "shipping_document"}),
        "FILL_RECORD": frozenset({"ao_scan"}),
        # the column choice of BATCH_RECORD, ESIGN and FILL_RECORD (TASK-2594)
        "BATCH_RECORD": frozenset({"ao_scan"}),
        "OTHER": _ALL},
    "DocumentType": {
        "OTHER": _ALL,
        "BATCH_RECORD": frozenset({"ao_scan"}),
        "CONTRACT": frozenset({"purchase_order_pdf", "customer_purchase_order"})},
    "ContentType": {
        "PLX Customer Document": _ALL, "Document": _ALL,
        "PLX Production Order": frozenset({"ao_scan"})},
}
# Every value the Portal defines for the column (a defined value outside both tables is
# another document class; anything else is not a Portal value at all).
_DEFINED: dict[str, frozenset[str]] = {
    "DocumentCategory": frozenset(taxonomy.STORAGE_CATEGORIES)
    | frozenset(taxonomy.DOCUMENT_TYPES)
    | frozenset(taxonomy.ISSUED_RECORD_CATEGORIES)
    | frozenset(taxonomy.SHIP_RELEASE_CATEGORIES),
    "DocumentType": frozenset(taxonomy.SP_DOCUMENT_TYPES),
    "ContentType": frozenset(taxonomy.CONTENT_TYPES) | {"Document"},
}


def _folded(value: Any) -> str:
    return "_".join(value.strip().upper().split()) if isinstance(value, str) else ""


def column_reading(column: str, value: Any) -> dict[str, Any]:
    """How one recorded column value reads: ``establishes`` a kind, is ``consistent`` with
    some kinds, carries ``no_information`` (empty), names ``another_document`` class, or is
    ``unrecognised`` (not a value the Portal defines for the column)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"reads_as": "no_information", "kinds": sorted(_ALL)}
    if not isinstance(value, str):
        return {"reads_as": "unrecognised", "kinds": []}
    key = value.strip() if column == "ContentType" else _folded(value)
    if key in _ESTABLISHES.get(column, {}):
        return {"reads_as": "establishes", "kinds": [_ESTABLISHES[column][key]]}
    if key in _CONSISTENT.get(column, {}):
        kinds = _CONSISTENT[column][key]
        return {"reads_as": "no_information" if kinds == _ALL else "consistent",
                "kinds": sorted(kinds)}
    if key in _DEFINED.get(column, frozenset()):
        return {"reads_as": "another_document", "kinds": []}
    return {"reads_as": "unrecognised", "kinds": []}


def recorded_columns(fields: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{column: {value, reads_as, kinds}} of the ``KIND_COLUMNS`` present in ``fields``."""
    out = {}
    for column in KIND_COLUMNS:
        if isinstance(fields, Mapping) and column in fields:
            out[column] = {"value": fields[column], **column_reading(column, fields[column])}
    return out


__all__ = ["KINDS", "KIND_COLUMNS", "PORTAL_COMMIT", "PORTAL_SOURCES", "column_reading",
           "recorded_columns"]
