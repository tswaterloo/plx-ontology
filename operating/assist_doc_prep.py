"""Proactive document, spreadsheet, presentation, and PO/Quote preparer for Assist.

Turns a ``prepare_document`` plan act (or a detected deliverable request in a matter) into a
real downloadable artifact (``.xlsx``, ``.pptx``, ``.docx``, ``.pdf``, ``.csv``, or filled form)
through ``operating.instruct_files`` (and ``operating.doc_fill`` when an existing template path
is supplied).

Design invariants:
* Grounded facts only: never invent Petra prices, inventory counts, margins, SKUs, or vendor
  terms. Any operational or financial value absent from the matter's grounded evidence is
  populated with the explicit placeholder ``[TBD - confirm from BC/inventory]``.
* Zero em dashes (``\\u2014``) in generated documents, titles, summaries, or preview lines.
* Safe private storage: files are written under ``<state_dir>/assist-docs/<artifact_id>/``
  (or a private temp directory when ``state_dir`` is omitted) with 0700 directory and 0600
  file permissions, using sanitized filenames from ``instruct_files.file_name``.
* Fail-closed screening: if a ``screen`` callable flags generated content, no file is written.

Evidence label: ``isolated-fixture`` (``operating/tests/test_assist_doc_prep.py``).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping, Optional, Sequence

from operating import instruct_files as inf
from operating.file_modes import keep_private

TBD_PLACEHOLDER = "[TBD - confirm from BC/inventory]"
DOC_KINDS: tuple[str, ...] = ("xlsx", "pptx", "docx", "pdf", "csv", "fill_form")
WORKBOOK_OPERATIONS: tuple[str, ...] = ("create", "add_sheet", "append_rows")

_ORDER_REF_RE = re.compile(
    r"\b((?:PO|SO|WO|MO|INV|QT|QUOTE|LOT|BATCH)[-\s#]*[A-Z0-9][A-Z0-9-]{1,20})\b",
    re.I,
)
_QTY_ITEM_RE = re.compile(
    r"\b(\d{1,3}(?:,\d{3})+|\d+)\s+"
    r"((?:\d+(?:\.\d+)?\s*(?:oz|ml|g|kg|lb|lbs|l|gal)\s+)?"
    r"[a-zA-Z][a-zA-Z0-9/\-]{1,30}(?:\s+[a-zA-Z][a-zA-Z0-9/\-]{1,30}){0,3})",
)
_PERIOD_RE = re.compile(
    r"\b((?:Q[1-4](?:\s+20\d{2})?)|(?:FY\s*20\d{2})|(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}))\b",
    re.I,
)
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")

# SharePoint destination & Excel workbook operation prompt extractors.
_SP_FOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Save to SharePoint under Customers/Soapbox/Q4"
    re.compile(
        r"\bsharepoint\b[^.\n]{0,30}?\b(?:under|in|into|to|folder|path)\s+(?:folder\s+|path\s+)?"
        r"[\"']?([A-Za-z0-9._() -]+(?:/[A-Za-z0-9._() -]+)*)[\"']?",
        re.I,
    ),
    # "Create folder Customers/Soapbox/2026/Q4 Reviews in SharePoint"
    re.compile(
        r"\b(?:create|make|add|ensure)\s+(?:a\s+|the\s+|new\s+)?(?:nested\s+)?folder\s+"
        r"[\"']?([A-Za-z0-9._() -]+(?:/[A-Za-z0-9._() -]+)*)[\"']?"
        r"(?:\s+(?:in|on|under)\s+sharepoint)?",
        re.I,
    ),
    # "Put in folder Customers/Soapbox/Q4" (explicit multi-segment folder path)
    re.compile(
        r"\b(?:save|put|upload|store|file)\b[^.\n]{0,40}?\b(?:under|in|into)\s+(?:folder\s+)?"
        r"[\"']?([A-Za-z0-9._() -]+/[A-Za-z0-9._() -]+(?:/[A-Za-z0-9._() -]+)*)[\"']?",
        re.I,
    ),
)
_SP_CREATE_FOLDER_RE = re.compile(
    r"\b(?:create|make|add|new|ensure)\s+(?:a\s+|the\s+|missing\s+|parent\s+|nested\s+)?folders?\b",
    re.I,
)
_SP_TRAILING_STOP_RE = re.compile(
    r"\s+(?:and\s+(?:save|upload|put|add|create|share|send|notify|use|keep)|"
    r"in\s+sharepoint|on\s+sharepoint|to\s+sharepoint|as\s+(?:a|an)\s+|with\s+).*$",
    re.I,
)
_XLSX_QUOTED_RE = re.compile(
    r"[\"']([A-Za-z0-9][A-Za-z0-9._() -]{0,120}\.xlsx)[\"']",
    re.I,
)
_XLSX_PREP_RE = re.compile(
    r"\b(?:to|in|into|from|for|update|open|edit|workbook|file)\s+(?:the\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9._() -]{0,120}\.xlsx)\b",
    re.I,
)
_XLSX_BARE_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9._()-]{0,120}\.xlsx)\b",
    re.I,
)
_ADD_SHEET_RE = re.compile(
    r"\b(?:add|create|insert|make)\s+(?:a\s+|an\s+|new\s+)?(?:sheet|worksheet|tab)\b",
    re.I,
)
_APPEND_ROWS_RE = re.compile(
    r"\b(?:append|add|insert|log)\s+(?:new\s+)?(?:rows?|entries|lines|records?)\s+(?:to|into|in)\b|"
    r"\bappend\s+(?:to|into)\b[^.\n]{0,50}\b(?:\.xlsx|excel|spreadsheet|workbook|tracker|sheet)\b|"
    r"\badd\s+to\b[^.\n]{0,40}\b(?:excel|spreadsheet|workbook|tracker|\.xlsx)\b",
    re.I,
)
_SHEET_NAME_RE = re.compile(
    r"\b(?:tab|sheet|worksheet)\s+(?:called|named)\s+[\"']([^\"'\n]{1,40})[\"']|"
    r"\b(?:tab|sheet|worksheet)\s+(?:called|named)\s+([A-Za-z0-9][A-Za-z0-9 _().-]{0,30}?)"
    r"(?=\s+(?:to|in|into|on|for|under|with)\b|[.,;]|$)|"
    r"\bon\s+(?:the\s+)?(?:sheet|tab|worksheet)\s+[\"']?([A-Za-z0-9][A-Za-z0-9 _().-]{0,30})[\"']?",
    re.I,
)

# Detection patterns for genuine proactive deliverable asks (narrow to avoid false positives on routine status emails).
_PROACTIVE_TRIGGERS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("xlsx", "count_sheet", re.compile(r"\b(?:count\s+sheet|inventory\s+(?:count\s+)?sheet|stock\s+count\s+sheet|cycle\s+count\s+sheet)\b", re.I)),
    ("xlsx", "pricing_sheet", re.compile(r"\b(?:pricing\s+sheet|price\s+sheet|cost\s+sheet|margin\s+sheet|bom\s+sheet)\b", re.I)),
    ("xlsx", "tracker", re.compile(r"\b(?:add|create|insert)\s+(?:a\s+|new\s+)?(?:tab|sheet|worksheet)\b[^.\n]{0,60}\b(?:\.xlsx|excel|workbook|tracker|spreadsheet)\b", re.I)),
    ("xlsx", "tracker", re.compile(r"\b(?:append|add)\s+(?:new\s+)?(?:rows?|entries|lines)\s+(?:to|into)\b[^.\n]{0,60}\b(?:\.xlsx|excel|workbook|tracker|spreadsheet|sheet)\b", re.I)),
    ("xlsx", "tracker", re.compile(r"\b(?:prepare|create|build|make|put\s+together|draft|send|need|asks?\s+\w+\s+for)\b[^.\n]{0,60}\b(?:spreadsheet|excel(?:\s+sheet)?|xlsx|workbook|tracker)\b", re.I)),
    ("pptx", "presentation", re.compile(r"\b(?:presentation|slide\s*deck|powerpoint|pptx|pitch\s*deck|qbr\s*deck|margin\s+deck|executive\s+deck)\b", re.I)),
    ("docx", "purchase_order", re.compile(r"\b(?:draft|prepare|create|generate|write|make)\b[^.\n]{0,40}\b(?:purchase\s+order|po)\b", re.I)),
    ("docx", "quote", re.compile(r"\b(?:draft|prepare|create|generate|write|build)\b[^.\n]{0,40}\b(?:quote|quotation|price\s+quote)\b", re.I)),
    ("docx", "memo", re.compile(r"\b(?:draft|prepare|create|write)\b[^.\n]{0,40}\b(?:memo|sop|standard\s+operating\s+procedure|customer\s+letter|supplier\s+letter|summary\s+doc(?:ument)?|word\s+doc(?:ument)?)\b", re.I)),
    ("pdf", "summary", re.compile(r"\b(?:prepare|create|generate|draft|make)\b[^.\n]{0,40}\b(?:pdf\s+(?:summary|report|sheet)|summary\s+pdf|report\s+pdf)\b", re.I)),
    ("fill_form", "form_fill", re.compile(r"\b(?:fill\s*(?:out|in)|complete)\b[^.\n]{0,50}\b(?:form|questionnaire|application|checklist)\b", re.I)),
)

_COUNT_SPLIT_RE = re.compile(
    r"\bcount\s+sheet\s+(?:of|for)\s+([^.\n;]+)",
    re.I,
)
_FOR_TOPIC_RE = re.compile(
    r"\b(?:prepare|draft|create|build|make|generate|write)\s+(?:a\s+|an\s+|the\s+)?([^.\n;]+)",
    re.I,
)
_ASK_FOR_RE = re.compile(
    r"\basks?\s+[A-Z][a-z]+\s+for\s+(?:a\s+|an\s+|the\s+)?([^.\n;]+)",
    re.I,
)


def _clean(raw: Any) -> str:
    """Plain single-line text with zero em dashes or en dashes."""
    text = str(raw or "").replace("\u2014", " - ").replace("\u2013", "-")
    return re.sub(r"\s+", " ", text).strip()


def _infer_kind_and_type(kind_raw: str, text: str) -> tuple[str, str]:
    low_kind = _clean(kind_raw).lower().lstrip(".")
    low_text = _clean(text).lower()

    if low_kind in ("xlsx", "xls", "excel", "spreadsheet", "sheet"):
        kind = "xlsx"
    elif low_kind in ("pptx", "ppt", "powerpoint", "deck", "slides", "presentation"):
        kind = "pptx"
    elif low_kind in ("docx", "doc", "word", "memo", "letter", "sop"):
        kind = "docx"
    elif low_kind == "pdf":
        kind = "pdf"
    elif low_kind == "csv":
        kind = "csv"
    elif low_kind in ("fill_form", "form", "form_fill"):
        kind = "fill_form"
    else:
        kind = ""

    doc_type = ""
    if any(tok in low_text for tok in ("count sheet", "inventory count", "stock count", "cycle count", "finished goods")):
        doc_type = "count_sheet"
        kind = kind or "xlsx"
    elif any(tok in low_text for tok in ("pricing sheet", "price sheet", "cost sheet", "margin sheet", "bom sheet")):
        doc_type = "pricing_sheet"
        kind = kind or "xlsx"
    elif any(tok in low_text for tok in ("presentation", "slide deck", "deck", "powerpoint", "pptx", "slides", "qbr")):
        doc_type = "presentation"
        kind = kind or "pptx"
    elif re.search(r"\b(?:purchase\s+order|po)\b", low_text):
        doc_type = "purchase_order"
        kind = kind or "docx"
    elif re.search(r"\b(?:quote|quotation)\b", low_text):
        doc_type = "quote"
        kind = kind or "docx"
    elif any(tok in low_text for tok in ("spreadsheet", "excel", "xlsx", "tracker", "workbook", "csv")):
        doc_type = "tracker"
        kind = kind or ("csv" if "csv" in low_text and "xlsx" not in low_text and "excel" not in low_text else "xlsx")
    elif any(tok in low_text for tok in ("fill out", "fill in", "questionnaire", "form")):
        doc_type = "form_fill"
        kind = kind or "fill_form"
    elif any(tok in low_text for tok in ("sop", "memo", "letter", "policy", "notice")):
        doc_type = "memo"
        kind = kind or "docx"
    elif "pdf" in low_text:
        doc_type = "summary"
        kind = kind or "pdf"
    else:
        kind = kind or "docx"
        if kind in ("xlsx", "csv"):
            doc_type = "tracker"
        elif kind == "pptx":
            doc_type = "presentation"
        elif kind == "fill_form":
            doc_type = "form_fill"
        else:
            doc_type = "summary"

    return kind, doc_type


def _derive_title(raw_title: str, about: str, doc_type: str) -> str:
    candidate = _clean(raw_title)
    if not candidate:
        m = _ASK_FOR_RE.search(about) or _FOR_TOPIC_RE.search(about)
        candidate = _clean(m.group(1) if m else about)
    candidate = re.sub(r"^(?:a|an|the)\s+", "", candidate, flags=re.I).strip(" .,:;")
    if not candidate:
        defaults = {
            "count_sheet": "Inventory Count Sheet",
            "pricing_sheet": "Pricing Sheet",
            "tracker": "Operations Tracker",
            "presentation": "Executive Summary Deck",
            "purchase_order": "Purchase Order Draft",
            "quote": "Customer Quote Draft",
            "memo": "Operations Memo",
            "form_fill": "Form Preparation Sheet",
            "summary": "Document Summary",
        }
        return defaults.get(doc_type, "Prepared Document")
    if len(candidate) > 76:
        candidate = candidate[:76].rsplit(" ", 1)[0].strip()
    return candidate[0].upper() + candidate[1:]


def _clean_folder_path(raw: Any) -> str:
    """Validate and normalize a SharePoint folder path (empty string if missing/invalid)."""
    text = _clean(raw).replace("\\", "/").strip(" /.,;:")
    if not text:
        return ""
    text = _SP_TRAILING_STOP_RE.sub("", text).strip(" /.,;:")
    if not text:
        return ""
    from operating.autopilot_write_adapters import AdapterRefused, SharePointAdapter
    try:
        segments = SharePointAdapter.normalize_folder_path(text)
    except AdapterRefused:
        return ""
    return "/".join(segments)


def parse_sharepoint_destination(
    text: str,
    explicit: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Parse SharePoint destination folder, folder creation flag, and conflict policy."""
    exp = explicit if isinstance(explicit, Mapping) else {}
    sp_sub = exp.get("sharepoint") if isinstance(exp.get("sharepoint"), Mapping) else {}

    raw_dest = (
        exp.get("destination_folder")
        or exp.get("folder_path")
        or exp.get("sharepoint_folder")
        or sp_sub.get("destination_folder")
        or sp_sub.get("folder_path")
        or ""
    )
    dest = _clean_folder_path(raw_dest)
    if not dest and text:
        cleaned = _clean(text)
        for pat in _SP_FOLDER_PATTERNS:
            m = pat.search(cleaned)
            if m:
                cand = _clean_folder_path(m.group(1))
                if cand:
                    dest = cand
                    break

    raw_create = exp.get("create_folder")
    if raw_create is None:
        raw_create = exp.get("create_folders")
    if raw_create is None:
        raw_create = sp_sub.get("create_folder")
    if raw_create is None:
        raw_create = sp_sub.get("create_folders")
    if isinstance(raw_create, bool):
        create_folder = raw_create
    else:
        create_folder = bool(_SP_CREATE_FOLDER_RE.search(text or "")) or bool(dest)

    raw_conflict = _clean(
        exp.get("conflict_policy")
        or exp.get("conflict_behavior")
        or sp_sub.get("conflict_policy")
        or sp_sub.get("conflict_behavior")
        or "rename"
    ).lower()
    if raw_conflict not in ("rename", "fail", "replace"):
        raw_conflict = "rename"
    confirmed_replace = bool(
        exp.get("confirmed_replace") is True or sp_sub.get("confirmed_replace") is True
    )
    if raw_conflict == "replace" and not confirmed_replace:
        raw_conflict = "rename"

    out: dict[str, Any] = {}
    if dest:
        out["destination_folder"] = dest
        out["create_folder"] = create_folder
        out["conflict_policy"] = raw_conflict
        if confirmed_replace:
            out["confirmed_replace"] = True
    elif isinstance(raw_create, bool) and raw_create:
        out["create_folder"] = True

    for key in ("drive_id", "parent_id", "site_id"):
        val = _clean(exp.get(key) or sp_sub.get(key) or "")
        if val:
            out[key] = val
    return out


