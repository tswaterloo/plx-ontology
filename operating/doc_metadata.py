"""Portal-taxonomy metadata proposals for one cited source document.

Given a SourceDocument observation (a SharePoint original or an attached
original already cited in the store) this module phrases what the Portal's
own upload rule would write for it, in the Portal's column names and allowed
values (``operating.doc_metadata_taxonomy``), and links the record numbers the
document prints (customer, sales order, customer PO, Petra PO, AO, lot) to
ACL-visible records by exact value.

Rules:

* Every proposed value carries evidence: exact current-text spans, the Portal
  rule it follows (``path@commit``), or both. The basis says whether the value
  is a deterministic rule, a literal labeled field, an OCR or interpreted
  reading (review-grade), or a model suggestion. Models only propose: their
  value never overrides a rule, and a disagreement is reported as a conflict.
* Unknown or conflicting fields are reported as such. Nothing is defaulted
  (not even the Portal's ``OTHER`` category, which is a human's choice). Since
  TASK-2215 the generic upload doors write the canonical choice of the page category
  (``written_as``: since TASK-2594 the short column choice of its category row, ``OTHER`` for
  most page categories) and the RetentionCategory of that row.
* Current SharePoint column values come only from a listItem snapshot read by
  the existing GET-only readers (stored ``source_columns`` or one supplied by
  the caller). Each column is reported ``agree``, ``differs``, ``missing``,
  ``undetermined``, ``current_only``, ``absent`` or ``not_read``. A proposal the current
  revision's columns already state is not emitted (``stated_by_portal``); one that
  differs from them is a ``conflict`` (``differs_from_portal_stated``). A column outside
  the snapshot's requested ``field_names`` is ``not_read``, never ``missing``.
* A person's confirmed value (``operating.kind_confirmation``) is held to the same rule:
  ``confirmed_against_stated`` reports one that differs from the column the Portal states on
  the same version (``SharePointFile.portal_metadata``) as a ``conflict``.
* Nothing is written anywhere. This module performs no I/O.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from integrations.document_understanding import _extract_claims
from ontology.types import EntityRef, ValidationError

from .doc_metadata_taxonomy import (ATTACHMENT_RULE_SOURCE, COLUMN_BY_NAME, COLUMNS,
    COLUMNS_SOURCE, CONTENT_TYPES, CONTENT_TYPES_SOURCE, DOC_STATUS_CHOICES,
    ENTITY_CONTEXTS, LOT_RULE_SOURCE, PORTAL_COMMIT,
    PORTAL_RECORD_COLUMNS, RECORD_LINK_COLUMNS, RETENTION_CATEGORY_CHOICES, STORAGE_CATEGORIES,
    build_attachment_metadata, document_category_column, party_type_for_entity,
    resolve_document_category)


SCHEMA = "plx.portal-document-metadata-proposal/v1"
# The only listItem fields a current-column read keeps: the Portal's column
# internal names (scripts/document-metadata/columns.mjs@8e3a951 and, since TASK-2222,
# ProductRef/OrderRef of plx-document-schema.ts@19f1d94:86-87, written by
# documentMetadataFields, portal/src/lib/services/document-governance.ts@19f1d94:121-140,
# PATCH /drives/{drive}/items/{item}/listItem/fields) plus ContentType, which
# the content-type comparison below reads (sharepoint-metadata.ts CONTENT_TYPES).
CURRENT_COLUMN_FIELDS: tuple[str, ...] = tuple(c["name"] for c in COLUMNS) + ("ContentType",)
# The closed PLX choice lists the Portal writes these columns from since TASK-2215
# (plx-document-schema.ts), where they differ from the columns.mjs set.
PLX_ALLOWED = {"DocStatus": DOC_STATUS_CHOICES, "RetentionCategory": RETENTION_CATEGORY_CHOICES}
# The columns a proposal reports. ProductRef/OrderRef (TASK-2222) are walked from Portal record
# links, which a document reading cannot state or check; their captured values are served as
# SharePointFile.portal_metadata, and a row each would push plx.document_fields past the MCP
# result budget (measured 49082 > 48000 bytes on the AO packet fixture).
PROPOSAL_COLUMNS = tuple(c for c in COLUMNS if c["name"] not in RECORD_LINK_COLUMNS)
OCR_WARNING = "ocr_text_requires_identity_review"
LINKS = ("assembly_order", "sales_order", "customer_po", "petra_po", "lot", "bulk_lot",
         "customer", "vendor")
# Exact attribute matches the caller's ``lookup`` is asked for, per linked record
# (the record's number attribute; the mirror's lot/batch rows).
RECORD_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "assembly_order": (("AssemblyOrder", "mo_code"),),
    "sales_order": (("SalesOrder", "orderNumber"), ("SalesOrder", "erpCode")),
    "petra_po": (("PurchaseOrder", "poNumber"),),
    "lot": (("InventoryLot", "lotNumber"),),
    "bulk_lot": (("BatchRecord", "batchNumber"),),
    "customer": (("Customer", "companyName"),),
    "vendor": (("Supplier", "code"),),
}

# Document-kind signals. Headings are the printed form titles (Ricardo's AO
# packet); phrases are whole words in title or text.
_AO_ANCHOR = re.compile(r"\bASSEMBLY[ \t]+ORDER\b|\bAO[ \t]+LOT[ \t]*#", re.I)
_CATEGORY_SIGNALS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("ASSEMBLY_ORDER", "FILL_RECORD",
     re.compile(r"\bASSEMBLY[ \t]+ORDER[ \t]*[-–][ \t]*SHOP[ \t]+FLOOR\b", re.I)),
    ("ASSEMBLY_ORDER", "QC_SIGNOFF",
     re.compile(r"\bFINISHED[ \t]+GOODS[ \t]+INSPECTION[ \t]*/[ \t]*RELEASE[ \t]+SHEET\b", re.I)),
    ("CUSTOMER_ORDER", "SHIPPING_DOC",
     re.compile(r"\b(?:packing[ \t]+slip|bill[ \t]+of[ \t]+lading|shipping[ \t]+document)\b", re.I)),
    ("CUSTOMER_ORDER", "QUOTE", re.compile(r"\b(?:quotation|quote)\b", re.I)),
    ("CUSTOMER_ORDER", "CUSTOMER_PO", re.compile(r"\bpurchase[ \t]+order\b", re.I)),
    ("PURCHASE_ORDER", "QUOTE", re.compile(r"\b(?:quotation|quote)\b", re.I)),
    ("PURCHASE_ORDER", "ORDER_CONFIRMATION",
     re.compile(r"\b(?:order|vendor|supplier)[ \t]+confirmation\b|\bproforma[ \t]+invoice\b", re.I)),
    ("PURCHASE_ORDER", "COA", re.compile(r"\bcertificate[ \t]+of[ \t]+analysis\b", re.I)),
    ("PURCHASE_ORDER", "CUSTOMS",
     re.compile(r"\bcustoms\b|\bcommercial[ \t]+invoice\b", re.I)),
    ("PURCHASE_ORDER", "SIGNED_PO", re.compile(r"\bpurchase[ \t]+order\b", re.I)),
)
_BULK_LOT = re.compile(r"\bBULK[ \t]+LOT[ \t]*#[ \t]*[:.]?[ \t]*(BC-[A-Z0-9]{3,20})(?![A-Z0-9])", re.I)
# Form #072B page-1 field labels: AO LOT#,
# BULK LOT#, NAME, SIZE, ERP CODE, OE, CUSTOMER, PO, REQUIRED DATE, BULK TYPE,
# PRODUCTION LINE. Only these count as "the next label" after a field.
_AO_FORM_LABEL = re.compile(
    r"(?:AO[ \t]+LOT|BULK[ \t]+LOT|NAME|SIZE\*?|ERP[ \t]+CODE|OE|CUSTOMER|PO|"
    r"REQUIRED[ \t]+DATE|BULK[ \t]+TYPE|PRODUCTION[ \t]+LINE)[ \t]*(?:#[ \t]*:?|:)")
# The AO form's OE and PO fields ("OE:", "PO:", "PO#:"). The field's content
# is the rest of its line up to the next known form label.
_AO_FIELD = re.compile(r"(?<![A-Za-z])(?P<label>OE|PO)[ \t]*(?:#[ \t]*:?|:)")
# A filled PO field holds exactly one token with a digit; dates are not POs.
_AO_PO_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,79}")
_DATE_SHAPED = re.compile(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}")
# "PETRA PO:", "PLX PO:", "OUR PO:", "VENDOR PO:" ... name Petra's own or a
# supplier's order, never the customer's.
_NOT_CUSTOMER_PO = re.compile(
    r"(?:PETRA|PLX|OUR|VENDOR|SUPPLIER|PURCHASE|INTERNAL)[ \t]*\Z", re.I)
_IDENTIFIER_LINKS = {"assembly_order": "assembly_order", "sales_order": "sales_order",
                     "customer_po": "customer_po", "petra_po": "petra_po"}
_PARTY_LINKS = {"customer": "customer", "supplier": "vendor", "vendor": "vendor"}


def _pages(extraction: Mapping[str, Any], text: str) -> list[tuple[int, int, int]]:
    rows = extraction.get("pages") if isinstance(extraction.get("pages"), list) else []
    return [(row["page"], row["start"], row["end"]) for row in rows[:500]
            if isinstance(row, Mapping) and all(type(row.get(k)) is int
                                                for k in ("page", "start", "end"))
            and 0 <= row["start"] <= row["end"] <= len(text)]


class _Evidence:
    """Exact spans over the current title/text, with reading grade."""

    def __init__(self, attrs: Mapping[str, Any], locator: str):
        self.text = attrs.get("text") if isinstance(attrs.get("text"), str) else ""
        self.title = attrs.get("title") if isinstance(attrs.get("title"), str) else ""
        extraction = attrs.get("extraction") if isinstance(attrs.get("extraction"), Mapping) else {}
        warnings = extraction.get("warnings") if isinstance(extraction.get("warnings"), list) else []
        self.ocr = OCR_WARNING in warnings
        self.pages = _pages(extraction, self.text)
        self.locator = locator

    def span(self, start: int, end: int, *, basis: str = "document_text",
             interpreted: bool = False) -> dict[str, Any]:
        source = self.text if basis == "document_text" else self.title
        marker = "documentText" if basis == "document_text" else "documentTitle"
        reading = ("interpreted" if interpreted else "ocr" if self.ocr and
                   basis == "document_text" else "literal")
        item = {"basis": basis, "span": {"start": start, "end": end},
                "quote": source[start:end], "reading": reading,
                "provenance": f"{self.locator}{'&' if '#' in self.locator else '#'}"
                              f"{marker}={start}-{end}"}
        page = next((number for number, low, high in self.pages
                     if basis == "document_text" and low <= start < high), None)
        if page is not None:
            item["page"] = page
        return item


def _requires_review(evidence: Iterable[Mapping[str, Any]]) -> bool:
    return any(item.get("reading") != "literal" for item in evidence)


def _document_values(ev: _Evidence) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Link values printed in the current text, re-extracted (never cached claims)."""
    found: dict[str, list[dict[str, Any]]] = {link: [] for link in LINKS}
    notes: list[str] = []
    if ev.text:
        try:
            claims, _ = _extract_claims(ev.text)
        except ValidationError:
            claims, notes = (), ["extraction_bound_exceeded_values_not_read"]
        for claim in claims:
            field, kind, value = claim["field"], claim["kind"], claim["value"]
            link = (_IDENTIFIER_LINKS.get(field) if kind == "identifier" else
                    _PARTY_LINKS.get(field) if kind == "party" else None)
            if link and isinstance(value, str) and value.strip():
                found[link].append({"value": value.strip(), **ev.span(
                    claim["span"]["start"], claim["span"]["end"],
                    interpreted=claim.get("interpretation") is True)})
        for match in _BULK_LOT.finditer(ev.text):
            found["bulk_lot"].append({"value": match.group(1), **ev.span(
                match.start(1), match.end(1))})
    return found, notes


