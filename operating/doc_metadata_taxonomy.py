"""The Portal's document-metadata taxonomy, mirrored exactly (read-only data).

Source of truth is the Portal repository ``petralabx/plx-customer-portal``
(branch ``staging``), first read at ``8da33e4`` and re-read since (last at
``45673ee``, TASK-2594, and per file at ``19f1d94``). Nothing here is invented: each
table and rule below copies one upstream definition, cited by ``path@commit``.
When the Portal changes, update this file from the upstream code; never fork
the column set (requirements ledger QA-06).

The Workbench only *reads* this taxonomy to phrase proposals. It never writes
SharePoint columns: the SharePoint tag writer was deleted on purpose and every
Petra system is read-only (hard rule).
"""

from __future__ import annotations

from typing import Any, Mapping


PORTAL_COMMIT = "8da33e4"
PORTAL_REPO = "petralabx/plx-customer-portal"


def cite(path: str, line: int | None = None) -> str:
    return f"{path}@{PORTAL_COMMIT}" + (f":{line}" if line else "")


# --- SharePoint list columns ------------------------------------------------
# scripts/document-metadata/columns.mjs@8e3a951 (LEGACY_COLUMNS, TARGET_COLUMNS).
# Choice columns are provisioned with allowTextEntry=true, so SharePoint itself
# accepts other text; a Workbench proposal is still restricted to the listed
# choices. CustomerVisible (boolean, TASK-2214) mirrors the Portal's
# app_document.customer_visible flag, which stays the source of truth
# (portal/src/lib/services/document-governance.ts@8e3a951
# setDocumentCustomerVisible); the Workbench reads it and never proposes it.
COLUMNS_SOURCE = "scripts/document-metadata/columns.mjs@8e3a951"
COLUMNS: tuple[dict[str, Any], ...] = (
    {"name": "DocumentType", "display_name": "Document Type", "kind": "text", "group": "legacy"},
    {"name": "ReviewStatus", "display_name": "Review Status", "kind": "text", "group": "legacy"},
    {"name": "ExpiryDate", "display_name": "Expiry Date", "kind": "dateTime", "group": "legacy"},
    {"name": "CustomerRef", "display_name": "Customer Ref", "kind": "text", "group": "legacy"},
    {"name": "ProjectRef", "display_name": "Project Ref", "kind": "text", "group": "legacy"},
    {"name": "RetentionCategory", "display_name": "Retention Category", "kind": "text",
     "group": "legacy"},
    {"name": "PartyType", "display_name": "Party Type", "kind": "choice",
     "choices": ("Customer", "Supplier", "Internal"), "group": "target"},
    {"name": "PartyRef", "display_name": "Party Ref", "kind": "text", "group": "target"},
    {"name": "DocumentCategory", "display_name": "Document Category", "kind": "text",
     "group": "target"},
    {"name": "ItemRef", "display_name": "Item Ref", "kind": "text", "group": "target"},
    {"name": "DocStatus", "display_name": "Doc Status", "kind": "choice",
     "choices": ("Draft", "Pending Review", "Approved", "Expired"), "group": "target"},
    {"name": "Source", "display_name": "Source", "kind": "choice",
     "choices": ("Customer", "Staff", "Vendor", "System"), "group": "target"},
    {"name": "Confidentiality", "display_name": "Confidentiality", "kind": "choice",
     "choices": ("Public", "Internal", "Customer Confidential"), "group": "target"},
    {"name": "CustomerVisible", "display_name": "Customer Visible", "kind": "boolean",
     "group": "target"},
    # plx-document-schema.ts@19f1d94:86-87 (2167c63 TASK-2222): the finished-product code and
    # the customer PO number (SalesOrder.poReference) the Portal walks from its record links
    # (lib/record-links/file-refs.ts) and writes on every stored file and issued record;
    # left empty when the links are ambiguous, never cleared by a later backfill.
    {"name": "ProductRef", "display_name": "Product Ref", "kind": "text", "group": "plx"},
    {"name": "OrderRef", "display_name": "Order Ref", "kind": "text", "group": "plx"},
)
# Columns the Portal sets from its own record; a document reading never proposes them.
# The record-link refs are captured and stated (stated_metadata, REF_COLUMNS) but are no row
# of a proposal (operating.doc_metadata).
RECORD_LINK_COLUMNS = frozenset({"ProductRef", "OrderRef"})
PORTAL_RECORD_COLUMNS = frozenset({"CustomerVisible"}) | RECORD_LINK_COLUMNS
COLUMN_BY_NAME = {column["name"]: column for column in COLUMNS}

# --- PLX document schema and retention (TASK-2215) -----------------------------
# portal/src/lib/sharepoint/plx-document-schema.ts@45673ee:23-56: the closed choice lists of
# the PLX site columns, which every Portal metadata write now uses (DocumentCategory is a
# choice, DocStatus gains Superseded, RetentionCategory has human-readable values). The
# provisioner is dry-run by default, so a library may still hold the columns.mjs set above,
# whose text columns accept these values too; legacy 7_YEAR style retention values stay valid
# (document-governance.ts@45673ee:65-66).
PLX_SCHEMA_SOURCE = "portal/src/lib/sharepoint/plx-document-schema.ts@45673ee:23-56"
DOCUMENT_CATEGORY_CHOICES = ("COA", "SDS", "SPEC_SHEET", "ARTWORK", "LABEL_PROOF", "AGREEMENT",
                             "BATCH_RECORD", "QUOTE", "COMMERCIAL_INVOICE", "PROJECT_BRIEF",
                             "SUPPLIER_QUESTIONNAIRE", "TECH_TRANSFER", "OTHER")
DOC_STATUS_CHOICES = ("Draft", "Pending Review", "Approved", "Expired", "Superseded")
RETENTION_CATEGORY_CHOICES = ("Permanent", "7 years", "3 years", "1 year")
# --- Document types: one table per category (TASK-2594) -----------------------------
# portal/src/lib/documents/document-types.ts@1d3eaf2:31-162 (DOCUMENT_TYPES,
# isDocumentCategory, documentCategoryColumn): one row per category the Portal files a document
# under, with the TYPE code of the stored name (file-naming.ts), the RetentionCategory
# (retention-policy.ts@45673ee:13-15 retentionForCategory reads the row) and the short
# DocumentCategory choice written to SharePoint (OTHER when none fits). The fine category stays
# inside the Portal (routing, name, retention); the column keeps the 13 choices above.
DOCUMENT_TYPES_SOURCE = "portal/src/lib/documents/document-types.ts@1d3eaf2:31-162"


def _rows(retention: str, **rows: tuple[str, str]) -> dict[str, dict[str, str]]:
    return {category: {"code": code, "retention": retention, "column": column}
            for category, (code, column) in rows.items()}