def _extract_target_workbook(text: str) -> str:
    m_q = _XLSX_QUOTED_RE.search(text)
    if m_q:
        return _clean(m_q.group(1))
    # Search after the last preposition before ".xlsx" so verbs like "Append rows to" stay out.
    matches = list(_XLSX_PREP_RE.finditer(text))
    if matches:
        cand = _clean(matches[-1].group(1))
        # Trim any nested preposition if multiple appeared in the span.
        cand = re.sub(r"^(?:.*?\b(?:to|in|into|from|for)\s+(?:the\s+)?)", "", cand, flags=re.I)
        return _clean(cand)
    m_b = _XLSX_BARE_RE.search(text)
    return _clean(m_b.group(1)) if m_b else ""


def parse_workbook_operation(
    text: str,
    explicit: Optional[Mapping[str, Any]] = None,
    *,
    kind: str = "",
) -> dict[str, Any]:
    """Parse Excel workbook operation (create | add_sheet | append_rows), target workbook, and tab name."""
    exp = explicit if isinstance(explicit, Mapping) else {}
    cleaned = _clean(text)

    raw_op = _clean(exp.get("workbook_operation") or "").lower()
    if raw_op in ("add_tab", "new_tab", "new_sheet", "add_worksheet"):
        raw_op = "add_sheet"
    elif raw_op in ("append", "add_row", "add_rows", "append_row"):
        raw_op = "append_rows"
    elif raw_op not in WORKBOOK_OPERATIONS:
        raw_op = ""

    if not raw_op and cleaned:
        if _ADD_SHEET_RE.search(cleaned):
            raw_op = "add_sheet"
        elif _APPEND_ROWS_RE.search(cleaned):
            raw_op = "append_rows"

    target_wb = _clean(exp.get("target_workbook") or "")
    if not target_wb and cleaned:
        target_wb = _extract_target_workbook(cleaned)
        if target_wb and not raw_op and ("add " in cleaned.lower() or "append " in cleaned.lower()):
            raw_op = "add_sheet" if "tab" in cleaned.lower() or "sheet" in cleaned.lower() else "append_rows"

    sheet_name = _clean(exp.get("sheet_name") or exp.get("tab_name") or "")
    if not sheet_name and cleaned:
        m_sh = _SHEET_NAME_RE.search(cleaned)
        if m_sh:
            sheet_name = _clean(next((g for g in m_sh.groups() if g), ""))

    out: dict[str, Any] = {}
    if raw_op:
        out["workbook_operation"] = raw_op
    elif kind == "xlsx" or target_wb:
        out["workbook_operation"] = "create"
    if target_wb:
        out["target_workbook"] = target_wb
    if sheet_name:
        from operating.doc_excel_ops import sanitize_sheet_name
        out["sheet_name"] = sanitize_sheet_name(sheet_name)
    return out