def _title_ao(ev: _Evidence) -> list[dict[str, Any]]:
    match = re.search(r"(?<![A-Za-z0-9])AO-[A-Za-z0-9-]{3,40}(?![A-Za-z0-9])", ev.title)
    if not match:
        return []
    return [{"value": match.group(), **ev.span(match.start(), match.end(),
                                               basis="document_title", interpreted=True)}]


def _distinct(values: Sequence[Mapping[str, Any]]) -> list[str]:
    seen: dict[str, str] = {}
    for item in values:
        seen.setdefault(item["value"].casefold(), item["value"])
    return list(seen.values())


def _anchor(ev: _Evidence, values: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Which Portal record page this document would be attached on, if decidable."""
    signals: dict[str, list[dict[str, Any]]] = {}
    for source, basis in ((ev.text, "document_text"), (ev.title, "document_title")):
        for match in _AO_ANCHOR.finditer(source):
            signals.setdefault("ASSEMBLY_ORDER", []).append(
                {"signal": "assembly_order_form", **ev.span(match.start(), match.end(), basis=basis)})
            break
    for item in values["petra_po"][:1]:
        signals.setdefault("PURCHASE_ORDER", []).append({"signal": "petra_po_label", **{
            k: v for k, v in item.items() if k != "value"}})
    # The AO form prints its order's OE and customer PO as fields of the AO:
    # on an AO form neither label names a second subject.
    if "ASSEMBLY_ORDER" not in signals:
        for item in values["customer_po"][:1]:
            signals.setdefault("CUSTOMER_ORDER", []).append({"signal": "customer_po_label", **{
                k: v for k, v in item.items() if k != "value"}})
        for item in values["sales_order"]:
            if item["reading"] != "interpreted":
                signals.setdefault("CUSTOMER_ORDER", []).append({"signal": "sales_order_label", **{
                    k: v for k, v in item.items() if k != "value"}})
                break
    candidates = sorted(signals)
    status = ("decided" if len(candidates) == 1 else "conflict" if candidates else "unknown")
    return {"status": status, "entity_type": candidates[0] if status == "decided" else None,
            "candidates": [{"entity_type": name, "source": ENTITY_CONTEXTS[name]["source"],
                            "evidence": signals[name]} for name in candidates],
            "rule": "one Portal attachment page per document; several record kinds named "
                    "as the document's subject is a conflict, never a choice"}


def _category(ev: _Evidence, entity_type: str | None) -> dict[str, Any]:
    if entity_type is None:
        return {"status": "unknown", "candidates": [], "reason": "anchor_not_decided"}
    allowed = ENTITY_CONTEXTS[entity_type]["categories"]
    hits: dict[str, list[dict[str, Any]]] = {}
    for anchor, category, pattern in _CATEGORY_SIGNALS:
        if anchor != entity_type:
            continue
        for source, basis in ((ev.text, "document_text"), (ev.title, "document_title")):
            match = pattern.search(source)
            if match:
                hits.setdefault(category, []).append(ev.span(match.start(), match.end(),
                                                             basis=basis))
                break
    # "Purchase order" is the generic phrase: a document that also states a
    # confirmation, quote, COA, customs or shipping heading is that more
    # specific document, which names the order it refers to.
    if len(hits) > 1:
        hits.pop("SIGNED_PO" if entity_type == "PURCHASE_ORDER" else "CUSTOMER_PO", None)
    names = sorted(hits)
    status = "decided" if len(names) == 1 else "conflict" if names else "unknown"
    return {"status": status, "value": names[0] if status == "decided" else None,
            "allowed_values": list(allowed),
            "candidates": [{"value": name, "evidence": hits[name]} for name in names],
            "reason": None if names else "no_portal_category_heading_found"}


def _record(row: Mapping[str, Any], field: str) -> dict[str, Any] | None:
    """Cite one visible observation (ref, revision, provenance, authority, attributes) matched on ``field``."""
    ref, attrs = row.get("ref"), row.get("attributes")
    if not isinstance(ref, Mapping) or not isinstance(attrs, Mapping):
        return None
    entity_ref = EntityRef(ref["source"], ref["entity_type"], ref["native_id"])
    record = {"entity_id": entity_ref.entity_id, **dict(ref),
              "revision": row.get("revision"), "provenance": row.get("provenance"),
              "authority": row.get("authority"), "matched_field": field,
              "matched_value": attrs.get(field)}
    if entity_ref.entity_type == "SalesOrder":
        if isinstance(attrs.get("poReference"), str) and attrs["poReference"].strip():
            record["customer_po"] = attrs["poReference"].strip()
        record["erp_code"], record["order_number"] = attrs.get("erpCode"), attrs.get("orderNumber")
    return record


def _portal_order_ref(record: Mapping[str, Any]) -> str | None:
    """The order page's ref token: ``order.erp_code || order.order_number``.

    portal/src/app/mrp/orders/[id]/order-detail.tsx@19f1d94:894 (JS ``||``:
    an empty string falls through; nothing is trimmed).
    """
    for key in ("erp_code", "order_number"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _ao_fields(ev: _Evidence, existing: Sequence[Mapping[str, Any]]
               ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[str]]:
    """Read the AO form's OE/PO fields: filled customer PO, printed blanks.

    Blank only when the field is followed by a known form label on its line,
    or ends its line and the next line is empty or starts with a known form
    label. Anything else printed there ("VERBAL", a value on the next line,
    "A, B", a date) is not read: neither a value nor a blank.
    """
    text = ev.text
    taken = [(item["span"]["start"], item["span"]["end"]) for item in existing
             if item.get("basis") == "document_text"]
    values: list[dict[str, Any]] = []
    blanks: dict[str, list[dict[str, Any]]] = {"customer_po": [], "sales_order": []}
    not_read: list[str] = []
    for match in _AO_FIELD.finditer(text):
        label = match.group("label")
        link = "customer_po" if label == "PO" else "sales_order"
        line_start = text.rfind("\n", 0, match.start()) + 1
        if label == "PO" and _NOT_CUSTOMER_PO.search(text[line_start:match.start()]):
            continue
        line_end = text.find("\n", match.end())
        line_end = len(text) if line_end < 0 else line_end
        rest = text[match.end():line_end]
        stop = next((m.start() for m in _AO_FORM_LABEL.finditer(rest)
                     if not rest[:m.start()].strip() or rest[m.start() - 1] in " \t"), len(rest))
        content = rest[:stop]
        if not content.strip():
            if stop < len(rest):
                blank = True
            else:
                following = text[line_end + 1:text.find("\n", line_end + 1)
                                 if text.find("\n", line_end + 1) >= 0 else len(text)]
                blank = (line_end >= len(text) or not following.strip() or
                         _AO_FORM_LABEL.match(following.lstrip()) is not None)
            if blank:
                blanks[link].append({"value": "", **ev.span(match.start(), match.end())})
            else:
                not_read.append(f"ao_{label.lower()}_field_not_read")
            continue
        if label != "PO":
            continue  # a printed OE is read by the general extractor
        offset = match.end() + len(content) - len(content.lstrip())
        token = content.strip()
        start, end = offset, offset + len(token)
        if any(low < end and start < high for low, high in taken):
            continue  # already read as a labelled claim ("CUSTOMER PO: ...")
        if (_AO_PO_VALUE.fullmatch(token) and any(char.isdigit() for char in token) and
                not _DATE_SHAPED.fullmatch(token)):
            # The bare "PO" label is read as the customer PO from the form's
            # layout: an interpretation, so it is review-grade.
            values.append({"value": token, **ev.span(start, end, interpreted=True)})
        else:
            not_read.append("ao_po_field_not_read")
    return values, blanks, sorted(set(not_read))


def _link_report(link: str, values: list[dict[str, Any]], blank: list[dict[str, Any]],
                 lookup: Callable[[str, str, str], list[dict[str, Any]]] | None,
                 extra_rule: str | None = None) -> dict[str, Any]:
    distinct = _distinct(values)
    if len(distinct) > 1:  # every linked record is single-valued per document
        status = "conflict_in_document"
    elif distinct:
        status = "found_in_document"
    elif blank:
        status = "blank_in_document"
    else:
        status = "absent_in_document"
    report: dict[str, Any] = {"link": link, "status": status, "values": distinct,
                              "evidence": values + blank,
                              "requires_review": _requires_review(values)}
    if extra_rule:
        report["portal_rule"] = extra_rule
    if status != "found_in_document" or link not in RECORD_KEYS:
        report["record_status"] = "not_looked_up"
        report["records"] = []
        return report
    if lookup is None:
        report["record_status"] = "lookup_unavailable"
        report["records"] = []
        return report
    records: list[dict[str, Any]] = []
    for entity_type, field in RECORD_KEYS[link]:
        for row in lookup(entity_type, field, distinct[0])[:10]:
            record = _record(row, field)
            if record and record["entity_id"] not in {item["entity_id"] for item in records}:
                records.append(record)
    report["records"] = records[:10]
    report["record_status"] = ("matched" if len(records) == 1 else
                               "multiple_records" if records else "no_visible_record")
    return report


def _model_rows(model_suggestions: Sequence[Mapping[str, Any]], ev: _Evidence,
                allowed_category: Sequence[str]) -> tuple[dict[str, list[dict]], list[dict]]:
    accepted: dict[str, list[dict[str, Any]]] = {}
    rejected: list[dict[str, Any]] = []
    for raw in list(model_suggestions)[:50]:
        if not isinstance(raw, Mapping):
            rejected.append({"reason": "suggestion_not_an_object"})
            continue
        column, value, model = raw.get("column"), raw.get("value"), raw.get("model")
        basis, span, quote = raw.get("basis", "document_text"), raw.get("span"), raw.get("quote")
        reason = None
        spec = COLUMN_BY_NAME.get(column) if isinstance(column, str) else None
        if spec is None:
            reason = "unknown_portal_column"
        elif column in PORTAL_RECORD_COLUMNS:
            reason = "column_set_from_portal_record"
        elif not isinstance(value, str) or not 1 <= len(value.strip()) <= 255:
            reason = "value_invalid"
        elif not isinstance(model, str) or not 1 <= len(model.strip()) <= 120:
            reason = "model_label_required"
        elif "choices" in spec and value not in spec["choices"]:
            reason = "value_not_an_allowed_choice"
        elif column == "DocumentCategory" and value not in (allowed_category or STORAGE_CATEGORIES):
            reason = "category_not_offered_for_anchor"
        elif basis not in {"document_text", "document_title"}:
            reason = "basis_invalid"
        elif (not isinstance(span, Mapping) or set(span) != {"start", "end"} or
              type(span["start"]) is not int or type(span["end"]) is not int):
            reason = "exact_span_required"
        else:
            source = ev.text if basis == "document_text" else ev.title
            if (not 0 <= span["start"] < span["end"] <= len(source) or
                    source[span["start"]:span["end"]] != quote):
                reason = "quote_does_not_match_current_span"
        if reason:
            rejected.append({"column": column if isinstance(column, str) else None,
                             "reason": reason})
            continue
        item = {"value": value.strip(), "model": model.strip(),
                **ev.span(span["start"], span["end"], basis=basis)}
        item["reading"] = "model_suggestion"
        accepted.setdefault(column, []).append(item)
    return accepted, rejected


def _compare(proposal: Mapping[str, Any], current: Mapping[str, Any] | None,
             fresh: bool = True, read: frozenset[str] | None = None) -> dict[str, Any]:
    if current is not None and read is not None and proposal["column"] not in read:
        # The snapshot did not request this column: its absence says nothing.
        return {"comparison": "not_read", "current_value": None}
    result = _compare_value(proposal, current)
    if current is not None and not fresh:
        # Values read at another drive revision are shown, never compared.
        result = {"comparison": "stale_snapshot", "current_value": result["current_value"],
                  "comparison_at_snapshot_revision": result["comparison"]}
    return result


def _compare_value(proposal: Mapping[str, Any], current: Mapping[str, Any] | None
                   ) -> dict[str, Any]:
    name = proposal["column"]
    if current is None:
        return {"comparison": "not_read", "current_value": None}
    raw = current.get(name)
    present = raw is not None and not (isinstance(raw, str) and not raw.strip())
    boolean = COLUMN_BY_NAME[name]["kind"] == "boolean"
    if present and not isinstance(raw, bool if boolean else str):
        return {"comparison": "current_value_unsupported_shape", "current_value": raw}
    value = raw if boolean or not present else raw.strip()
    if present and COLUMN_BY_NAME[name]["kind"] == "dateTime":
        value = value[:10]
    status = proposal["status"]
    if status in {"proposed", "model_suggestion"}:
        # a DocumentCategory agrees as the page category or as its canonical choice
        comparison = ("agree" if present and value in (proposal["value"],
                                                       proposal.get("written_as")) else
                      "differs" if present else "missing")
    elif status in {"unknown", "conflict"}:
        comparison = "undetermined"
    else:
        comparison = "current_only" if present else "absent"
    return {"comparison": comparison, "current_value": value}


def _against_stated(row: dict[str, Any]) -> None:
    """A value the Portal already states on this revision is never proposed again; a
    proposal that differs from it is a conflict, never a choice (QA-06)."""
    if row["status"] not in {"proposed", "model_suggestion"}:
        return
    if row["comparison"] == "agree":
        row.update(status="stated_by_portal", value=None, basis=None,
                   reason="portal_states_this_value")
        row.pop("written_as", None)
    elif row["comparison"] == "differs":
        row.update(status="conflict", reason="differs_from_portal_stated", value=None,
                   candidates=[{"value": row["value"], "basis": row["basis"]},
                               {"value": row["current_value"], "basis": "stated_by_portal"}])
        row.pop("written_as", None)


CONFIRMED_VS_STATED = "plx.confirmed-vs-portal-stated/v1"


def confirmed_against_stated(confirmed: Mapping[str, Any], stated: Any) -> dict[str, Any]:
    """A person's confirmed Portal columns against what the Portal states on the same
    version (``SharePointFile.portal_metadata``, ``taxonomy.stated_metadata``). Per
    confirmed column: ``agree``, ``conflict`` (``differs_from_portal_stated``, as for a
    proposal: never a choice, the stated value is kept beside it), ``portal_empty`` (a
    typed gap: empty or not provisioned) or ``not_read`` (the version's column read did not
    cover it, or no stated read at all). A DocumentCategory agrees as the category or as the
    column choice the Portal writes for it (``document_category_column``, TASK-2594). Pure; no I/O."""
    columns = stated.get("columns") if isinstance(stated, Mapping) else None
    gaps = stated.get("gaps") if isinstance(stated, Mapping) else None
    columns = columns if isinstance(columns, Mapping) else {}
    gaps = gaps if isinstance(gaps, Mapping) else {}
    out: dict[str, dict[str, Any]] = {}
    for name, value in sorted(confirmed.items()):
        if name in columns:
            current = columns[name]
            same = {value}
            if name == "DocumentCategory":
                same.add(document_category_column(resolve_document_category(value)))
            elif (COLUMN_BY_NAME.get(name) or {}).get("kind") == "dateTime" and \
                    isinstance(current, str):
                current = current[:10]
            row = ({"comparison": "agree"} if current in same else
                   {"comparison": "conflict", "reason": "differs_from_portal_stated",
                    "candidates": [{"value": value, "basis": "person_confirmed"},
                                   {"value": current, "basis": "stated_by_portal"}]})
        elif gaps.get(name) in {"empty", "empty_or_not_provisioned"}:
            row = {"comparison": "portal_empty", "gap": gaps[name]}
        else:
            row = {"comparison": "not_read"}
        out[name] = row
    conflicts = [name for name, row in out.items() if row["comparison"] == "conflict"]
    return {"format": CONFIRMED_VS_STATED,
            "status": ("conflict" if conflicts else "no_conflict" if any(
                row["comparison"] != "not_read" for row in out.values()) else "not_read"),
            "columns": out, "conflicts": conflicts,
            "stated_citation": stated.get("citation") if isinstance(stated, Mapping) else None}


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~")


def _quote(value: str) -> str:
    """urllib.parse.quote(value, safe="") without importing a network package."""
    return "".join(chr(byte) if chr(byte) in _UNRESERVED else f"%{byte:02X}"
                   for byte in value.encode("utf-8"))


def _listitem_api(attrs: Mapping[str, Any], source: str | None,
                  native_id: str | None) -> str | None:
    """The document's own listItem URL (drive id = source_container, item id =
    the rest of the native id). None when the document's identity does not give one."""
    drive_id = attrs.get("source_container")
    if (source != "sharepoint-drive" or not isinstance(native_id, str) or
            not isinstance(drive_id, str) or not drive_id or
            not native_id.startswith(drive_id + ":") or len(native_id) == len(drive_id) + 1):
        return None
    return ("https://graph.microsoft.com/v1.0/drives/" + _quote(drive_id) +
            "/items/" + _quote(native_id[len(drive_id) + 1:]) +
            "/listItem?expand=fields")