_PERMANENT, _GMP, _PROJECT = "Permanent", "7 years", "3 years"
DOCUMENT_TYPES: dict[str, dict[str, str]] = {
    # Product Development
    **_rows(_PROJECT, PROJECT_BRIEF=("BRIEF", "PROJECT_BRIEF"),
            TECH_TRANSFER=("TT", "TECH_TRANSFER"), BRAND_GUIDELINES=("BRAND", "PROJECT_BRIEF"),
            # a pinned Outlook email on a project (document-types.ts@b4118a3:41, TASK-3207)
            OUTLOOK_PIN=("EML", "OTHER")),
    **_rows(_GMP, REGULATORY_REQ=("REG", "PROJECT_BRIEF"), ARTWORK=("ART", "ARTWORK"),
            ARTWORK_REFERENCE=("ARTREF", "ARTWORK"),
            # artwork sample photos (2e6984f TASK-2788), templates (162b108 TASK-2787)
            ARTWORK_SAMPLE=("ARTSMP", "ARTWORK"), ARTWORK_TEMPLATE=("ARTTPL", "ARTWORK"),
            LABEL_PROOF=("LBL", "LABEL_PROOF"),
            DIELINE=("DWG", "ARTWORK"), DRAWING=("DWG", "ARTWORK"),
            TECHNICAL_DRAWING=("DWG", "ARTWORK"),
            # internal reference tree, kept 7 years (bc144cd TASK-3105, document-types.ts@f3e3a53:45-46)
            REFERENCE=("REF", "ARTWORK"),
            PALLET_PATTERN=("PALLET", "OTHER"),
            FORMULA_APPROVAL=("FAPP", "OTHER"), CUSTOMER_APPROVAL=("CAPP", "OTHER"),
            SAMPLE_REPORT=("SAMPLE", "OTHER"),
            # Quality: materials and specs
            COA=("COA", "COA"), COC=("COC", "COA"), SDS=("SDS", "SDS"),
            TDS=("TDS", "SPEC_SHEET"), SPEC_SHEET=("SPEC", "SPEC_SHEET"),
            COMPONENT_SPEC=("SPEC", "SPEC_SHEET"), FG_SPEC=("SPEC", "SPEC_SHEET"),
            CUSTOMER_SPEC=("SPEC", "SPEC_SHEET"), APPROVED_SPEC=("SPEC", "SPEC_SHEET"),
            METHOD_VALIDATION=("MVAL", "OTHER"), ALLERGEN=("ALG", "OTHER"),
            REGULATORY_CERT=("CERT", "OTHER"),
            # requirement evidence (5d3a5e8 TASK-2684/2685)
            CERTIFICATE=("CERT", "OTHER"), SUPPLIER_STATEMENT=("STMT", "OTHER"),
            IFRA=("IFRA", "OTHER"), FOOD_CONTACT=("FCM", "OTHER"), PCR_CONTENT=("PCR", "OTHER"),
            PACKAGING_CERTIFICATE=("CERT", "OTHER"),
            # Quality: formulas (INGREDIENT_LIST: 47e489d TASK-2745)
            FORMULA_SHEET=("FORMULA", "OTHER"), INGREDIENT_LIST=("IL", "OTHER"),
            STABILITY=("STAB", "OTHER"),
            HRIPT=("HRIPT", "OTHER"), FREEZE_THAW=("FT", "OTHER"), PET=("PET", "OTHER"),
            PACK_COMPAT=("PCOMP", "OTHER"), PROCEDURE=("PROC", "OTHER"),
            PROTOCOL=("PROT", "OTHER"), PULL_PHOTO=("PHOTO", "OTHER"),
            # Quality: batches and tests
            ISSUED_COA=("COA", "COA"), QC_RESULT=("QC", "OTHER"), IN_PROCESS_QC=("QC", "OTHER"),
            LAB_RESULT=("LAB", "OTHER"), LAB_REPORT=("LAB", "OTHER"),
            # WAIVER: e2728afe6 TASK-1929 (document-types.ts@1d3eaf2:93)
            WAIVER=("WVR", "OTHER"),
            # Quality: events
            EVIDENCE=("EVID", "OTHER"), PHOTO=("PHOTO", "OTHER"), CONTAINMENT=("CONT", "OTHER"),
            TRAINING_RECORD=("TRN", "OTHER"), REVISED_SOP=("SOP", "OTHER"),
            VERIFICATION=("VERIF", "OTHER"), EQMS_EVIDENCE=("EQMS", "OTHER"),
            EQMS_AFTER_CHANGE=("EQMS", "OTHER"),
            # Quality: suppliers
            QUALIFICATION=("QUAL", "SUPPLIER_QUESTIONNAIRE"),
            SUPPLIER_ASSESSMENT=("QUAL", "SUPPLIER_QUESTIONNAIRE"),
            SUPPLIER_QUESTIONNAIRE=("QUAL", "SUPPLIER_QUESTIONNAIRE"),
            GMP_CERT=("GMP", "OTHER"), ISO_CERT=("ISO", "OTHER"),
            # Batches and assembly orders
            BATCH_RECORD=("BR", "BATCH_RECORD"), ESIGN=("ESIG", "BATCH_RECORD"),
            FILL_RECORD=("FILL", "BATCH_RECORD"), QC_SIGNOFF=("QCS", "OTHER"),
            DAMAGE_PHOTO=("PHOTO", "OTHER"),
            # Maintenance
            CALIBRATION_CERT=("CAL", "OTHER"), PM_CHECKLIST=("PM", "OTHER"),
            NAMEPLATE_PHOTO=("PHOTO", "OTHER"), OEM_MANUAL=("MAN", "OTHER")),
    # Supply Chain: suppliers and POs
    **_rows(_PERMANENT, SIGNED_PO=("PO", "OTHER"), ISSUED_PO=("PO", "OTHER"),
            ORDER_CONFIRMATION=("OC", "OTHER"), CUSTOMS=("CUSTOMS", "OTHER"),
            ISSUED_CUSTOMS=("CUSTOMS", "OTHER"), PACKAGING_QUOTE=("SQUOTE", "QUOTE"),
            AGREEMENT=("AGR", "AGREEMENT"), INSURANCE_COI=("COI", "OTHER")),
    **_rows(_PROJECT, PRODUCT_IMAGE=("IMG", "OTHER")),
    # Supply Chain: shipping
    **_rows(_GMP, SHIPPING_DOC=("SHIP", "OTHER"), BOL=("BOL", "OTHER"),
            PACKING_LIST=("PL", "OTHER"), ISSUED_PACKING_LIST=("PL", "OTHER"),
            COMMERCIAL_INVOICE=("CINV", "COMMERCIAL_INVOICE"),
            ISSUED_COMMERCIAL_INVOICE=("CINV", "COMMERCIAL_INVOICE"), POD=("POD", "OTHER"),
            CUSTOMER_EMAIL=("EML", "OTHER"), PICKUP_SHEET=("PICKUP", "OTHER"),
            CASE_LABELS=("CASELBL", "OTHER")),
    # Sales; Finance
    **_rows(_PERMANENT, CUSTOMER_PO=("CPO", "OTHER"), QUOTE=("CQUOTE", "QUOTE"),
            ISSUED_SALES_ORDER=("SO", "OTHER"), BANK_REFERENCE=("BANK", "OTHER"),
            TAX_FORM=("TAX", "OTHER"), BUSINESS_REGISTRATION=("BIZREG", "OTHER"),
            FINANCIAL_STMT=("FIN", "OTHER"), W9_TAX=("W9", "OTHER")),
    # No category, or one with no route: Inbox until filed.
    **_rows(_PROJECT, OTHER=("DOC", "OTHER")),
}