def normalize_document_act(act: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a ``prepare_document`` act dict into clean structured document fields."""
    doc_raw = act.get("document") if isinstance(act.get("document"), Mapping) else {}
    merged_explicit = {**act, **doc_raw}
    about = _clean(doc_raw.get("about") or act.get("about") or act.get("title") or "")
    raw_title = _clean(doc_raw.get("title") or act.get("title") or "")
    raw_kind = _clean(doc_raw.get("kind") or act.get("kind") or "")
    source_path = _clean(
        doc_raw.get("source_path")
        or doc_raw.get("target_workbook_path")
        or doc_raw.get("template")
        or act.get("attach")
        or ""
    )
    combined_prompt = f"{raw_title} {about}".strip()
    wb_meta = parse_workbook_operation(combined_prompt, merged_explicit, kind=raw_kind)
    if wb_meta.get("workbook_operation") in ("add_sheet", "append_rows") or wb_meta.get("target_workbook"):
        raw_kind = raw_kind or "xlsx"
    kind, doc_type = _infer_kind_and_type(raw_kind, combined_prompt)
    if kind == "xlsx" and "workbook_operation" not in wb_meta:
        wb_meta["workbook_operation"] = "create"
    title = _derive_title(raw_title, about, doc_type)
    sp_meta = parse_sharepoint_destination(combined_prompt, merged_explicit)

    out: dict[str, Any] = {
        "kind": kind,
        "doc_type": doc_type,
        "title": title,
        "about": about or title,
    }
    if source_path:
        out["source_path"] = source_path
    if wb_meta:
        out.update(wb_meta)
    if sp_meta.get("destination_folder"):
        out["destination_folder"] = sp_meta["destination_folder"]
        out["create_folder"] = bool(sp_meta.get("create_folder", True))
        out["conflict_policy"] = sp_meta.get("conflict_policy", "rename")
        if sp_meta.get("confirmed_replace"):
            out["confirmed_replace"] = True
        out["sharepoint"] = dict(sp_meta)
    elif sp_meta.get("create_folder"):
        out["create_folder"] = True
    for key in ("formulas", "number_formats", "headers", "rows"):
        if key in doc_raw and doc_raw[key] is not None:
            out[key] = doc_raw[key]
        elif key in act and act[key] is not None:
            out[key] = act[key]
    return out


def detect_document_requests(
    matter: Mapping[str, Any],
    matter_text: str = "",
    *,
    rows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return normalized ``prepare_document`` specs from explicit session rows or clear matter asks."""
    found: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    def add(spec: Mapping[str, Any]) -> None:
        norm = normalize_document_act(spec)
        key = (norm["kind"], norm["title"].lower())
        if key not in seen_keys:
            seen_keys.add(key)
            found.append(norm)

    # 1. Explicit document requests returned by a prepared session or planner.
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for list_key in ("document_requests", "documents"):
            items = row.get(list_key)
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                for item in items:
                    if isinstance(item, Mapping):
                        add(item)
        for prop in row.get("proposals") or ():
            if not isinstance(prop, Mapping):
                continue
            tool = str(prop.get("tool") or "").strip().lower()
            if tool == "plx.prepare_document" or prop.get("act") == "prepare_document":
                args = prop.get("arguments") if isinstance(prop.get("arguments"), Mapping) else prop
                add(args)

    if found:
        return found

    # 2. Proactive detection from matter title, detail, and thread text.
    combined = "\n".join(
        _clean(part)
        for part in (matter.get("title"), matter.get("detail"), matter_text)
        if _clean(part)
    )
    if not combined:
        return []
    for kind, doc_type, pattern in _PROACTIVE_TRIGGERS:
        if pattern.search(combined):
            headline = _clean(matter.get("detail") or matter.get("title") or combined.splitlines()[0])
            add({"document": {"kind": kind, "about": headline}, "about": headline})
            break
    return found


def extract_grounded_facts(
    matter: Optional[Mapping[str, Any]] = None,
    matter_text: str = "",
    grounded_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Extract only grounded facts explicitly present in the matter or passed records."""
    m = matter or {}
    combined = "\n".join(
        _clean(part)
        for part in (m.get("title"), m.get("detail"), matter_text)
        if _clean(part)
    )
    order_refs = list(dict.fromkeys(
        re.sub(r"\s+", "-", match.group(1).strip().upper())
        for match in _ORDER_REF_RE.finditer(combined)
    ))
    quantities: list[dict[str, str]] = []
    seen_qty: set[str] = set()
    for match in _QTY_ITEM_RE.finditer(combined):
        qty = match.group(1).strip()
        item = _clean(match.group(2)).strip(" .,;:")
        # Skip plain calendar years or dates like "2026-11-25" or "2026 margin".
        if re.fullmatch(r"20\d{2}", qty) and item.lower().startswith(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "q1", "q2", "q3", "q4")):
            continue
        key = f"{qty}|{item.lower()}"
        if key not in seen_qty:
            seen_qty.add(key)
            quantities.append({"quantity": qty, "item": item})

    periods = list(dict.fromkeys(_clean(m_p.group(1)) for m_p in _PERIOD_RE.finditer(combined)))
    dates = list(dict.fromkeys(m_d.group(1) for m_d in _DATE_RE.finditer(combined)))
    counterpart = _clean(m.get("counterpart") or "")
    records = [dict(r) for r in grounded_records if isinstance(r, Mapping)]

    return {
        "order_refs": order_refs,
        "quantities": quantities,
        "periods": periods,
        "dates": dates,
        "counterpart": counterpart,
        "records": records,
    }


def _count_sheet_items(about: str, facts: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Derive grounded count-sheet scope rows from the request text (zero fabricated SKUs)."""
    records = facts.get("records") or ()
    if records:
        out: list[tuple[str, str]] = []
        for rec in records:
            label = _clean(rec.get("item") or rec.get("name") or rec.get("description") or "Grounded item")
            scope = _clean(rec.get("category") or rec.get("scope") or "Inventory")
            out.append((label, scope))
        return out

    m = _COUNT_SPLIT_RE.search(about)
    phrase = _clean(m.group(1) if m else about)
    # Split on " and " or "," when multiple categories are requested (e.g. "Dandylion packaging and finished goods").
    parts = [p.strip(" .,:;") for p in re.split(r"\s+and\s+|,\s*", phrase, flags=re.I) if p.strip(" .,:;")]
    if len(parts) >= 2:
        first_tokens = parts[0].split()
        prefix = first_tokens[0] if len(first_tokens) >= 2 and first_tokens[0][:1].isupper() else ""
        rows: list[tuple[str, str]] = []
        for idx, part in enumerate(parts):
            if idx == 0 and prefix:
                cat = " ".join(first_tokens[1:])
                rows.append((f"{prefix} - {cat.title()}", cat.title()))
            elif prefix and not part.lower().startswith(prefix.lower()):
                rows.append((f"{prefix} - {part.title()}", part.title()))
            else:
                rows.append((part.title(), "Requested Scope"))
        return rows
    return [(phrase.title() if phrase else "Requested Item", "Requested Scope")]


def _build_sharepoint_plan(norm: Mapping[str, Any], filename: str) -> Optional[dict[str, Any]]:
    """Build a structured, previewable SharePoint upload/folder plan for a prepared document."""
    dest_folder = str(norm.get("destination_folder") or "").strip()
    if not dest_folder:
        return None
    sp = norm.get("sharepoint") if isinstance(norm.get("sharepoint"), Mapping) else {}
    create_folder = bool(norm.get("create_folder", True))
    conflict_policy = str(norm.get("conflict_policy") or "rename")
    segments = [s for s in dest_folder.split("/") if s]
    folders_to_ensure = ["/".join(segments[: i + 1]) for i in range(len(segments))] if create_folder else []
    dest_path = f"{dest_folder}/{filename}"
    plan_out: dict[str, Any] = {
        "operation": "upload_document",
        "destination_folder": dest_folder,
        "destination_path": dest_path,
        "create_folder": create_folder,
        "folders_to_ensure": folders_to_ensure,
        "conflict_policy": conflict_policy,
        "filename": filename,
    }
    for key in ("drive_id", "parent_id", "site_id"):
        if sp.get(key):
            plan_out[key] = sp[key]
    if sp.get("drive_id"):
        from urllib.parse import quote
        from operating.autopilot_write_adapters import GRAPH
        drive_q = quote(str(sp["drive_id"]), safe="!-_")
        parent = str(sp.get("parent_id") or "root")
        parent_q = quote(parent, safe="!-_")
        path_q = "/".join(quote(s, safe="()") for s in segments)
        name_q = quote(filename, safe="()")
        prefix = f"root:/{path_q}/{name_q}" if parent == "root" else f"items/{parent_q}:/{path_q}/{name_q}"
        plan_out["destination_url"] = f"{GRAPH}/drives/{drive_q}/{prefix}"
    return plan_out


def _attach_studio_metadata(spec: dict[str, Any], norm: Mapping[str, Any]) -> dict[str, Any]:
    """Attach SharePoint destination and Excel workbook operation metadata to a document spec."""
    actual_kind = "docx" if spec["kind"] == "fill_form" else spec["kind"]
    target_wb = str(norm.get("target_workbook") or "").strip()
    filename = target_wb if (target_wb and spec["kind"] == "xlsx") else inf.file_name(spec["title"], actual_kind)

    if norm.get("source_path"):
        spec["source_path"] = norm["source_path"]
    if norm.get("workbook_operation"):
        spec["workbook_operation"] = norm["workbook_operation"]
    if target_wb:
        spec["target_workbook"] = target_wb
    if norm.get("sheet_name"):
        spec["sheet_name"] = norm["sheet_name"]
    for key in ("formulas", "number_formats"):
        if key in norm and norm[key] is not None:
            spec[key] = norm[key]

    preview_lines = list(spec.get("preview_lines") or ())
    if spec.get("workbook_operation") in ("add_sheet", "append_rows"):
        op = spec["workbook_operation"]
        tab = spec.get("sheet_name") or (spec["sheets"][0]["name"] if spec.get("sheets") else "Sheet1")
        wb_label = target_wb or filename
        preview_lines.append(f"Workbook operation: {op} on '{tab}' in {wb_label}")

    sp_plan = _build_sharepoint_plan(norm, filename)
    if sp_plan is not None:
        spec["destination_folder"] = sp_plan["destination_folder"]
        spec["create_folder"] = sp_plan["create_folder"]
        spec["conflict_policy"] = sp_plan["conflict_policy"]
        spec["sharepoint_plan"] = sp_plan
        folder_note = "create folders if missing" if sp_plan["create_folder"] else "existing folder only"
        preview_lines.append(
            f"SharePoint destination: {sp_plan['destination_path']} ({folder_note}, conflict: {sp_plan['conflict_policy']})"
        )
    spec["preview_lines"] = preview_lines
    return spec


def build_document_spec(
    act: Mapping[str, Any],
    *,
    matter: Optional[Mapping[str, Any]] = None,
    matter_text: str = "",
    grounded_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a deterministic, grounded document spec ready for ``instruct_files.make``."""
    norm = normalize_document_act(act)
    kind = norm["kind"]
    doc_type = norm["doc_type"]
    title = norm["title"]
    about = norm["about"]
    facts = extract_grounded_facts(matter, matter_text, grounded_records)
    grounded_list: list[str] = []
    if facts["counterpart"]:
        grounded_list.append(f"Counterpart: {facts['counterpart']}")
    if facts["order_refs"]:
        grounded_list.append("References: " + ", ".join(facts["order_refs"]))
    if facts["periods"]:
        grounded_list.append("Period: " + ", ".join(facts["periods"]))
    for q in facts["quantities"]:
        grounded_list.append(f"Requested quantity: {q['quantity']} {q['item']}")
    if not grounded_list:
        grounded_list.append(f"Request: {about}")

    placeholders_needed: list[str] = []
    spec: dict[str, Any] = {
        "kind": kind,
        "doc_type": doc_type,
        "title": title,
        "about": about,
        "grounded_facts": grounded_list,
    }

    if kind in ("xlsx", "csv"):
        explicit_headers = norm.get("headers")
        explicit_rows = norm.get("rows")
        if isinstance(explicit_headers, Sequence) and not isinstance(explicit_headers, (str, bytes)) \
                and isinstance(explicit_rows, Sequence) and not isinstance(explicit_rows, (str, bytes)):
            columns = [_clean(c) for c in explicit_headers]
            data_rows: list[list[str]] = []
            for r in explicit_rows:
                if isinstance(r, Mapping):
                    data_rows.append([_clean(r.get(c, "")) for c in columns])
                elif isinstance(r, Sequence) and not isinstance(r, (str, bytes)):
                    data_rows.append([_clean(v) for v in r])
            summary = f"Workbook sheet for {title} with structured grounded rows."
        elif doc_type == "count_sheet":
            columns = [
                "Item / Category",
                "SKU / Item No.",
                "Scope",
                "BC System Qty",
                "Physical Count",
                "Variance",
                "Location / Bin",
                "Notes",
            ]
            data_rows = []
            if facts["records"]:
                for rec in facts["records"]:
                    data_rows.append([
                        _clean(rec.get("item") or rec.get("name") or TBD_PLACEHOLDER),
                        _clean(rec.get("sku") or rec.get("item_no") or TBD_PLACEHOLDER),
                        _clean(rec.get("category") or rec.get("scope") or "Inventory"),
                        _clean(rec.get("system_qty") if rec.get("system_qty") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("physical_count") if rec.get("physical_count") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("variance") if rec.get("variance") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("location") or TBD_PLACEHOLDER),
                        _clean(rec.get("notes") or f"Requested: {about}"),
                    ])
            else:
                for label, scope in _count_sheet_items(about, facts):
                    data_rows.append([
                        label,
                        TBD_PLACEHOLDER,
                        scope,
                        TBD_PLACEHOLDER,
                        TBD_PLACEHOLDER,
                        TBD_PLACEHOLDER,
                        TBD_PLACEHOLDER,
                        f"Requested: {about}",
                    ])
                placeholders_needed.extend(["SKU / Item No.", "BC System Qty", "Physical Count", "Location / Bin"])
            summary = f"Count sheet for {title} with grounded scope rows and TBD inventory placeholders."
        elif doc_type == "pricing_sheet":
            columns = ["Item / SKU", "Description", "Quantity", "Unit Cost", "Unit Price", "Margin %", "Notes"]
            data_rows = []
            if facts["records"]:
                for rec in facts["records"]:
                    data_rows.append([
                        _clean(rec.get("sku") or TBD_PLACEHOLDER),
                        _clean(rec.get("description") or rec.get("item") or title),
                        _clean(rec.get("quantity") if rec.get("quantity") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("unit_cost") if rec.get("unit_cost") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("unit_price") if rec.get("unit_price") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("margin_pct") if rec.get("margin_pct") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("notes") or ""),
                    ])
            elif facts["quantities"]:
                for q in facts["quantities"]:
                    data_rows.append([
                        TBD_PLACEHOLDER,
                        q["item"],
                        q["quantity"],
                        TBD_PLACEHOLDER,
                        TBD_PLACEHOLDER,
                        TBD_PLACEHOLDER,
                        f"Requested: {about}",
                    ])
                placeholders_needed.extend(["Item / SKU", "Unit Cost", "Unit Price", "Margin %"])
            else:
                data_rows.append([
                    TBD_PLACEHOLDER,
                    title,
                    TBD_PLACEHOLDER,
                    TBD_PLACEHOLDER,
                    TBD_PLACEHOLDER,
                    TBD_PLACEHOLDER,
                    f"Requested: {about}",
                ])
                placeholders_needed.extend(["Item / SKU", "Quantity", "Unit Cost", "Unit Price", "Margin %"])
            summary = f"Pricing sheet for {title} with grounded request details and TBD cost/price columns."
        else:
            columns = ["Item", "Reference", "Quantity", "Status", "Owner", "Due Date", "Notes"]
            data_rows = []
            if facts["records"]:
                for rec in facts["records"]:
                    data_rows.append([
                        _clean(rec.get("item") or rec.get("name") or title),
                        _clean(rec.get("ref") or rec.get("order_no") or TBD_PLACEHOLDER),
                        _clean(rec.get("quantity") if rec.get("quantity") is not None else TBD_PLACEHOLDER),
                        _clean(rec.get("status") or TBD_PLACEHOLDER),
                        _clean(rec.get("owner") or TBD_PLACEHOLDER),
                        _clean(rec.get("due_date") or TBD_PLACEHOLDER),
                        _clean(rec.get("notes") or ""),
                    ])
            else:
                ref_val = facts["order_refs"][0] if facts["order_refs"] else TBD_PLACEHOLDER
                qty_val = f"{facts['quantities'][0]['quantity']} {facts['quantities'][0]['item']}" if facts["quantities"] else TBD_PLACEHOLDER
                due_val = facts["dates"][0] if facts["dates"] else TBD_PLACEHOLDER
                data_rows.append([title, ref_val, qty_val, TBD_PLACEHOLDER, TBD_PLACEHOLDER, due_val, about])
                placeholders_needed.extend(["Status", "Owner"])
            summary = f"Tracker sheet for {title} with grounded request fields and TBD slots."

        tab_name = str(norm.get("sheet_name") or "Sheet1")
        lines = [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
            *("| " + " | ".join(_clean(c).replace("|", "/") for c in row) + " |" for row in data_rows),
        ]
        spec["content"] = "\n".join(lines)
        spec["summary"] = _clean(summary)
        spec["table_columns"] = columns
        spec["table_rows"] = data_rows
        spec["sheets"] = [{"name": tab_name, "columns": columns, "row_count": len(data_rows)}]
        spec["preview_lines"] = [
            f"{tab_name}: {', '.join(columns)}",
            *(" | ".join(row[:4]) for row in data_rows[:5]),
        ]
        spec["placeholders_needed"] = list(dict.fromkeys(placeholders_needed))
        return _attach_studio_metadata(spec, norm)

    if kind == "pptx":
        period_str = ", ".join(facts["periods"]) if facts["periods"] else TBD_PLACEHOLDER
        counterpart_str = facts["counterpart"] or TBD_PLACEHOLDER
        table_rows: list[list[str]] = []
        if facts["records"]:
            for rec in facts["records"]:
                table_rows.append([
                    _clean(rec.get("item") or rec.get("sku") or rec.get("name") or title),
                    _clean(rec.get("revenue") if rec.get("revenue") is not None else TBD_PLACEHOLDER),
                    _clean(rec.get("cost") if rec.get("cost") is not None else TBD_PLACEHOLDER),
                    _clean(rec.get("margin") if rec.get("margin") is not None else TBD_PLACEHOLDER),
                ])
        else:
            table_rows.append([title, TBD_PLACEHOLDER, TBD_PLACEHOLDER, TBD_PLACEHOLDER])
            placeholders_needed.extend(["Revenue", "COGS / Cost", "Gross Margin"])

        slides = [
            {
                "title": title,
                "bullets": [
                    f"Objective: {about}",
                    f"Period: {period_str}",
                    f"Account / Counterpart: {counterpart_str}",
                ],
            },
            {
                "title": "Grounded Context",
                "bullets": list(grounded_list) + [
                    f"Live BC/financial figures required before distribution: {TBD_PLACEHOLDER}",
                ],
            },
            {
                "title": "Key Figures Summary",
                "bullets": [
                    "Populate live revenue, cost, and margin numbers from Business Central before sharing.",
                ],
                "table_columns": ["Segment / SKU", "Revenue", "Cost", "Gross Margin"],
                "table_rows": table_rows,
            },
            {
                "title": "Open Items and Next Steps",
                "bullets": [
                    "Confirm live BC sales and cost figures for the target period.",
                    "Review assumptions with owner before presenting.",
                    "Export final slides after TBD fields are verified.",
                ],
            },
        ]
        md_blocks: list[str] = []
        for sl in slides:
            md_blocks.append(f"# {sl['title']}")
            for b in sl["bullets"]:
                md_blocks.append(f"- {_clean(b)}")
            if sl.get("table_columns"):
                cols = sl["table_columns"]
                md_blocks.append("")
                md_blocks.append("| " + " | ".join(cols) + " |")
                md_blocks.append("| " + " | ".join("---" for _ in cols) + " |")
                for row in sl["table_rows"]:
                    md_blocks.append("| " + " | ".join(_clean(c).replace("|", "/") for c in row) + " |")
            md_blocks.append("")

        spec["content"] = "\n".join(md_blocks).strip()
        spec["summary"] = _clean(f"Presentation deck ({len(slides)} slides) for {title} with grounded scope and TBD data slots.")
        spec["slides"] = [{"title": s["title"], "bullets": s["bullets"]} for s in slides]
        spec["preview_lines"] = [f"Slide {i}: {s['title']}" for i, s in enumerate(slides, 1)]
        spec["placeholders_needed"] = list(dict.fromkeys(placeholders_needed))
        return _attach_studio_metadata(spec, norm)

    # Word (docx), PDF (pdf), or form_fill fallback checklist
    if doc_type == "purchase_order":
        po_ref = next((r for r in facts["order_refs"] if r.startswith("PO")), TBD_PLACEHOLDER)
        vendor = facts["counterpart"] or TBD_PLACEHOLDER
        req_date = facts["dates"][0] if facts["dates"] else TBD_PLACEHOLDER
        line_rows: list[list[str]] = []
        if facts["records"]:
            for idx, rec in enumerate(facts["records"], 1):
                line_rows.append([
                    str(idx),
                    _clean(rec.get("item") or rec.get("description") or title),
                    _clean(rec.get("quantity") if rec.get("quantity") is not None else TBD_PLACEHOLDER),
                    _clean(rec.get("uom") or "EA"),
                    _clean(rec.get("unit_cost") if rec.get("unit_cost") is not None else TBD_PLACEHOLDER),
                    _clean(rec.get("extended_total") if rec.get("extended_total") is not None else TBD_PLACEHOLDER),
                ])
        elif facts["quantities"]:
            for idx, q in enumerate(facts["quantities"], 1):
                line_rows.append([str(idx), q["item"], q["quantity"], "EA", TBD_PLACEHOLDER, TBD_PLACEHOLDER])
            placeholders_needed.extend(["Vendor", "Unit Cost", "Extended Total", "Requested Receipt Date"])
        else:
            line_rows.append(["1", title, TBD_PLACEHOLDER, "EA", TBD_PLACEHOLDER, TBD_PLACEHOLDER])
            placeholders_needed.extend(["Vendor", "Quantity", "Unit Cost", "Extended Total"])

        sections = [
            {
                "heading": "Order Header",
                "lines": [
                    f"PO Number: {po_ref}",
                    f"Vendor: {vendor}",
                    f"Requested Receipt Date: {req_date}",
                    f"Ship-To Location: {TBD_PLACEHOLDER}",
                ],
            },
            {
                "heading": "Grounded Request Context",
                "lines": list(grounded_list),
            },
            {
                "heading": "Confirmation Checklist",
                "lines": [
                    "Confirm vendor item number, unit cost, and lead time in Business Central.",
                    "Confirm delivery dock and receiving schedule before issuing PO.",
                ],
            },
        ]
        md = [
            f"# Purchase Order Draft: {title}",
            "",
            "## Order Header",
            *(f"- {ln}" for ln in sections[0]["lines"]),
            "",
            "## Line Items",
            "| Line | Item Description | Quantity | UOM | Unit Cost | Extended Total |",
            "| --- | --- | --- | --- | --- | --- |",
            *("| " + " | ".join(row) + " |" for row in line_rows),
            "",
            "## Grounded Request Context",
            *(f"- {ln}" for ln in sections[1]["lines"]),
            "",
            "## Confirmation Checklist",
            *(f"- {ln}" for ln in sections[2]["lines"]),
        ]
        summary = f"Draft Purchase Order document for {title} with grounded line items and TBD cost/vendor fields."
    elif doc_type == "quote":
        customer = facts["counterpart"] or TBD_PLACEHOLDER
        line_rows = []
        if facts["quantities"]:
            for idx, q in enumerate(facts["quantities"], 1):
                line_rows.append([str(idx), q["item"], q["quantity"], TBD_PLACEHOLDER, TBD_PLACEHOLDER])
        else:
            line_rows.append(["1", title, TBD_PLACEHOLDER, TBD_PLACEHOLDER, TBD_PLACEHOLDER])
        placeholders_needed.extend(["Unit Price", "Extended Total", "Valid Until Date"])
        sections = [
            {
                "heading": "Quote Summary",
                "lines": [
                    f"Customer: {customer}",
                    f"Valid Until: {TBD_PLACEHOLDER}",
                    f"Lead Time: {TBD_PLACEHOLDER}",
                ],
            },
            {"heading": "Grounded Request Context", "lines": list(grounded_list)},
            {
                "heading": "Review Before Sending",
                "lines": [
                    "Verify unit pricing and freight terms against Business Central price lists.",
                    "Confirm lead time with production planning.",
                ],
            },
        ]
        md = [
            f"# Customer Quote Draft: {title}",
            "",
            "## Quote Summary",
            *(f"- {ln}" for ln in sections[0]["lines"]),
            "",
            "## Proposed Line Items",
            "| Line | Item Description | Quantity | Unit Price | Extended Total |",
            "| --- | --- | --- | --- | --- |",
            *("| " + " | ".join(row) + " |" for row in line_rows),
            "",
            "## Grounded Request Context",
            *(f"- {ln}" for ln in sections[1]["lines"]),
            "",
            "## Review Before Sending",
            *(f"- {ln}" for ln in sections[2]["lines"]),
        ]
        summary = f"Draft quote document for {title} with grounded scope and TBD pricing placeholders."
    else:
        sections = [
            {"heading": "Request Overview", "lines": [f"Topic: {about}"]},
            {"heading": "Grounded Facts", "lines": list(grounded_list)},
            {
                "heading": "Items Requiring Verification",
                "lines": [
                    f"Confirm operational figures from Business Central and inventory records: {TBD_PLACEHOLDER}",
                ],
            },
        ]
        placeholders_needed.append("Operational figures")
        md = [
            f"# {title}",
            "",
            "## Request Overview",
            *(f"- {ln}" for ln in sections[0]["lines"]),
            "",
            "## Grounded Facts",
            *(f"- {ln}" for ln in sections[1]["lines"]),
            "",
            "## Items Requiring Verification",
            *(f"- {ln}" for ln in sections[2]["lines"]),
        ]
        summary = f"Prepared document for {title} built from grounded matter context."

    spec["content"] = "\n".join(md)
    spec["summary"] = _clean(summary)
    spec["sections"] = sections
    spec["preview_lines"] = [
        title,
        *(f"{sec['heading']}: {sec['lines'][0]}" for sec in sections if sec.get("lines")),
    ]
    spec["placeholders_needed"] = list(dict.fromkeys(placeholders_needed))
    return _attach_studio_metadata(spec, norm)


def _artifact_storage_dir(state_dir: Optional[Path], output_dir: Optional[Path]) -> Path:
    if output_dir is not None:
        base = Path(output_dir)
    elif state_dir is not None:
        base = Path(state_dir) / "assist-docs"
    else:
        base = Path(tempfile.gettempdir()) / "plx-assist-docs"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    keep_private(base, 0o700)
    return base


def _write_private_bytes(base_dir: Path, artifact_id: str, filename: str, data: bytes) -> Path:
    sub = base_dir / artifact_id
    sub.mkdir(parents=True, exist_ok=True, mode=0o700)
    keep_private(sub, 0o700)
    dest = sub / filename
    if not dest.exists():
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    return dest


def prepare_document_artifact(
    act: Mapping[str, Any],
    *,
    matter: Optional[Mapping[str, Any]] = None,
    matter_text: str = "",
    grounded_records: Sequence[Mapping[str, Any]] = (),
    state_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    screen: Optional[Callable[[str], Optional[str]]] = None,
) -> dict[str, Any]:
    """Generate a real downloadable artifact via ``instruct_files`` (or ``doc_fill``) and return metadata."""
    spec = build_document_spec(
        act,
        matter=matter,
        matter_text=matter_text,
        grounded_records=grounded_records,
    )
    if screen is not None:
        why = screen(spec["title"] + "\n" + spec["content"])
        if why:
            return {
                "kind": spec["kind"],
                "doc_type": spec["doc_type"],
                "title": spec["title"],
                "summary": "Document preparation withheld by content screen.",
                "download_ready": False,
                "refused": True,
                "reason": str(why),
                "preview_lines": [],
            }

    requested_kind = spec["kind"]
    actual_kind = "docx" if requested_kind == "fill_form" else requested_kind
    fallback_from = ""
    form_template_needed = requested_kind == "fill_form"
    form_fields: list[dict[str, Any]] = []
    workbook_op = str(spec.get("workbook_operation") or "")
    workbook_template_needed = (actual_kind == "xlsx" and workbook_op in ("add_sheet", "append_rows"))
    workbook_inspection: Optional[dict[str, Any]] = None
    data: Optional[bytes] = None

    # Optional real form-fill path when a readable template file is provided.
    source_path_str = str(spec.get("source_path") or "").strip()
    if requested_kind == "fill_form" and source_path_str:
        src_path = Path(source_path_str)
        if src_path.is_file():
            try:
                from operating import doc_fill as df
                raw_bytes = src_path.read_bytes()
                terms = df.load_terms()
                result = df.fill(raw_bytes, [], terms)
                data = result.data if result.data is not None else raw_bytes
                actual_kind = "pdf" if result.kind.startswith("pdf") else result.kind
                form_template_needed = False
                form_fields = [
                    f.as_dict() if hasattr(f, "as_dict") else {
                        "label": str(getattr(f, "label", "")),
                        "status": str(getattr(f, "status", "")),
                    }
                    for f in (result.fields or ())
                ]
            except Exception:  # noqa: BLE001 - fall back cleanly to structured checklist docx
                data = None

    # Optional non-destructive Excel workbook edit when an existing .xlsx file is provided.
    if actual_kind == "xlsx" and workbook_op in ("add_sheet", "append_rows") and source_path_str:
        src_wb = Path(source_path_str)
        if src_wb.is_file():
            from operating import doc_excel_ops as deo
            existing_bytes = src_wb.read_bytes()
            cols = list(spec.get("table_columns") or ())
            rows_data = list(spec.get("table_rows") or ())
            tab_name = str(spec.get("sheet_name") or deo.sanitize_sheet_name(spec["title"]))
            if workbook_op == "add_sheet":
                data = deo.add_sheet_to_workbook(
                    existing_bytes,
                    tab_name,
                    cols,
                    rows_data,
                    title=spec["title"],
                    formulas=spec.get("formulas"),
                    number_formats=spec.get("number_formats"),
                    if_exists="rename",
                )
            else:
                row_dicts = [
                    {cols[i]: r[i] for i in range(min(len(cols), len(r)))}
                    for r in rows_data
                ] if cols else rows_data
                data = deo.append_rows_to_sheet(
                    existing_bytes,
                    tab_name,
                    row_dicts,
                    match_headers=True,
                    create_if_missing=True,
                    headers=cols or None,
                    number_formats=spec.get("number_formats"),
                )
            workbook_template_needed = False
            workbook_inspection = deo.inspect_workbook(data)

    if data is None:
        try:
            data = inf.make(actual_kind, spec["content"])
        except inf.FileRefused as exc:
            if actual_kind == "pdf" and exc.code == "pdf_unavailable":
                fallback_from = "pdf"
                actual_kind = "docx"
                data = inf.make(actual_kind, spec["content"])
            else:
                raise

    digest = hashlib.sha256(data).hexdigest()
    artifact_id = "doc_" + digest[:16]
    target_wb = str(spec.get("target_workbook") or "").strip()
    filename = (
        inf.file_name(target_wb, actual_kind)
        if (target_wb and actual_kind == "xlsx" and workbook_op in ("add_sheet", "append_rows"))
        else inf.file_name(spec["title"], actual_kind)
    )
    base_dir = _artifact_storage_dir(state_dir, output_dir)
    dest = _write_private_bytes(base_dir, artifact_id, filename, data)

    meta: dict[str, Any] = {
        "artifact_id": artifact_id,
        "kind": actual_kind,
        "media_type": inf.MEDIA.get(actual_kind, "application/octet-stream"),
        "filename": filename,
        "title": spec["title"],
        "doc_type": spec["doc_type"],
        "summary": spec["summary"],
        "download_ready": True,
        "path": str(dest),
        "size_bytes": len(data),
        "sha256": digest,
        "preview_lines": list(spec.get("preview_lines") or ()),
        "grounded_facts": list(spec.get("grounded_facts") or ()),
        "placeholders_needed": list(spec.get("placeholders_needed") or ()),
    }
    if "sheets" in spec:
        meta["sheets"] = spec["sheets"]
    if "slides" in spec:
        meta["slides"] = spec["slides"]
    if "sections" in spec:
        meta["sections"] = spec["sections"]
    if fallback_from:
        meta["fallback_from"] = fallback_from
    if form_template_needed:
        meta["form_template_needed"] = True
    if form_fields:
        meta["form_fields"] = form_fields
    if workbook_op:
        meta["workbook_operation"] = workbook_op
    if target_wb:
        meta["target_workbook"] = target_wb
    if spec.get("sheet_name"):
        meta["sheet_name"] = spec["sheet_name"]
    if workbook_template_needed:
        meta["workbook_template_needed"] = True
    if workbook_inspection is not None:
        meta["workbook_sheets"] = workbook_inspection["sheet_names"]
    if spec.get("destination_folder"):
        meta["destination_folder"] = spec["destination_folder"]
        meta["create_folder"] = bool(spec.get("create_folder", True))
        meta["conflict_policy"] = spec.get("conflict_policy", "rename")
    if spec.get("sharepoint_plan"):
        meta["sharepoint_plan"] = spec["sharepoint_plan"]
    return meta


def prepare_plan_documents(
    plan: Mapping[str, Any],
    *,
    matter: Optional[Mapping[str, Any]] = None,
    matter_text: str = "",
    grounded_records: Sequence[Mapping[str, Any]] = (),
    state_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    screen: Optional[Callable[[str], Optional[str]]] = None,
) -> list[dict[str, Any]]:
    """Prepare downloadable artifacts for every ``prepare_document`` act in ``plan``."""
    acts = plan.get("acts") if isinstance(plan, Mapping) else ()
    if not isinstance(acts, Sequence) or isinstance(acts, (str, bytes)):
        return []
    artifacts: list[dict[str, Any]] = []
    for act in acts:
        if not isinstance(act, Mapping) or act.get("act") != "prepare_document":
            continue
        artifact = prepare_document_artifact(
            act,
            matter=matter,
            matter_text=matter_text,
            grounded_records=grounded_records,
            state_dir=state_dir,
            output_dir=output_dir,
            screen=screen,
        )
        artifacts.append(artifact)
    return artifacts


def prepare_documents_for_matter(
    matter: Mapping[str, Any],
    matter_text: str = "",
    *,
    rows: Sequence[Mapping[str, Any]] = (),
    grounded_records: Sequence[Mapping[str, Any]] = (),
    state_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    screen: Optional[Callable[[str], Optional[str]]] = None,
) -> list[dict[str, Any]]:
    """Detect and prepare any requested documents/spreadsheets/decks/PO drafts for a matter."""
    reqs = detect_document_requests(matter, matter_text, rows=rows)
    if not reqs:
        return []
    out: list[dict[str, Any]] = []
    for req in reqs:
        art = prepare_document_artifact(
            {"act": "prepare_document", "document": req, "about": req.get("about", "")},
            matter=matter,
            matter_text=matter_text,
            grounded_records=grounded_records,
            state_dir=state_dir,
            output_dir=output_dir,
            screen=screen,
        )
        if art.get("download_ready"):
            out.append(art)
    return out