def _columns_snapshot(attrs: Mapping[str, Any], revision: str,
                      supplied: Mapping[str, Any] | None, expected_api: str | None
                      ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    snapshot, origin = (supplied, "supplied_listitem_snapshot") if supplied is not None else (
        attrs.get("source_columns"), "stored_listitem_snapshot")
    if snapshot is None:
        return None, {"status": "not_read",
                      "reason": "no SharePoint listItem fields snapshot for this document"}
    if (not isinstance(snapshot, Mapping) or
            snapshot.get("source") != "sharepoint-listitem-fields/v1" or
            not isinstance(snapshot.get("fields"), Mapping)):
        return None, {"status": "invalid_snapshot"}
    # A stored snapshot was tied to this drive item by the code that stored it.
    # A supplied one must name this document's own listItem, or it describes
    # some other file.
    if supplied is not None and (expected_api is None or
                                 snapshot.get("source_api") != expected_api):
        return None, {"status": "snapshot_identity_mismatch", "origin": origin,
                      "source_api": snapshot.get("source_api"),
                      "expected_source_api": expected_api,
                      "reason": "supplied listItem snapshot is not this document's drive item"}
    etag = snapshot.get("drive_etag")
    # Imported observations carry the source's own revision (the drive eTag)
    # separately from the store's content-hash revision.
    source_revision = attrs.get("source_revision")
    if not isinstance(source_revision, str) or not source_revision:
        source_revision = revision
    return dict(snapshot["fields"]), {
        "status": "read", "origin": origin, "drive_etag": etag,
        **({"field_names": list(snapshot["field_names"])}
           if isinstance(snapshot.get("field_names"), list) else {}),
        "source_api": snapshot.get("source_api"),
        "document_source_revision": source_revision,
        "matches_document_revision": etag == source_revision,
        # Graph omits empty fields from listItem.fields, so an absent column
        # is "empty or not provisioned" (SPEC §1 critical finding), never
        # proof that the column exists and is blank. Since Portal TASK-2250
        # (portal/src/app/api/attachments/route.ts@8f9009d:491) a Portal
        # upload's columns are written later by a finalize job the reporting
        # API does not serve, so "empty" includes "not finalized yet".
        "absent_field_meaning": "empty_or_not_provisioned"}


def build_proposals(*, document_id: str, revision: str, fingerprint: str,
                    attrs: Mapping[str, Any], locator: str,
                    lookup: Callable[[str, str, str], list[dict[str, Any]]] | None = None,
                    model_suggestions: Sequence[Mapping[str, Any]] = (),
                    current_columns: Mapping[str, Any] | None = None,
                    source: str | None = None, native_id: str | None = None
                    ) -> dict[str, Any]:
    """Return Portal-taxonomy proposals for one current document. Pure; no I/O.

    ``lookup(entity_type, field, value)`` returns the caller's ACL-visible
    observations (``ref``, ``revision``, ``provenance``, ``authority``,
    ``attributes``) whose attribute ``field`` equals ``value`` exactly. Several matches are reported, never merged.
    ``source``/``native_id`` identify the document; a supplied
    ``current_columns`` snapshot must be this drive item's listItem.
    """
    ev = _Evidence(attrs, locator)
    values, notes = _document_values(ev)
    title_leads = _title_ao(ev)
    anchor = _anchor(ev, values)
    entity_type = anchor["entity_type"]
    blanks: dict[str, list[dict[str, Any]]] = {"customer_po": [], "sales_order": []}
    if "ASSEMBLY_ORDER" in {c["entity_type"] for c in anchor["candidates"]}:
        ao_values, blanks, unread = _ao_fields(
            ev, [item for link in ("customer_po", "petra_po", "sales_order")
                 for item in values[link]])
        values["customer_po"] += ao_values
        notes.extend(unread)
    # Portal: finished-good lot number = AO mo_code (LOT_RULE_SOURCE).
    if entity_type == "ASSEMBLY_ORDER":
        values["lot"] = [dict(item) for item in values["assembly_order"]]
    links = {link: _link_report(link, values[link], blanks.get(link, []), lookup,
                                LOT_RULE_SOURCE if link in {"lot", "bulk_lot"} else None)
             for link in LINKS}
    if title_leads and not values["assembly_order"]:
        # A filename is a review lead, never the document's own statement.
        links["assembly_order"]["title_leads"] = title_leads
        notes.append("assembly_order_only_in_title")
    # Sales-order records carry the customer PO: a blank or different printed
    # PO is reported, never filled from either side.
    po_records = [row for row in links["sales_order"]["records"]
                  if row.get("customer_po")]
    # Only a printed-blank label says the document is blank; a PO the reader
    # did not find is "not read", never "blank".
    if po_records:
        record_pos = sorted({row["customer_po"] for row in po_records})
        po_link = links["customer_po"]
        po_link["sales_order_record_values"] = record_pos
        po_link["document_vs_record"] = {
            "blank_in_document": "document_blank_record_has_value",
            "absent_in_document": "document_value_not_read",
            "conflict_in_document": "document_values_conflict",
        }.get(po_link["status"]) or (
            "agree" if {v.casefold() for v in po_link["values"]} ==
            {v.casefold() for v in record_pos} else "differs")
    category = _category(ev, entity_type)
    allowed = ENTITY_CONTEXTS[entity_type]["categories"] if entity_type else ()
    models, rejected = _model_rows(model_suggestions, ev, allowed)

    # The ref token the Portal page would send for this anchor.
    ref_value, ref_evidence, ref_status = None, [], "unknown"
    ref_basis, ref_reason = "labeled_document_value", None
    if entity_type:
        ref_link = links[ENTITY_CONTEXTS[entity_type]["ref_link"]]
        ref_evidence = ref_link["evidence"]
        if ref_link["status"] == "found_in_document" and entity_type == "CUSTOMER_ORDER":
            # The order page sends the order record's erp_code || order_number,
            # not whichever number the document prints: only one visible
            # matched order record decides it.
            ref_reason = "order_record_not_resolved"
            if ref_link["record_status"] == "matched":
                ref_value = _portal_order_ref(ref_link["records"][0])
                if ref_value:
                    ref_status, ref_basis, ref_reason = ("proposed",
                                                         "portal_rule_on_linked_record", None)
                    ref_evidence = list(ref_evidence) + [{
                        "basis": "linked_record", "rule": ENTITY_CONTEXTS[entity_type]["source"],
                        **{k: ref_link["records"][0][k] for k in (
                            "entity_id", "source", "entity_type", "native_id", "revision",
                            "provenance", "erp_code", "order_number")}}]
        elif ref_link["status"] == "found_in_document":
            ref_value, ref_status = ref_link["values"][0], "proposed"
        elif ref_link["status"] == "conflict_in_document":
            ref_status = "conflict"
    rule = (build_attachment_metadata(entity_type, category["value"] or "", {
        "ref": ref_value}) if entity_type else {})
    columns: list[dict[str, Any]] = []
    for spec in PROPOSAL_COLUMNS:
        name = spec["name"]
        choices = PLX_ALLOWED.get(name) or spec.get("choices")
        row: dict[str, Any] = {"column": name, "display_name": spec["display_name"],
                               "kind": spec["kind"],
                               "allowed_values": list(choices) if choices
                               else (list(allowed) if name == "DocumentCategory" and allowed
                                     else None),
                               "status": "not_set_by_portal_rule", "value": None,
                               "basis": None, "evidence": [], "requires_review": False,
                               "portal_rule": ATTACHMENT_RULE_SOURCE}
        if name in PORTAL_RECORD_COLUMNS:
            row["reason"] = "set_from_portal_record_not_proposed"
        elif entity_type is None and name in {"PartyType", "PartyRef", "DocumentCategory",
                                            "ItemRef", "DocStatus", "Source",
                                            "RetentionCategory", "Confidentiality"}:
            row.update(status="unknown", reason="anchor_" + anchor["status"])
        elif entity_type is not None:
            party = party_type_for_entity(entity_type)
            if name in {"PartyType", "DocStatus", "Source", "Confidentiality"}:
                row.update(status="proposed", value=rule[name], basis="portal_rule",
                           evidence=anchor["candidates"][0]["evidence"],
                           requires_review=_requires_review(anchor["candidates"][0]["evidence"]))
            elif name in {"DocumentCategory", "RetentionCategory"}:
                # The proposal is the page category; since TASK-2215 the generic upload
                # doors write its canonical choice (written_as; since TASK-2594 its row's
                # short column choice) and the retention of its row, while files written
                # before (and issued records) hold the page category itself.
                if category["status"] == "decided":
                    evidence = category["candidates"][0]["evidence"]
                    row.update(status="proposed", basis="portal_rule",
                               value=rule["RetentionCategory"], evidence=evidence,
                               requires_review=_requires_review(evidence))
                    if name == "DocumentCategory":
                        row.update(value=category["value"], written_as=rule[name],
                                   basis="portal_category_heading_rule")
                elif name == "DocumentCategory":
                    row.update(status=category["status"], candidates=category["candidates"],
                               reason=category["reason"] or "several_portal_categories_found")
                else:
                    row.update(status=category["status"],
                               reason="retention_follows_document_category")
            elif name == "ItemRef":
                printed = [item for item in ref_evidence if item.get("basis") != "linked_record"]
                if ref_status == "proposed":
                    row.update(status="proposed", value=ref_value, basis=ref_basis,
                               evidence=ref_evidence, requires_review=_requires_review(printed))
                else:
                    row.update(status=ref_status, evidence=ref_evidence,
                               reason=ref_reason or "record_number_" + ("conflict_in_document"
                                   if ref_status == "conflict" else "not_found_in_document"))
            elif name == "PartyRef":
                if party == "Supplier":
                    row.update(status="proposed" if ref_status == "proposed" else ref_status,
                               value=ref_value, basis="labeled_document_value" if ref_value else None,
                               evidence=ref_evidence,
                               requires_review=_requires_review(ref_evidence))
                elif party == "Customer":
                    # PartyRef = customerCode ?? customer; neither is printed.
                    row.update(status="unknown", reason="customer_code_not_resolved")
                else:
                    row.update(status="not_applicable", reason="internal_party_has_no_party_ref")
            elif name == "CustomerRef" and party != "Customer":
                row.update(status="not_applicable", reason="customer_ref_only_for_customer_party")
        suggestions = models.get(name, [])
        if suggestions:
            row["model_suggestions"] = suggestions
            suggested = _distinct(suggestions)
            if row["status"] == "proposed":
                if any(v != row["value"] for v in suggested):
                    row.update(status="conflict", reason="model_disagrees_with_rule",
                               candidates=[{"value": row["value"], "basis": row["basis"]}] +
                                          [{"value": v, "basis": "model_suggestion"}
                                           for v in suggested if v != row["value"]],
                               value=None)
            elif row["status"] in {"unknown", "not_set_by_portal_rule"} and len(suggested) == 1:
                row.update(status="model_suggestion", value=suggested[0],
                           basis="model_suggestion", evidence=suggestions, requires_review=True)
                if name == "DocumentCategory":
                    row["written_as"] = document_category_column(
                        resolve_document_category(suggested[0]))
            elif row["status"] == "conflict":
                pass  # a model never resolves a conflict
            elif len(suggested) > 1:
                row.update(status="conflict", reason="models_disagree", value=None)
        columns.append(row)

    current, snapshot = _columns_snapshot(attrs, revision, current_columns,
                                          _listitem_api(attrs, source, native_id))
    fresh = snapshot.get("matches_document_revision") is True
    # The columns the snapshot requested (``field_names``, as the capture stores it); a
    # snapshot that does not say is read as requesting every column.
    names = snapshot.get("field_names")
    read = (frozenset(n for n in names if isinstance(n, str)) | frozenset(current or ())
            if isinstance(names, list) else None)
    summary: dict[str, int] = {}
    for row in columns:
        row.update(_compare(row, current, fresh, read))
        summary[row["comparison"]] = summary.get(row["comparison"], 0) + 1
        _against_stated(row)
    content_type_current = current.get("ContentType") if current else None
    content_type = {
        "status": "no_portal_rule",
        "reason": "the Portal provisions one content type and assigns none at upload; "
                  "CONTENT_TYPES maps inbound SharePoint content types only",
        "portal_rule": CONTENT_TYPES_SOURCE, "allowed_values": sorted(CONTENT_TYPES),
        "current_value": content_type_current if isinstance(content_type_current, str) else None,
        "current_value_is_current_revision": fresh if current else None,
        "current_portal_document_type": CONTENT_TYPES.get(content_type_current)
        if isinstance(content_type_current, str) else None}
    return {"schema": SCHEMA, "document_id": document_id, "source_revision": revision,
            "source_fingerprint": fingerprint, "original_locator": locator,
            "portal_taxonomy": {"repo": "petralabx/plx-customer-portal",
                                "commit": PORTAL_COMMIT, "columns": COLUMNS_SOURCE},
            "reading": "ocr_requires_review" if ev.ocr else "text",
            "anchor": anchor, "category": category, "content_type": content_type,
            "columns": columns, "linked_records": [links[link] for link in LINKS],
            "rejected_model_suggestions": rejected,
            "current_columns": snapshot, "comparison_summary": summary,
            "notes": notes,
            "authority": "proposal_only_no_source_write",
            "sharepoint_write_available": False, "business_system_writes": 0}


__all__ = ["build_proposals", "confirmed_against_stated", "CURRENT_COLUMN_FIELDS", "SCHEMA",
           "RECORD_KEYS", "LINKS"]