def _document_type(category: Any) -> dict[str, str]:
    """The category's row; an unknown or missing category is OTHER (isDocumentCategory)."""
    return DOCUMENT_TYPES[category if isinstance(category, str) and category in DOCUMENT_TYPES
                          else "OTHER"]


def retention_for_category(category: Any) -> str:
    """retention-policy.ts retentionForCategory: the row's retention."""
    return _document_type(category)["retention"]


def document_category_column(category: Any) -> str:
    """document-types.ts documentCategoryColumn: the short DocumentCategory choice written to
    SharePoint for a category."""
    return _document_type(category)["column"]

# portal/src/lib/services/document-governance.ts@8da33e4 (type unions written
# by setDocumentMetadata into the legacy text columns).
SP_DOCUMENT_TYPES = ("NDA", "CREDIT_APP", "FORMULATION", "MSDS", "COA", "BATCH_RECORD",
                     "DEVIATION", "CONTRACT", "PROJECT_BRIEF", "OTHER")
SP_REVIEW_STATUSES = ("DRAFT", "PENDING_REVIEW", "APPROVED", "REJECTED", "EXPIRED")
SP_RETENTION_CATEGORIES = ("PERMANENT", "7_YEAR", "3_YEAR", "1_YEAR")

# --- Prisma DocumentType and the tag/storage maps ------------------------------
# portal/prisma/schema.prisma@8da33e4:700 enum DocumentType.
PRISMA_DOCUMENT_TYPES = ("SOP", "FORM", "BATCH_RECORD", "COA", "MSDS", "SPECIFICATION",
                         "ARTWORK", "LABEL", "CONTRACT", "NDA", "QUOTE", "INVOICE", "OTHER")
# portal/src/lib/documents/tag-taxonomy.ts@8da33e4 ALIAS_TO_DOCTYPE / DOCTYPE_TO_SP.
ALIAS_TO_DOCTYPE = {
    "COA": "COA", "MSDS": "MSDS", "SDS": "MSDS", "SPECIFICATION": "SPECIFICATION",
    "SPEC_SHEET": "SPECIFICATION", "ARTWORK": "ARTWORK", "LABEL": "LABEL",
    "CONTRACT": "CONTRACT", "NDA": "NDA", "BATCH_RECORD": "BATCH_RECORD", "SOP": "SOP",
    "FORM": "FORM", "QUOTE": "QUOTE", "INVOICE": "INVOICE", "OTHER": "OTHER",
    "FORMULATION": "OTHER", "PRODUCT_BRIEF": "OTHER", "SUPPLIER_QUESTIONNAIRE": "FORM",
    "SUPPLIER_ASSESSMENT": "FORM", "CONTRACTOR_QUESTIONNAIRE": "FORM",
}
DOCTYPE_TO_SP = {
    "COA": "COA", "MSDS": "MSDS", "SPECIFICATION": "OTHER", "ARTWORK": "OTHER",
    "LABEL": "OTHER", "CONTRACT": "CONTRACT", "NDA": "NDA", "BATCH_RECORD": "BATCH_RECORD",
    "SOP": "OTHER", "FORM": "OTHER", "QUOTE": "OTHER", "INVOICE": "OTHER", "OTHER": "OTHER",
}
# portal/src/lib/documents/storage-category.ts@45673ee:9-33 DOCTYPE_TO_STORAGE (unchanged since
# 8da33e4).
DOCTYPE_TO_STORAGE = {
    "COA": "COA", "MSDS": "SDS", "SDS": "SDS", "SPECIFICATION": "SPEC_SHEET",
    "SPEC_SHEET": "SPEC_SHEET", "ARTWORK": "ARTWORK", "LABEL": "LABEL_PROOF",
    "CONTRACT": "AGREEMENT", "NDA": "AGREEMENT", "BATCH_RECORD": "BATCH_RECORD",
    "SOP": "OTHER", "FORM": "OTHER", "QUOTE": "QUOTE", "INVOICE": "COMMERCIAL_INVOICE",
    "OTHER": "OTHER", "FORMULATION": "OTHER", "PRODUCT_BRIEF": "PROJECT_BRIEF",
    "SUPPLIER_QUESTIONNAIRE": "SUPPLIER_QUESTIONNAIRE",
    "SUPPLIER_ASSESSMENT": "SUPPLIER_QUESTIONNAIRE",
    "CONTRACTOR_QUESTIONNAIRE": "SUPPLIER_QUESTIONNAIRE",
    "TECH_TRANSFER": "TECH_TRANSFER", "PROJECT_BRIEF": "PROJECT_BRIEF",
}


def _key(raw: Any) -> str:
    return "_".join(raw.strip().upper().split()) if isinstance(raw, str) else ""


def resolve_doc_type(raw: Any) -> str:
    """tag-taxonomy.ts resolveDocType: unknown values become OTHER."""
    key = _key(raw)
    if not key or key in {"PARTY:CUSTOMER", "PARTY:VENDOR"}:
        return "OTHER"
    if key in ALIAS_TO_DOCTYPE:
        return ALIAS_TO_DOCTYPE[key]
    return key if key in set(ALIAS_TO_DOCTYPE.values()) else "OTHER"


def sharepoint_document_type(doc_type: str) -> str:
    """tag-taxonomy.ts sharePointDocumentType."""
    return DOCTYPE_TO_SP.get(doc_type, "OTHER")


def resolve_storage_category(document_type: Any) -> str:
    """storage-category.ts resolveStorageCategory."""
    key = _key(document_type)
    return DOCTYPE_TO_STORAGE.get(key, "OTHER") if key else "OTHER"


def resolve_document_category(category: Any) -> str:
    """storage-category.ts@45673ee:51-55 resolveDocumentCategory (TASK-2215, TASK-2594): a
    category with its own DOCUMENT_TYPES row is kept, anything else goes through
    DOCTYPE_TO_STORAGE, an unknown value is OTHER. The column gets
    document_category_column of the result, never the result itself."""
    key = _key(category)
    return key if key in DOCUMENT_TYPES else resolve_storage_category(category)


# --- SharePoint content types (inbound mapping only) --------------------------
# portal/src/lib/sharepoint/sharepoint-metadata.ts@8da33e4 CONTENT_TYPES. The
# Portal maps these when Power Automate pushes metadata back; no upload path
# assigns a content type (SPEC §2.3: one content type + one column set).
CONTENT_TYPES_SOURCE = cite("portal/src/lib/sharepoint/sharepoint-metadata.ts")
CONTENT_TYPES = {
    "PLX Customer Document": "GENERAL", "PLX CoA": "COA", "PLX Formula": "FORMULA",
    "PLX Production Order": "PRODUCTION_ORDER", "PLX Sample Request": "SAMPLE_REQUEST",
    "PLX Evaluation Report": "EVALUATION_REPORT", "PLX Approval Form": "APPROVAL_FORM",
    "PLX Raw Material Spec": "RAW_MATERIAL_SPEC", "PLX Project Document": "PROJECT_DOCUMENT",
}

# --- Storage categories -> party / phase -----------------------------------------
# portal/src/lib/storage/folder-taxonomy.ts@19f1d94:61-165 CATEGORY_ROUTES (root C =
# Customer, S/P = Supplier, I = Internal). Since e8a89d5 (TASK-1379) ARTWORK files on
# LABEL_PROOF's customer -> project tree, with ARTWORK_SAMPLE (TASK-2788) and
# ARTWORK_TEMPLATE (TASK-2787); ARTWORK_REFERENCE stays Internal. Files stored before
# never move, so an older ARTWORK file sits on the Internal tree. The TASK-2684 requirement
# evidence categories are Internal Quality.
CATEGORY_ROUTES_SOURCE = "portal/src/lib/storage/folder-taxonomy.ts@19f1d94:61-165"
_ROUTES = {
    "C": {"ARTWORK", "ARTWORK_SAMPLE", "ARTWORK_TEMPLATE", "PROJECT_BRIEF", "TECH_TRANSFER",
          "FORMULA_APPROVAL", "PROCEDURE", "SAMPLE_REPORT",
          "CUSTOMER_APPROVAL", "LABEL_PROOF", "BRAND_GUIDELINES", "FG_SPEC", "CUSTOMER_SPEC",
          "REGULATORY_REQ", "COA", "CUSTOMER_PO", "SHIPPING_DOC", "BOL", "PACKING_LIST",
          "COMMERCIAL_INVOICE", "POD", "BANK_REFERENCE", "TAX_FORM",
          "BUSINESS_REGISTRATION", "FINANCIAL_STMT", "OTHER", "TECHNICAL_DRAWING"},
    "S": {"SUPPLIER_QUESTIONNAIRE", "SUPPLIER_ASSESSMENT", "QUALIFICATION", "W9_TAX",
          "INSURANCE_COI", "GMP_CERT", "ISO_CERT", "AGREEMENT",
          "SIGNED_PO", "QUOTE", "ORDER_CONFIRMATION", "CUSTOMS"},
    "I": {"SDS", "TDS", "SPEC_SHEET", "ALLERGEN", "REGULATORY_CERT", "COC", "COMPONENT_SPEC",
          "STABILITY", "CERTIFICATE", "SUPPLIER_STATEMENT", "IFRA", "FOOD_CONTACT",
          "PCR_CONTENT", "PACKAGING_CERTIFICATE", "HRIPT", "FREEZE_THAW", "PET", "PACK_COMPAT",
          "EVIDENCE", "PHOTO",
          "LAB_REPORT", "CONTAINMENT", "TRAINING_RECORD", "REVISED_SOP", "VERIFICATION",
          "PROTOCOL", "PULL_PHOTO", "APPROVED_SPEC", "METHOD_VALIDATION", "QC_RESULT",
          "LAB_RESULT", "IN_PROCESS_QC", "ARTWORK_REFERENCE", "DIELINE",
          "DRAWING", "REFERENCE", "BATCH_RECORD", "ESIGN", "FILL_RECORD", "DAMAGE_PHOTO",
          "QC_SIGNOFF", "CALIBRATION_CERT", "PM_CHECKLIST", "OEM_MANUAL", "NAMEPLATE_PHOTO"},
}
STORAGE_CATEGORIES = frozenset().union(*_ROUTES.values())

# --- Issued records (TASK-2218) ----------------------------------------------------
# portal/src/lib/storage/issued-record-categories.ts@4af6957:25-32 (ISSUED_RECORD_CATEGORIES,
# labels :39-46, code-default roots :61-68). The Portal files the exact bytes of every PDF
# it issues (COA, PO, customs copy, sales order, packing slip, commercial invoice) under
# these categories, a new SharePoint object per render; its finalize job writes them into
# DocumentCategory (bd5b343). They are Portal-only values: no upload page offers them
# (ENTITY_CONTEXTS), so a Workbench proposal never uses one, and they are not in
# CATEGORY_ROUTES, so partyTypeForCategory (party_type_for_category) answers Customer for
# them as it does upstream; the issued party is the record's root below (P = supplier
# _Procurement, C = customer, I = internal).
ISSUED_RECORD_CATEGORIES: dict[str, dict[str, str]] = {
    "ISSUED_COA": {"label": "Issued COA", "root": "I"},
    "ISSUED_PO": {"label": "Issued purchase order", "root": "P"},
    "ISSUED_CUSTOMS": {"label": "Issued purchase order (customs copy)", "root": "P"},
    "ISSUED_SALES_ORDER": {"label": "Issued sales order", "root": "C"},
    "ISSUED_PACKING_LIST": {"label": "Issued packing slip", "root": "C"},
    "ISSUED_COMMERCIAL_INVOICE": {"label": "Issued commercial invoice", "root": "C"},
}


def party_type_for_category(category: str) -> str:
    """folder-taxonomy.ts partyTypeForCategory (unknown categories route as OTHER)."""
    if category in _ROUTES["S"]:
        return "Supplier"
    if category in _ROUTES["I"]:
        return "Internal"
    return "Customer"


# --- Entity attachments: the upload-time decision rule ---------------------------
# portal/src/lib/sharepoint/attachment-metadata.ts@45673ee:37-126 (SUPPLIER_ENTITIES,
# CUSTOMER_ENTITIES, confidentialityForParty, partyTypeForEntity, buildAttachmentMetadata,
# buildCompleteAttachmentMetadata), called from
# portal/src/app/api/attachments/route.ts@45673ee:507 with the stored entityType (the
# page's, canonicalised by canonical_entity_type since TASK-2399), the category the user
# picked, and the folder tokens. Since TASK-2215 (e6dc559) the generic upload doors write the
# complete payload. Since TASK-2594 (e677b49) the picked category resolves to its
# DOCUMENT_TYPES row (resolve_document_category); DocumentCategory is that row's short column
# choice and RetentionCategory that row's period, and Confidentiality comes from the party
# (a category with no row is OTHER: column OTHER, 3 years). ExpiryDate
# has no source and CustomerVisible is not sent. Since TASK-2250
# the result (through documentMetadataFields, document-governance.ts@45673ee:117-134) is
# stored on a durable finalize job
# and written to SharePoint after the upload answers, so the values are the same and
# only arrive later. Since TASK-2212
# (route.ts@6e76a5b:150-159, lib/storage/attachment-location.ts@71f7214:140-183) the tokens
# come from the record when its PLX library has an active LIBRARY_* row; otherwise, since
# TASK-2835 (2e36901, legacyLocation :104-122), from the record's own names when it loads,
# else the page's hints, a hint equal to the record id dropped. The rule below takes them
# as given either way.
# Since TASK-2222 (2167c63) the payload also carries ProductRef/OrderRef from the record links
# (route.ts@19f1d94:504,533-534; issued-records.ts@19f1d94:315,361), which this rule does
# not derive: it reads no record link, so a proposal never states them. Re-read at 1d3eaf2
# (e2728afe6 TASK-1929): the column choice is unchanged (fileRefFieldsForEntity :593, payload
# :629-630); the new QC-document upload gate (:273) and optional sourceNote (:510) file no
# column this rule writes.
ATTACHMENT_RULE_SOURCE = "portal/src/lib/sharepoint/attachment-metadata.ts@45673ee:37-126"
SUPPLIER_ENTITIES = frozenset({"SUPPLIER", "PURCHASE_ORDER", "PO", "RFQ", "RECEIPT"})
# SHIP_RELEASE: attachment-metadata.ts@8f9009d:39 (TASK-2399).
CUSTOMER_ENTITIES = frozenset({"CUSTOMER", "RD_CUSTOMER", "RD_PROJECT", "PROJECT",
                               "SALES_ORDER", "ORDER", "OE", "QUOTE", "SHIP_RELEASE"})

# --- Entity routes: library and folder per (entity type, category) ---------------
# portal/src/lib/storage/entity-routing.ts@19f1d94 (TASK-2212, TASK-2399, TASK-2506
# FORMULA_SHEET, TASK-2488 the sourcing request's RAW documents, TASK-2685 requirement
# evidence, TASK-2745 INGREDIENT_LIST, TASK-2788 ARTWORK_VERSION, TASK-2787
# ARTWORK_COMPONENT), table for table: FOLDERS (:40-57), ENTITY_ROUTES (:111-295);
# re-read at f3e3a53 (FOLDERS :42-59, ENTITY_ROUTES :114-300): only CREDIT_APPLICATION
# OTHER is new (66d08fb TASK-3075); re-read at b4118a3 (ENTITY_ROUTES :114-302): only
# RD_PROJECT OUTLOOK_PIN is new (:236-237, TASK-3207); re-read at 1d3eaf2 (ENTITY_ROUTES
# :114-305): only BATCH WAIVER is new (:177, e2728afe6 TASK-1929).
# Read as data only (the release categories, FINANCE_CATEGORIES): nothing here files a
# document, so routeForEntity (@1d3eaf2:308-310; any pair not listed goes to Inbox
# "Unfiled"), the blank-folder rule of resolveAttachmentLocation
# (lib/storage/attachment-location.ts@71f7214:97-102,161-182), its RD_PROJECT OUTLOOK_PIN
# fallback (@b4118a3 isProjectOutlookPin/legacyOutlookPinLocation: the OUTLOOK_PIN row, else
# the PROJECT_BRIEF folder + Communications/Outlook/<year>) and its 422 refusal of a path over
# the 400-character SharePoint cap (locations.ts@b4118a3 StoragePathTooLongError, TASK-3197)
# are not mirrored. Ship
# releases (portal/src/lib/attachments/ship-release.ts@8f9009d) are stored under the
# canonical entity type (route.ts@8f9009d:86,161).
SHIP_RELEASE_ENTITY_TYPE = "SHIP_RELEASE"
_SHIP_RELEASE_ALIASES = frozenset({"SHIP_RELEASE", "SHIPRELEASE"})
_FOLDERS = {
    "materials": "Materials/{ref}", "packaging": "Packaging/{ref}",
    "formulas": "Formulas/{ref}", "batches": "Batches/{year}/{ref}",
    "assemblyOrders": "Assembly Orders/{year}/{ref}",
    "qualityEvents": "Quality Events/{year}/{ref}", "equipment": "Equipment/{ref}",
    "supplier": "Suppliers/{supplierFolder}", "supplierRecord": "Suppliers/{supplierFolder}/{ref}",
    "customer": "Customers/{customerFolder}", "customerRecord": "Customers/{customerFolder}/{ref}",
    "customerProject": "Customers/{customerFolder}/{project}", "sourcing": "Sourcing/{ref}",
    "inbox": "Unfiled",
}


def _routes(library: str, folder: str, *categories: str) -> dict[str, tuple[str, str]]:
    return {category: (library, _FOLDERS[folder]) for category in categories}


_EQUIPMENT_ROUTES = _routes("MAINTENANCE", "equipment", "CALIBRATION_CERT", "PM_CHECKLIST",
                            "OEM_MANUAL", "NAMEPLATE_PHOTO", "PHOTO")
_ORDER_ROUTES = {**_routes("SALES", "customerRecord", "CUSTOMER_PO", "QUOTE"),
                 **_routes("SUPPLY_CHAIN", "customerRecord", "SHIPPING_DOC", "BOL",
                           "PACKING_LIST", "COMMERCIAL_INVOICE", "POD")}
_ISSUED_COA = _routes("QUALITY", "batches", "ISSUED_COA")
ENTITY_ROUTES: dict[str, dict[str, tuple[str, str]]] = {
    "RAW_MATERIAL": _routes("QUALITY", "materials", "COA", "COC", "SDS", "TDS", "SPEC_SHEET",
                            "ALLERGEN", "REGULATORY_CERT", "SUPPLIER_QUESTIONNAIRE",
                            "CERTIFICATE", "SUPPLIER_STATEMENT", "IFRA"),
    "PACKAGING": {**_routes("PRODUCT_DEVELOPMENT", "packaging", "ARTWORK", "DIELINE", "DRAWING"),
                  **_routes("QUALITY", "materials", "COMPONENT_SPEC", "SPEC_SHEET", "COC", "COA",
                            "FOOD_CONTACT", "PCR_CONTENT", "PACKAGING_CERTIFICATE")},
    "BULK": {**_routes("QUALITY", "materials", "SPEC_SHEET", "STABILITY"),
             **_routes("PRODUCT_DEVELOPMENT", "customerProject", "CUSTOMER_APPROVAL",
                       "TECH_TRANSFER")},
    "FINAL_PRODUCT": {**_routes("PRODUCT_DEVELOPMENT", "customerProject", "LABEL_PROOF",
                                "ARTWORK"),
                      **_routes("QUALITY", "customerRecord", "FG_SPEC", "COA")},
    "FORMULA": {**_routes("PRODUCT_DEVELOPMENT", "customerProject", "FORMULA_APPROVAL",
                          "SAMPLE_REPORT"),
                **_routes("QUALITY", "formulas", "FORMULA_SHEET", "INGREDIENT_LIST",
                          "STABILITY", "HRIPT",
                          "FREEZE_THAW", "PET", "PACK_COMPAT", "PROCEDURE", "PROTOCOL")},
    "STABILITY_TIMEPOINT": _routes("QUALITY", "formulas", "PULL_PHOTO"),
    "BATCH": {**_routes("PRODUCTION", "batches", "BATCH_RECORD", "ESIGN"),
              **_routes("QUALITY", "batches", "IN_PROCESS_QC", "LAB_RESULT", "QC_RESULT", "COA",
                        "WAIVER"),
              **_ISSUED_COA},
    "BATCH_RECORD": dict(_ISSUED_COA),
    "ASSEMBLY_ORDER": _routes("PRODUCTION", "assemblyOrders", "FILL_RECORD", "DAMAGE_PHOTO",
                              "QC_SIGNOFF"),
    "EQUIPMENT": _EQUIPMENT_ROUTES,
    "MAINTENANCE_WORK_ORDER": _EQUIPMENT_ROUTES,
    "PURCHASE_ORDER": {**_routes("SUPPLY_CHAIN", "supplierRecord", "SIGNED_PO", "QUOTE",
                                 "ORDER_CONFIRMATION", "CUSTOMS", "ISSUED_PO", "ISSUED_CUSTOMS"),
                       **_routes("QUALITY", "supplier", "COA", "COC")},
    "CUSTOMER_ORDER": _ORDER_ROUTES,
    "SALES_ORDER": {**_ORDER_ROUTES,
                    **_routes("SALES", "customerRecord", "ISSUED_SALES_ORDER"),
                    **_routes("SUPPLY_CHAIN", "customerRecord", "ISSUED_PACKING_LIST",
                              "ISSUED_COMMERCIAL_INVOICE")},
    SHIP_RELEASE_ENTITY_TYPE: _routes("SUPPLY_CHAIN", "customerRecord", "CUSTOMER_EMAIL",
                                      "PICKUP_SHEET", "CASE_LABELS"),
    "SUPPLIER": {**_routes("QUALITY", "supplier", "QUALIFICATION", "SUPPLIER_ASSESSMENT",
                           "SUPPLIER_QUESTIONNAIRE", "GMP_CERT", "ISO_CERT"),
                 **_routes("SUPPLY_CHAIN", "supplier", "AGREEMENT", "INSURANCE_COI"),
                 **_routes("FINANCE", "supplier", "W9_TAX")},
    "RD_CUSTOMER": {**_routes("PRODUCT_DEVELOPMENT", "customer", "PROJECT_BRIEF", "TECH_TRANSFER",
                              "BRAND_GUIDELINES", "REGULATORY_REQ"),
                    **_routes("QUALITY", "customer", "CUSTOMER_SPEC")},
    "RD_PROJECT": {**_routes("PRODUCT_DEVELOPMENT", "customerProject", "PROJECT_BRIEF",
                             "TECH_TRANSFER", "ARTWORK", "ARTWORK_REFERENCE",
                             # entity-routing.ts@b4118a3:236-237 (TASK-3207)
                             "OUTLOOK_PIN"),
                   **_routes("SUPPLY_CHAIN", "customerProject", "PACKAGING_QUOTE")},
    "FG_PACK_LABEL": _routes("PRODUCT_DEVELOPMENT", "customerProject", "LABEL_PROOF"),
    # entityId = the LABEL_PROOF attachment id (ship-release.ts@19f1d94 checkAttachmentTarget
    # refuses any other with 404), and <projectKey>:<componentKey>.
    "ARTWORK_VERSION": _routes("PRODUCT_DEVELOPMENT", "customerProject", "ARTWORK_SAMPLE"),
    "ARTWORK_COMPONENT": _routes("PRODUCT_DEVELOPMENT", "customerProject", "ARTWORK_TEMPLATE"),
    "FG_PACK_PALLET": _routes("PRODUCT_DEVELOPMENT", "customerProject", "PALLET_PATTERN"),
    # Every file on a credit application is a Finance file, OTHER included
    # (entity-routing.ts@b4118a3:253-260, 66d08fb TASK-3075).
    "CREDIT_APPLICATION": _routes("FINANCE", "customer", "BANK_REFERENCE", "TAX_FORM",
                                  "BUSINESS_REGISTRATION", "FINANCIAL_STMT", "OTHER"),
    "DEVIATION": _routes("QUALITY", "qualityEvents", "EVIDENCE", "PHOTO", "LAB_REPORT",
                         "CONTAINMENT"),
    "CAPA": _routes("QUALITY", "qualityEvents", "TRAINING_RECORD", "REVISED_SOP",
                    "VERIFICATION", "EVIDENCE"),
    "EqmsRecord": _routes("QUALITY", "qualityEvents", "EQMS_EVIDENCE", "EQMS_AFTER_CHANGE"),
    "SPECIFICATION": _routes("QUALITY", "materials", "APPROVED_SPEC", "CUSTOMER_SPEC",
                             "METHOD_VALIDATION"),
    "QUALITY_TEST": {**_routes("QUALITY", "batches", "QC_RESULT", "LAB_REPORT", "COA"),
                     **_ISSUED_COA},
    "SOURCING_REQUEST": _routes("SUPPLY_CHAIN", "sourcing", "PRODUCT_IMAGE", "SDS", "TDS",
                                "SPEC_SHEET", "COA", "REGULATORY_CERT", "ALLERGEN"),
}
# The release request categories (ship-release.ts@8f9009d:20), read from the one table.
SHIP_RELEASE_CATEGORIES = tuple(ENTITY_ROUTES[SHIP_RELEASE_ENTITY_TYPE])


def canonical_entity_type(entity_type: str) -> str:
    """ship-release.ts canonicalEntityType: ``ShipRelease``, ``ship_release`` and
    ``SHIP_RELEASE`` store as ``SHIP_RELEASE``; every other type is unchanged."""
    return (SHIP_RELEASE_ENTITY_TYPE if entity_type.strip().upper() in _SHIP_RELEASE_ALIASES
            else entity_type)


def party_type_for_entity(entity_type: str) -> str:
    key = entity_type.strip().upper()
    if key in SUPPLIER_ENTITIES:
        return "Supplier"
    if key in CUSTOMER_ENTITIES:
        return "Customer"
    return "Internal"


def confidentiality_for_party(party_type: str) -> str:
    """attachment-metadata.ts confidentialityForParty."""
    return "Customer Confidential" if party_type == "Customer" else "Internal"


def build_attachment_metadata(entity_type: str, category: str,
                              tokens: Mapping[str, str | None]) -> dict[str, str]:
    """buildCompleteAttachmentMetadata (buildAttachmentMetadata plus the DocumentCategory
    column choice and RetentionCategory of the category's DOCUMENT_TYPES row, and
    Confidentiality), field for field (falsy values are not written), on the entity type the
    route stores (canonical_entity_type)."""
    party_type = party_type_for_entity(canonical_entity_type(entity_type))
    customer, customer_code = tokens.get("customer"), tokens.get("customerCode")
    project, ref = tokens.get("project"), tokens.get("ref")
    if party_type == "Supplier":
        party_ref = ref
    elif party_type == "Customer":
        party_ref = customer_code if customer_code is not None else customer
    else:
        party_ref = None
    resolved = resolve_document_category(category)
    meta = {"PartyType": party_type, "DocumentCategory": document_category_column(resolved),
            "DocStatus": "Pending Review", "Source": "Staff",
            "RetentionCategory": retention_for_category(resolved),
            "Confidentiality": confidentiality_for_party(party_type)}
    if party_ref:
        meta["PartyRef"] = party_ref
    if project:
        meta["ProjectRef"] = project
    if ref:
        meta["ItemRef"] = ref
    if party_type == "Customer" and customer:
        meta["CustomerRef"] = customer
    return meta


# The Portal pages that attach documents to the records this module can
# anchor, with the categories each page offers and the tokens it sends.
ENTITY_CONTEXTS: dict[str, dict[str, Any]] = {
    "ASSEMBLY_ORDER": {
        "source": cite("portal/src/app/mrp/assembly/[id]/assembly-detail.tsx", 1445),
        "categories": ("FILL_RECORD", "DAMAGE_PHOTO", "QC_SIGNOFF", "OTHER"),
        # tokens={{ customer: ao.customer_name, ref: displayCode }};
        # displayCode = resolveAODisplayCode(ao) = mo_code
        # (portal/src/app/mrp/assembly/assembly-helpers.ts@8da33e4:24).
        "ref_link": "assembly_order",
    },
    "CUSTOMER_ORDER": {
        "source": cite("portal/src/app/mrp/orders/[id]/order-detail.tsx", 818),
        "categories": ("CUSTOMER_PO", "QUOTE", "SHIPPING_DOC", "OTHER"),
        # tokens={{ customer: order.customer_name, ref: order.erp_code || order.order_number }}
        "ref_link": "sales_order",
    },
    "PURCHASE_ORDER": {
        "source": cite("portal/src/app/mrp/purchase-orders/[id]/purchase-order-detail.tsx", 783),
        "categories": ("SIGNED_PO", "QUOTE", "ORDER_CONFIRMATION", "COA", "CUSTOMS", "OTHER"),
        # tokens={{ ref: po.po_number }}
        "ref_link": "petra_po",
    },
}

# Finished-good lot number for a realized AO is the AO's mo_code, and the bulk
# batch number is silver.batches.display_batch_number (else the MO lot_number):
# portal/src/lib/wms/fp-bridge.ts@8da33e4:1029 and
# portal/src/lib/inventory/assembly-lifecycle.ts@8da33e4:870.
LOT_RULE_SOURCE = (cite("portal/src/lib/wms/fp-bridge.ts", 1029) + "; " +
                   cite("portal/src/lib/inventory/assembly-lifecycle.ts", 870))


# --- Security class: who may read a file (TASK-2249, TASK-2213) -----------------
# The Portal decides a file's readers from its row's securityClass, set by its
# destination (portal/prisma/schema.prisma@790256c:8605 enum SecurityClass), never
# from category text at read time. The Workbench reads SharePoint, not those rows,
# so it resolves the same class from what a captured drive item carries: its
# library (``library_classes``: drive id -> class, deployment configuration), the
# Portal's own Staff Scratch folder and its DocumentCategory column read through
# the routing table that picks the Finance library.
SECURITY_CLASSES = ("STANDARD", "FINANCE", "BUSINESS_CENTRAL", "CUSTOMER_ORIGINAL", "SCRATCH")
# file-clearance.ts@790256c:35-41 RESTRICTED_CLASS_READERS (app_department.code).
RESTRICTED_CLASS_READERS = {"FINANCE": ("FIN", "EXEC"), "BUSINESS_CENTRAL": ("FIN", "EXEC", "IT")}
# Every category ENTITY_ROUTES files into the FINANCE library (entity-routing.ts@549f6f0
# :211,234-237). A DocumentCategory column carries no entity type, so a category any
# entity routes there resolves FINANCE, except OTHER: since 66d08fb (TASK-3075,
# entity-routing.ts@b4118a3:258-259) a credit application's OTHER file routes to Finance,
# but OTHER is every entity's fallback, so it would mark every unclassified file FINANCE.
# The Portal takes the class from the route (securityClassForRoute :314-316), which a
# SharePoint column cannot show; such a file reads FINANCE here only from its library.
FINANCE_CATEGORIES = frozenset(category for routes in ENTITY_ROUTES.values()
                               for category, (library, _) in routes.items()
                               if library == "FINANCE" and category != "OTHER")
# portal/src/lib/storage/locations.ts@790256c:107-112 (unchanged at b4118a3:127-132)
# BENCH_SCRATCH:
# "Staff Scratch/{ref}/{year}", {ref} = the owner's Portal user id.
SCRATCH_FOLDER = "staff scratch"
# A file whose class cannot be decided (its column snapshot belongs to another
# version, or the column is not text): every reader is refused.
UNRESOLVED_CLASS = "UNRESOLVED"
LIST_ITEM_FIELDS_SOURCE = "sharepoint-listitem-fields/v1"
# The PLX site's libraries that are never open to all staff
# (plx-customer-portal docs/projects/plx-sharepoint-site/README.md@b4118a3:86-91):
# Finance, Business Central and Staff Scratch. A library of one of these names
# (whole words, any case: "PLX Finance" too) is captured only once a scope
# classifies it (``integrations.capture_sharepoint``); unclassified it is refused.
RESTRICTED_LIBRARY_NAMES = {"finance": "FINANCE", "business central": "BUSINESS_CENTRAL",
                            "staff scratch": "SCRATCH"}


# lib/attachments/qc-document-categories.ts@1d3eaf2:8-10,27-32 (e2728afe6 TASK-1929): a BATCH
# attachment of one of these categories (case-blind) is a QC document, read only by an admin
# or by an active member of a QC_READER_DEPARTMENTS department (:13; access.ts@1d3eaf2:88-93).
# It files in the QUALITY library under Batches/{year}/{ref} (ENTITY_ROUTES BATCH).
QC_DOCUMENT_CATEGORIES = frozenset({"COA", "WAIVER"})
QC_READER_DEPARTMENTS = ("QUALITY", "QA", "QC")
BATCH_FOLDER = _FOLDERS["batches"].split("/", 1)[0].casefold()
# SharePoint holds the short DocumentCategory column, not the fine category: a WAIVER writes
# OTHER and a COA writes COA (document-types.ts@1d3eaf2:28,93), columns that open Batches
# files (QC_RESULT, LAB_RESULT, ISSUED_COA) share. Open in a Batches folder is therefore
# only a column some category routed there writes and no QC category writes.
OPEN_BATCH_COLUMNS = frozenset(
    document_category_column(category) for routes in ENTITY_ROUTES.values()
    for category, (_, folder) in routes.items() if folder == _FOLDERS["batches"]
) - {document_category_column(category) for category in QC_DOCUMENT_CATEGORIES}


def may_be_qc_document(record: Any) -> bool:
    """Whether a drive-item record may be a Portal QC document: a folder on its path is a
    Batches folder (any depth, any case) and its confirmed DocumentCategory column is not
    one of OPEN_BATCH_COLUMNS (OTHER, COA, any other value, or none captured). A capture
    record names its drive, not its library, so the QUALITY library is not required: this
    only narrows, and refuses an open OTHER or issued-COA Batches file too."""
    path = record.get("path") if isinstance(record, Mapping) else None
    if not isinstance(path, str):
        return True
    folders = [part.casefold() for part in path.split("/") if part][:-1]
    if BATCH_FOLDER not in folders:
        return False
    return confirmed_category(record) not in OPEN_BATCH_COLUMNS


def known_library_class(name: Any) -> str | None:
    """The class a library's display name says it carries, else None."""
    if not isinstance(name, str):
        return None
    words = " " + " ".join("".join(c if c.isalnum() else " " for c in name.casefold())
                           .split()) + " "
    found = {cls for key, cls in RESTRICTED_LIBRARY_NAMES.items() if f" {key} " in words}
    return found.pop() if len(found) == 1 else (UNRESOLVED_CLASS if found else None)


def confirmed_category(record: Any) -> str | None:
    """The DocumentCategory captured for this very version of a drive-item record (its
    ``list_item_fields`` snapshot bound to the version's eTag, as
    :func:`file_security_class` reads it), upper-cased; None when none was captured for
    it or the value is not text."""
    snapshot = record.get("list_item_fields") if isinstance(record, Mapping) else None
    if (not isinstance(snapshot, Mapping) or snapshot.get("source") != LIST_ITEM_FIELDS_SOURCE
            or snapshot.get("drive_etag") != record.get("etag")
            or not isinstance(snapshot.get("fields"), Mapping)):
        return None
    value = snapshot["fields"].get("DocumentCategory")
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


# --- Columns the Portal states on a captured file (QA-06) --------------------------
# A capture scope reads exactly COLUMNS (``CAPTURE_COLUMNS``, never a per-scope list); the
# values stay in the version's eTag-bound ``list_item_fields`` and are projected as
# ``SharePointFile.portal_metadata``: what the Portal states, never a Workbench proposal.
# Graph omits an empty column from listItem.fields, so a requested column that is absent
# is ``empty_or_not_provisioned`` (never proof the column exists and is blank).
CAPTURE_COLUMNS = "portal_taxonomy"
STATED_BY = "stated-by-Portal"
# The columns holding a Portal record's number or code (build_attachment_metadata; ProductRef and
# OrderRef from the record links, TASK-2222): each
# stated value is an identifier mention of its file for the one mention resolver.
REF_COLUMNS = ("PartyRef", "ItemRef", "CustomerRef", "ProjectRef", "ProductRef", "OrderRef")
GAP_NOT_READ = "not_read"
GAP_ABSENT = "empty_or_not_provisioned"
GAP_EMPTY = "empty"
GAP_SHAPE = "unsupported_shape"


def stated_metadata(record: Any) -> dict[str, Any] | None:
    """The Portal-taxonomy columns the Portal states on this very version of a drive-item
    record, cited to its listItem: ``columns`` (name -> value, text trimmed) and ``gaps``
    (name -> ``not_read`` / ``empty_or_not_provisioned`` / ``empty`` /
    ``unsupported_shape``) cover every column of COLUMNS once. None when the record holds
    no column snapshot bound to its own eTag. Fields outside COLUMNS are never served."""
    if not isinstance(record, Mapping):
        return None
    snapshot = record.get("list_item_fields")
    if (not isinstance(snapshot, Mapping) or snapshot.get("source") != LIST_ITEM_FIELDS_SOURCE
            or not isinstance(record.get("etag"), str)
            or snapshot.get("drive_etag") != record.get("etag")
            or not isinstance(snapshot.get("fields"), Mapping)
            or not isinstance(snapshot.get("source_api"), str)):
        return None
    fields = snapshot["fields"]
    requested = snapshot.get("field_names")
    requested = (frozenset(requested) if isinstance(requested, list)
                 and all(isinstance(n, str) for n in requested) else frozenset())
    columns: dict[str, Any] = {}
    gaps: dict[str, str] = {}
    for spec in COLUMNS:
        name = spec["name"]
        if name not in fields:
            gaps[name] = GAP_ABSENT if name in requested else GAP_NOT_READ
            continue
        value = fields[name]
        if spec["kind"] == "boolean":
            if isinstance(value, bool):
                columns[name] = value
            else:
                gaps[name] = GAP_EMPTY if value is None else GAP_SHAPE
        elif value is None or (isinstance(value, str) and not value.strip()):
            gaps[name] = GAP_EMPTY
        elif isinstance(value, str):
            columns[name] = value.strip()
        else:
            gaps[name] = GAP_SHAPE
    return {"stated_by": STATED_BY, "columns": columns, "gaps": gaps,
            "citation": {"source_api": snapshot["source_api"],
                         "list_item_id": snapshot.get("list_item_id"),
                         "drive_etag": snapshot["drive_etag"]},
            "taxonomy": COLUMNS_SOURCE, "written_to_sources": False}


def file_security_class(record: Any, library_classes: Mapping[str, str] | None = None
                        ) -> tuple[str, str | None]:
    """(security class, Staff Scratch owner) of one SharePoint drive-item record.

    ``record`` is the ``graph_read.SharePointReader`` record shape (``drive_id``,
    ``path``, ``etag``), with the item's column snapshot under ``list_item_fields``
    (``sharepoint-listitem-fields/v1``) and its library's class as the capture scope
    classified it under ``library_security_class``, when the capture holds them. The
    library's class is the policy's (``library_classes``) and the capture's together:
    two different classes are UNRESOLVED. A restricted library decides alone; a Staff
    Scratch path (or library) is SCRATCH with the owner folder; else a DocumentCategory
    the Portal files into Finance is FINANCE; else the library's class, else STANDARD
    (the Portal's default). The category rule is stricter than the Portal, which
    decides from the destination alone (file-clearance.ts@790256c:7): it only narrows.
    """
    if not isinstance(record, Mapping):
        return UNRESOLVED_CLASS, None
    drive, path = record.get("drive_id"), record.get("path")
    if not isinstance(drive, str) or not isinstance(path, str):
        return UNRESOLVED_CLASS, None
    stated = {cls for cls in ((library_classes or {}).get(drive),
                              record.get("library_security_class")) if cls is not None}
    if len(stated) > 1 or stated - set(SECURITY_CLASSES):
        return UNRESOLVED_CLASS, None
    library = stated.pop() if stated else "STANDARD"
    if library in RESTRICTED_CLASS_READERS:
        return library, None
    parts = [part for part in path.split("/") if part]
    if library != "SCRATCH" and parts and parts[0].casefold() == SCRATCH_FOLDER:
        library, parts = "SCRATCH", parts[1:]
    if library == "SCRATCH":
        # The owner folder, then at least the file itself.
        return "SCRATCH", parts[0] if len(parts) > 1 else None
    snapshot = record.get("list_item_fields")
    if snapshot is None:
        return library, None
    if (not isinstance(snapshot, Mapping) or snapshot.get("source") != LIST_ITEM_FIELDS_SOURCE
            or not isinstance(snapshot.get("fields"), Mapping)
            or snapshot.get("drive_etag") != record.get("etag")):
        return UNRESOLVED_CLASS, None
    category = snapshot["fields"].get("DocumentCategory")
    if category is None:
        return library, None
    if not isinstance(category, str):
        return UNRESOLVED_CLASS, None
    return ("FINANCE" if _key(category) in FINANCE_CATEGORIES else library), None


__all__ = ["PORTAL_COMMIT", "COLUMNS", "COLUMN_BY_NAME", "CONTENT_TYPES", "ENTITY_CONTEXTS",
           "ISSUED_RECORD_CATEGORIES", "STORAGE_CATEGORIES", "PRISMA_DOCUMENT_TYPES",
           "SP_DOCUMENT_TYPES", "build_attachment_metadata", "party_type_for_entity",
           "party_type_for_category", "resolve_doc_type", "resolve_storage_category",
           "resolve_document_category", "retention_for_category", "confidentiality_for_party",
           "DOCUMENT_TYPES", "document_category_column",
           "DOCUMENT_CATEGORY_CHOICES", "DOC_STATUS_CHOICES", "RETENTION_CATEGORY_CHOICES",
           "sharepoint_document_type", "cite", "FINANCE_CATEGORIES",
           "QC_DOCUMENT_CATEGORIES", "QC_READER_DEPARTMENTS", "RESTRICTED_CLASS_READERS",
           "RESTRICTED_LIBRARY_NAMES", "may_be_qc_document", "OPEN_BATCH_COLUMNS",
           "SECURITY_CLASSES",
           "UNRESOLVED_CLASS", "confirmed_category", "file_security_class",
           "CAPTURE_COLUMNS", "REF_COLUMNS", "STATED_BY", "stated_metadata",
           "known_library_class",
           "ENTITY_ROUTES", "canonical_entity_type", "RECORD_LINK_COLUMNS"]
