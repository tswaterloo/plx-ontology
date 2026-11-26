"""Prepared system writes wired to dry-run adapters (ASSIST specs 8.5 F8).

Translates a prepared session's or matter's system steps (``plx.prepare_write`` proposals or
plan ``system_write`` steps) targeting Business Central (``bc``), Calendar (``calendar``),
Planner (``planner``), SharePoint (``sharepoint``), or Mission Control (``mc``) into validated,
structured dry-run write intents with current (``before``) and proposed (``after``) state.

Safety invariants (enforced fail-closed):
* Zero live external writes: every adapter is instantiated with ``transport=None``; ``mode`` is
  always ``"dry_run"`` and ``perform()`` is never called.
* 0 automatic on class B: money, invoices, payments, PO/SO totals or prices, credit notes,
  charges, approvals, deletes, and external sends are always ``safety_class = "class_b"``,
  ``confirm_only = True``, and ``auto_allowed = False``. ``enforce_execution_policy`` refuses
  automatic execution on any class B or confirm-only intent even if fields were mutated.
* Reader permissions, people exclusion, door visibility, and sensitive-content screens are
  enforced before any ``before`` field is exposed; unreadable or denied records fail closed.

Reuses ``operating.autopilot_write_adapters``, ``operating.write_classes``,
``operating.prepared_writes``, ``operating.autopilot_write``, and
``operating.autopilot_write_readiness`` with no duplicate adapter or safety-class tables.

Evidence label: ``isolated-fixture`` (``operating/tests/test_assist_f8_writes.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import autopilot_write as aw
from operating import autopilot_write_adapters as wa
from operating import autopilot_write_readiness as readiness
from operating import prepared_writes as pw
from operating import write_classes as wc

SUPPORTED_SYSTEMS: tuple[str, ...] = readiness.SYSTEMS
DRY_RUN_ONLY_SYSTEMS: frozenset[str] = frozenset({"mc"})
CANONICAL_SYSTEMS: tuple[str, ...] = (
    "bc",
    "portal",
    "planner",
    "sharepoint",
    "calendar",
    "mc",
    "filemaker",
    "sage",
)

# Canonical (system, operation) -> write-adapter operation name ("" when no direct adapter op exists yet).
CANONICAL_TO_ADAPTER_OP: Mapping[tuple[str, str], str] = {
    ("bc", "create_purchase_order"): "purchase_order_create",
    ("bc", "update_purchase_order"): "purchase_order_update",
    ("bc", "delete_purchase_order"): "purchase_order_delete",
    ("bc", "add_purchase_order_line"): "purchase_order_line_create",
    ("bc", "update_purchase_order_line"): "purchase_order_line_update",
    ("bc", "comment_purchase_order"): "purchase_order_comment",
    ("bc", "attach_purchase_order"): "purchase_order_attach",
    ("bc", "create_sales_order"): "sales_order_create",
    ("bc", "update_sales_order"): "sales_order_update",
    ("bc", "delete_sales_order"): "sales_order_delete",
    ("bc", "add_sales_order_line"): "sales_order_line_create",
    ("bc", "update_sales_order_line"): "sales_order_line_update",
    ("bc", "add_sales_order_charge"): "sales_order_charge_line",
    ("bc", "comment_sales_order"): "sales_order_comment",
    ("bc", "attach_sales_order"): "sales_order_attach",
    ("bc", "add_sales_invoice_charge"): "sales_invoice_charge_line",
    ("bc", "attach_sales_invoice"): "sales_invoice_attach",
    ("bc", "create_quote"): "",
    ("bc", "update_quote"): "",
    ("bc", "record_receipt"): "",
    ("bc", "update_inventory"): "",
    ("bc", "record_qc"): "",
    ("portal", "update_order"): "sales-order.update",
    ("portal", "update_purchase_order"): "purchase-order.update",
    ("portal", "create_ship_release"): "ship-release.create",
    ("portal", "update_ship_release"): "ship-release.update",
    ("portal", "add_note"): "activity.note",
    ("portal", "create_contact"): "customer-contact.create",
    ("portal", "update_contact"): "customer-contact.update",
    ("portal", "create_location"): "customer-location.create",
    ("portal", "update_location"): "customer-location.update",
    ("planner", "create_task"): "task_create",
    ("planner", "update_task"): "task_update",
    ("sharepoint", "upload_document"): "file_upload",
    ("sharepoint", "replace_document"): "file_replace",
    ("sharepoint", "create_list_item"): "list_item_create",
    ("sharepoint", "update_list_item"): "list_item_update",
    ("calendar", "schedule_meeting"): "event_create",
    ("calendar", "update_meeting"): "event_update",
    ("calendar", "respond_meeting"): "event_respond",
    ("mc", "add_note"): "task_note",
    ("mc", "update_task"): "task_update",
    ("mc", "decide_approval"): "approval_decide",
    ("filemaker", "create_record"): "record_create",
    ("filemaker", "update_record"): "record_edit",
}

_ADAPTER_OPS_BY_SYSTEM: dict[str, frozenset[str]] = {}
for _cls in wa.ADAPTER_TYPES:
    _existing = set(_ADAPTER_OPS_BY_SYSTEM.get(_cls.system, frozenset()))
    _existing.update(getattr(_cls, "operations", {}).keys())
    _ADAPTER_OPS_BY_SYSTEM[_cls.system] = frozenset(_existing)

_SYSTEM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("planner", re.compile(r"\b(?:microsoft\s+|ms\s+)?planner\b", re.I)),
    ("bc", re.compile(r"\b(?:microsoft\s+)?(?:dynamics\s*365\s*)?(?:business\s*central|d365(?:\s*bc)?|navision|bc)\b", re.I)),
    ("portal", re.compile(r"\b(?:petra\s+|customer\s+|operations?\s+|ops\s+|order\s+|web\s+)?portal\b", re.I)),
    ("sharepoint", re.compile(r"\b(?:microsoft\s+|ms\s+)?(?:share\s*point(?:\s+online)?|one\s*drive)\b", re.I)),
    ("calendar", re.compile(r"\b(?:outlook\s+|microsoft\s+|ms\s+|teams\s+|exchange\s+|office\s*365\s+)?calendar\b", re.I)),
    ("mc", re.compile(r"\b(?:mission[\s_-]*control|mc)\b", re.I)),
    ("filemaker", re.compile(r"\b(?:file\s*maker(?:\s+pro)?|fm\s+pro)\b", re.I)),
    ("sage", re.compile(r"\bsage(?:\s*(?:50|100|300|intacct|accounting))?\b", re.I)),
)

_GENERIC_CONTEXT_TOKENS: frozenset[str] = frozenset({
    "bc", "erp", "d365", "dynamics", "dynamics 365", "business central", "microsoft",
    "ms", "online", "legacy", "sandbox", "production", "prod", "live", "app",
    "system", "platform", "portal", "planner", "sharepoint", "calendar", "mc",
    "mission control", "filemaker", "sage", "read-only", "read only",
})

_PARENS_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")
_COMBINED_OP_RE = re.compile(r"\s*(?:::|->)\s*")
_SEP_CONTEXT_RE = re.compile(r"^\s*([^:\-]+?)\s*(?::|\s+-\s+)\s*(.+?)\s*$")
_PLAN_SUFFIX_RE = re.compile(r"\s+(?:plan|board|planner)$", re.I)
_FOLDER_SUFFIX_RE = re.compile(r"\s+(?:folder|library|site|drive)$", re.I)
_SNAKE_BAD_RE = re.compile(r"[^a-z0-9]+")


def _clean_text(raw: Any) -> str:
    return re.sub(r"\s+", " ", str(raw or "").replace("\u2014", " ").replace("\u2013", " ")).strip()


def _match_canonical_system(text: str) -> str:
    cleaned = _clean_text(text)
    if not cleaned:
        return ""
    low = cleaned.lower()
    if low in CANONICAL_SYSTEMS:
        return low
    for canonical, pat in _SYSTEM_PATTERNS:
        if pat.search(cleaned):
            return canonical
    return ""


def _to_snake(text: str) -> str:
    return _SNAKE_BAD_RE.sub("_", _clean_text(text).lower()).strip("_")


def _normalize_operation(system: str, raw_op: str, about: str = "", record: str = "") -> str:
    op_clean = _clean_text(raw_op)
    op_low = op_clean.lower()
    op_snake = _to_snake(op_clean)
    hint = _clean_text(f"{op_clean} {about} {record}").lower()

    if system == "planner":
        if op_snake in ("create_task", "task_create"):
            return "create_task"
        if op_snake in ("update_task", "task_update"):
            return "update_task"
        if any(tok in op_low for tok in ("update", "edit", "complete", "close", "move", "reassign", "due", "progress", "check")):
            return "update_task"
        if any(tok in op_low for tok in ("create", "add", "new", "schedule", "assign", "log", "make", "task")):
            return "create_task"
        if not op_clean:
            if any(tok in hint for tok in ("update", "complete", "close", "move", "reassign", "due date")):
                return "update_task"
            if any(tok in hint for tok in ("task", "planner", "schedule", "assign", "follow up")):
                return "create_task"
        return op_snake

    if system == "bc":
        exact = {
            "create_purchase_order": "create_purchase_order",
            "purchase_order_create": "create_purchase_order",
            "po_create": "create_purchase_order",
            "update_purchase_order": "update_purchase_order",
            "purchase_order_update": "update_purchase_order",
            "po_update": "update_purchase_order",
            "delete_purchase_order": "delete_purchase_order",
            "purchase_order_delete": "delete_purchase_order",
            "add_purchase_order_line": "add_purchase_order_line",
            "purchase_order_line_create": "add_purchase_order_line",
            "update_purchase_order_line": "update_purchase_order_line",
            "purchase_order_line_update": "update_purchase_order_line",
            "comment_purchase_order": "comment_purchase_order",
            "purchase_order_comment": "comment_purchase_order",
            "attach_purchase_order": "attach_purchase_order",
            "purchase_order_attach": "attach_purchase_order",
            "create_sales_order": "create_sales_order",
            "sales_order_create": "create_sales_order",
            "so_create": "create_sales_order",
            "update_sales_order": "update_sales_order",
            "sales_order_update": "update_sales_order",
            "so_update": "update_sales_order",
            "delete_sales_order": "delete_sales_order",
            "sales_order_delete": "delete_sales_order",
            "add_sales_order_line": "add_sales_order_line",
            "sales_order_line_create": "add_sales_order_line",
            "update_sales_order_line": "update_sales_order_line",
            "sales_order_line_update": "update_sales_order_line",
            "add_sales_order_charge": "add_sales_order_charge",
            "sales_order_charge_line": "add_sales_order_charge",
            "comment_sales_order": "comment_sales_order",
            "sales_order_comment": "comment_sales_order",
            "attach_sales_order": "attach_sales_order",
            "sales_order_attach": "attach_sales_order",
            "add_sales_invoice_charge": "add_sales_invoice_charge",
            "sales_invoice_charge_line": "add_sales_invoice_charge",
            "attach_sales_invoice": "attach_sales_invoice",
            "sales_invoice_attach": "attach_sales_invoice",
            "create_quote": "create_quote",
            "sales_quote_create": "create_quote",
            "quote_create": "create_quote",
            "update_quote": "update_quote",
            "sales_quote_update": "update_quote",
            "record_receipt": "record_receipt",
            "purchase_receipt_create": "record_receipt",
            "warehouse_receipt_create": "record_receipt",
            "update_inventory": "update_inventory",
            "inventory_adjustment": "update_inventory",
            "record_qc": "record_qc",
        }
        if op_snake in exact:
            return exact[op_snake]
        src = op_low or hint
        is_po = bool(re.search(r"\b(?:purchase\s*orders?|po\b|po[\s#-]*\d)", src))
        is_so = bool(re.search(r"\b(?:sales\s*orders?|so\b|so[\s#-]*\d)", src))
        is_inv = bool(re.search(r"\b(?:sales\s*invoice|invoice)\b", src))
        is_quote = bool(re.search(r"\b(?:quote|quotation)\b", src))
        is_receipt = bool(re.search(r"\b(?:receipt|receive|receiving)\b", src))
        is_qc = bool(re.search(r"\b(?:qc|quality\s*(?:hold|release|check)|lot\s*release)\b", src))
        is_stock = bool(re.search(r"\b(?:inventory|stock|cycle\s*count|adjustment)\b", src))
        is_line = bool(re.search(r"\bline\b", src))
        is_charge = bool(re.search(r"\b(?:charge|freight|fee)\b", src))
        is_comment = bool(re.search(r"\b(?:comment|note)\b", src))
        is_attach = bool(re.search(r"\battach", src))
        is_delete = bool(re.search(r"\b(?:delete|cancel|remove|void)\b", src))
        is_create = bool(re.search(r"\b(?:create|new|draft|enter|raise|issue|add|open|prepare|generate)\b", src))
        is_update = bool(re.search(r"\b(?:update|edit|modify|change|revise|adjust|set|move)\b", src))

        if is_quote:
            return "update_quote" if (is_update and not is_create) else "create_quote"
        if is_receipt:
            return "record_receipt"
        if is_qc:
            return "record_qc"
        if is_po:
            if is_delete:
                return "delete_purchase_order"
            if is_attach:
                return "attach_purchase_order"
            if is_comment:
                return "comment_purchase_order"
            if is_line:
                return "update_purchase_order_line" if (is_update and not is_create) else "add_purchase_order_line"
            return "update_purchase_order" if (is_update and not is_create) else "create_purchase_order"
        if is_so:
            if is_delete:
                return "delete_sales_order"
            if is_attach:
                return "attach_sales_order"
            if is_charge:
                return "add_sales_order_charge"
            if is_comment:
                return "comment_sales_order"
            if is_line:
                return "update_sales_order_line" if (is_update and not is_create) else "add_sales_order_line"
            return "update_sales_order" if (is_update and not is_create) else "create_sales_order"
        if is_inv:
            if is_attach:
                return "attach_sales_invoice"
            if is_charge:
                return "add_sales_invoice_charge"
        if is_stock:
            return "update_inventory"
        return op_snake

    if system == "portal":
        exact_portal = {
            "update_order": "update_order",
            "sales_order_update": "update_order",
            "update_sales_order": "update_order",
            "update_purchase_order": "update_purchase_order",
            "purchase_order_update": "update_purchase_order",
            "create_ship_release": "create_ship_release",
            "ship_release_create": "create_ship_release",
            "update_ship_release": "update_ship_release",
            "ship_release_update": "update_ship_release",
            "add_note": "add_note",
            "activity_note": "add_note",
            "create_contact": "create_contact",
            "customer_contact_create": "create_contact",
            "update_contact": "update_contact",
            "customer_contact_update": "update_contact",
            "create_location": "create_location",
            "customer_location_create": "create_location",
            "update_location": "update_location",
            "customer_location_update": "update_location",
        }
        if op_snake in exact_portal:
            return exact_portal[op_snake]
        src = op_low or hint
        if "release" in src or "shipment" in src:
            return "update_ship_release" if any(w in src for w in ("update", "edit", "change", "reschedule")) else "create_ship_release"
        if "contact" in src:
            return "update_contact" if any(w in src for w in ("update", "edit", "change")) else "create_contact"
        if "location" in src or "address" in src:
            return "update_location" if any(w in src for w in ("update", "edit", "change")) else "create_location"
        if "note" in src or "comment" in src or "activity" in src:
            return "add_note"
        if re.search(r"\b(?:purchase\s*order|po\b)", src):
            return "update_purchase_order"
        if any(w in src for w in ("order", "status", "date", "update", "eta")):
            return "update_order"
        return op_snake

    if system == "sharepoint":
        exact_sp = {
            "upload_document": "upload_document",
            "file_upload": "upload_document",
            "upload_file": "upload_document",
            "replace_document": "replace_document",
            "file_replace": "replace_document",
            "replace_file": "replace_document",
            "create_list_item": "create_list_item",
            "list_item_create": "create_list_item",
            "update_list_item": "update_list_item",
            "list_item_update": "update_list_item",
        }
        if op_snake in exact_sp:
            return exact_sp[op_snake]
        src = op_low or hint
        if "list" in src or "row" in src or "item" in src:
            return "update_list_item" if any(w in src for w in ("update", "edit", "change")) else "create_list_item"
        if "replace" in src or "overwrite" in src:
            return "replace_document"
        if any(w in src for w in ("upload", "file", "doc", "save", "post", "add", "store")):
            return "upload_document"
        return op_snake

    if system == "calendar":
        exact_cal = {
            "schedule_meeting": "schedule_meeting",
            "event_create": "schedule_meeting",
            "create_event": "schedule_meeting",
            "create_meeting": "schedule_meeting",
            "book_meeting": "schedule_meeting",
            "update_meeting": "update_meeting",
            "event_update": "update_meeting",
            "reschedule_meeting": "update_meeting",
            "respond_meeting": "respond_meeting",
            "event_respond": "respond_meeting",
        }
        if op_snake in exact_cal:
            return exact_cal[op_snake]
        src = op_low or hint
        if any(w in src for w in ("accept", "decline", "tentative", "respond", "rsvp")):
            return "respond_meeting"
        if any(w in src for w in ("update", "reschedule", "move", "change", "edit")):
            return "update_meeting"
        if any(w in src for w in ("schedule", "book", "create", "new", "invite", "meeting", "event", "call")):
            return "schedule_meeting"
        return op_snake

    if system == "mc":
        exact_mc = {
            "add_note": "add_note",
            "task_note": "add_note",
            "update_task": "update_task",
            "task_update": "update_task",
            "decide_approval": "decide_approval",
            "approval_decide": "decide_approval",
        }
        if op_snake in exact_mc:
            return exact_mc[op_snake]
        src = op_low or hint
        if any(w in src for w in ("approve", "reject", "approval", "gate", "decide")):
            return "decide_approval"
        if any(w in src for w in ("label", "priority", "title", "update", "edit")):
            return "update_task"
        if any(w in src for w in ("note", "comment", "progress")):
            return "add_note"
        return op_snake

    if system == "filemaker":
        if op_snake in ("record_create", "create_record"):
            return "create_record"
        if op_snake in ("record_edit", "update_record", "edit_record"):
            return "update_record"
        src = op_low or hint
        if any(w in src for w in ("create", "new", "add")):
            return "create_record"
        if any(w in src for w in ("edit", "update", "change", "modify")):
            return "update_record"
        return op_snake

    return op_snake


def canonicalize_system_write(
    system_spec: Any,
    *,
    operation: str = "",
    record: str = "",
    about: str = "",
) -> dict[str, Any]:
    """Normalize a messy LLM or free-text system_write descriptor into canonical keys.

    Examples:
    - ``"Microsoft Planner (QA/QC Task Schedule plan) :: create task"`` ->
      ``system="planner"``, ``operation="create_task"``, ``canonical_key="planner::create_task"``,
      ``adapter_operation="task_create"``, ``plan_name="QA/QC Task Schedule"``.
    - ``{"system": "QA/QC Task Schedule (Planner)", "operation": "add task"}`` ->
      ``system="planner"``, ``operation="create_task"``, ``plan_name="QA/QC Task Schedule"``.
    - ``{"system": "Dynamics 365 Business Central", "operation": "Create Purchase Order"}`` ->
      ``system="bc"``, ``operation="create_purchase_order"``, ``canonical_key="bc::create_purchase_order"``,
      ``adapter_operation="purchase_order_create"``.
    """
    extra: dict[str, str] = {}
    if isinstance(system_spec, Mapping):
        raw_sys = _clean_text(system_spec.get("system") or "")
        raw_op = _clean_text(operation or system_spec.get("operation") or system_spec.get("action") or "")
        clean_rec = _clean_text(record or system_spec.get("record") or system_spec.get("target_handle") or "")
        for meta_key in ("plan_name", "folder_name", "company_name"):
            val = _clean_text(system_spec.get(meta_key) or "")
            if val:
                extra[meta_key] = val
    else:
        raw_sys = _clean_text(system_spec or "")
        raw_op = _clean_text(operation or "")
        clean_rec = _clean_text(record or "")

    orig_sys = raw_sys
    orig_op = raw_op

    # Split combined "System :: operation" or "System -> operation" strings.
    if "::" in raw_sys or "->" in raw_sys:
        parts = _COMBINED_OP_RE.split(raw_sys, maxsplit=1)
        raw_sys = parts[0].strip()
        if len(parts) > 1 and parts[1].strip() and not raw_op:
            raw_op = parts[1].strip()

    canonical_sys = ""
    context_label = ""

    # Handle parenthetical forms: "Planner (QA/QC Task Schedule)" OR "QA/QC Task Schedule (Planner)".
    m_paren = _PARENS_RE.match(raw_sys)
    if m_paren:
        outer, inner = m_paren.group(1).strip(), m_paren.group(2).strip()
        outer_sys = _match_canonical_system(outer)
        inner_sys = _match_canonical_system(inner)
        if outer_sys and not inner_sys:
            canonical_sys = outer_sys
            context_label = inner
        elif inner_sys and not outer_sys:
            canonical_sys = inner_sys
            context_label = outer
        elif outer_sys:
            canonical_sys = outer_sys
            context_label = inner

    # Handle "Planner - QA/QC Task Schedule" or "SharePoint: Quality Docs".
    if not canonical_sys:
        m_sep = _SEP_CONTEXT_RE.match(raw_sys)
        if m_sep:
            left, right = m_sep.group(1).strip(), m_sep.group(2).strip()
            left_sys = _match_canonical_system(left)
            right_sys = _match_canonical_system(right)
            if left_sys and not right_sys:
                canonical_sys = left_sys
                context_label = right
            elif right_sys and not left_sys:
                canonical_sys = right_sys
                context_label = left

    if not canonical_sys:
        canonical_sys = _match_canonical_system(raw_sys)

    if not canonical_sys:
        # Try inferring from combined record/about only when raw_sys was empty.
        if not raw_sys:
            canonical_sys = _match_canonical_system(f"{raw_op} {about}")
        if not canonical_sys:
            canonical_sys = _to_snake(raw_sys) if raw_sys else ""

    # Attach extracted context_label to the right structured metadata key.
    if context_label and context_label.lower() not in _GENERIC_CONTEXT_TOKENS:
        if canonical_sys == "planner" and "plan_name" not in extra:
            cleaned_plan = _PLAN_SUFFIX_RE.sub("", context_label).strip()
            if cleaned_plan and cleaned_plan.lower() not in _GENERIC_CONTEXT_TOKENS:
                extra["plan_name"] = cleaned_plan
        elif canonical_sys == "sharepoint" and "folder_name" not in extra:
            cleaned_folder = _FOLDER_SUFFIX_RE.sub("", context_label).strip()
            if cleaned_folder and cleaned_folder.lower() not in _GENERIC_CONTEXT_TOKENS:
                extra["folder_name"] = cleaned_folder
        elif canonical_sys == "bc" and "company_name" not in extra:
            extra["company_name"] = context_label
        elif not clean_rec:
            clean_rec = context_label

    if canonical_sys == "planner" and "plan_name" in extra:
        extra["plan_name"] = _PLAN_SUFFIX_RE.sub("", extra["plan_name"]).strip()

    canonical_op = _normalize_operation(canonical_sys, raw_op, about=about, record=clean_rec)
    adapter_op = CANONICAL_TO_ADAPTER_OP.get((canonical_sys, canonical_op), "")
    if not adapter_op and raw_op and raw_op in _ADAPTER_OPS_BY_SYSTEM.get(canonical_sys, frozenset()):
        adapter_op = raw_op

    out: dict[str, Any] = {
        "system": canonical_sys,
        "operation": canonical_op,
        "record": clean_rec,
    }
    if canonical_sys and canonical_op:
        out["canonical_key"] = f"{canonical_sys}::{canonical_op}"
    if adapter_op:
        out["adapter_operation"] = adapter_op
    for k in ("plan_name", "folder_name", "company_name"):
        if extra.get(k):
            out[k] = extra[k]
    if orig_sys and orig_sys.lower() != canonical_sys:
        out["raw_system"] = orig_sys
    if orig_op and orig_op.lower() != canonical_op:
        out["raw_operation"] = orig_op
    return out

# Operations and payload fields that always force class_b (confirm-only, auto_allowed=False)
# on top of write_classes.write_class (money, invoices, payments, PO totals/prices, credit
# notes, approvals, deletes, and external sends).
_ALWAYS_CLASS_B_OPS: frozenset[str] = frozenset({
    "approval_decide",
    "purchase_order_delete",
    "sales_order_delete",
    "sales_order_charge_line",
    "sales_invoice_charge_line",
    "purchase_order_create",
    "sales_order_create",
    "purchase_order_update",
    "sales_order_update",
    "purchase_order_line_create",
    "purchase_order_line_update",
    "sales_order_line_create",
    "sales_order_line_update",
    "file_replace",
})
_MONEY_FIELD_NAMES: frozenset[str] = frozenset({
    "unitprice", "directunitcost", "amount", "totalamount", "total", "price", "cost",
    "discountpercent", "discountamount", "creditamount", "paymentamount", "invoiceamount",
    "usd", "currencycode",
})
_SENSITIVE_MARKINGS: frozenset[str] = frozenset({
    "pay", "payroll", "hr", "compensation", "personal", "private", "restricted",
})
# Operations that mutate or decide an existing record and therefore require a readable current
# state so ``before`` is always sourced from the system/store, never invented.
_REQUIRES_CURRENT_READ: frozenset[str] = frozenset({
    "purchase_order_update",
    "sales_order_update",
    "purchase_order_line_update",
    "sales_order_line_update",
    "purchase_order_delete",
    "sales_order_delete",
    "event_update",
    "event_respond",
    "task_update",
    "list_item_update",
    "file_replace",
    "approval_decide",
})

ReadCurrent = Callable[[pw.Person, Mapping[str, Any]], Optional[Mapping[str, Any]]]
MayCheck = Callable[[pw.Person, Mapping[str, Any]], Optional[str]]
DoorCheck = Callable[[Mapping[str, Any]], bool]
ScreenCheck = Callable[[str], Optional[str]]


def dry_run_adapters(environ: Optional[Mapping[str, str]] = None) -> dict[str, wa.Adapter]:
    """Build dry-run-only adapter instances (``transport=None`` on every adapter) so no
    prepared write check or preview can ever send an external request."""
    env = dict(os.environ if environ is None else environ)
    adapters: dict[str, wa.Adapter] = {}
    for cls in wa.ADAPTER_TYPES:
        if cls.system in SUPPORTED_SYSTEMS:
            adapters[cls.kind] = cls(None, environ=env)
    return adapters


def _normalize_person(owner: pw.Person | str) -> pw.Person:
    if isinstance(owner, pw.Person):
        return owner
    text = str(owner or "").strip().lower()
    if not text:
        raise pw.Refused(["A signed-in reader identity is required to prepare a write."], 403)
    return pw.Person(ids=(text,), full=False)


def _extract_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """Accept either a raw system step or a ``plx.prepare_write`` proposal envelope."""
    if not isinstance(step, Mapping):
        raise pw.Refused(["System step must be an object."])
    if "arguments" in step and isinstance(step.get("arguments"), Mapping):
        tool = str(step.get("tool") or "")
        if tool and tool != pw.TOOL_NAME:
            raise pw.Refused([f"Unsupported write tool {tool}."])
        raw = dict(step["arguments"])
    else:
        raw = dict(step)
    raw_system = str(raw.get("system") or "").strip()
    raw_operation = str(raw.get("operation") or raw.get("action") or "").strip()
    system = raw_system.lower()
    operation = raw_operation
    target = dict(raw.get("target") or {}) if isinstance(raw.get("target"), Mapping) else {}
    change_raw = raw.get("change") if "change" in raw else raw.get("payload")
    change = dict(change_raw or {}) if isinstance(change_raw, Mapping) else {}
    precondition = (dict(raw.get("precondition") or {})
                    if isinstance(raw.get("precondition"), Mapping) else {})
    prior = dict(raw.get("prior") or {}) if isinstance(raw.get("prior"), Mapping) else {}
    people = [str(p) for p in (raw.get("people") or ()) if isinstance(p, str) and p.strip()]
    reason = str(raw.get("reason") or raw.get("about") or "")
    title = str(raw.get("title") or reason)
    target_handle = str(raw.get("target_handle") or raw.get("record") or "").strip()

    if system not in CANONICAL_SYSTEMS or (
        operation and operation not in _ADAPTER_OPS_BY_SYSTEM.get(system, frozenset())
    ):
        canon = canonicalize_system_write(
            {"system": raw_system, "operation": raw_operation, "record": target_handle},
            about=reason,
        )
        if canon["system"] in CANONICAL_SYSTEMS:
            system = canon["system"]
            if operation not in _ADAPTER_OPS_BY_SYSTEM.get(system, frozenset()):
                if canon.get("adapter_operation"):
                    operation = canon["adapter_operation"]
            if not target_handle and canon.get("record"):
                target_handle = canon["record"]
    out: dict[str, Any] = {
        "system": system,
        "operation": operation,
        "target": target,
        "change": change,
        "people": people,
        "reason": reason,
        "title": title,
    }
    if precondition:
        out["precondition"] = precondition
    if prior:
        out["prior"] = prior
    if target_handle:
        out["target_handle"] = target_handle
    return out


def _has_money_fields(value: Any) -> bool:
    if isinstance(value, Mapping):
        for k, v in value.items():
            key = str(k).lower().replace("_", "").replace("-", "")
            if key in _MONEY_FIELD_NAMES and v is not None:
                return True
            if _has_money_fields(v):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_money_fields(item) for item in value)
    return False


def classify_safety(action: Mapping[str, Any], *, item: Optional[Mapping[str, Any]] = None,
                    rules: Optional[Mapping[str, Any]] = None, workflow: str = "",
                    known_domains: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Return ``("class_a" | "class_b", reasons)`` for ``action``.

    Reuses ``write_classes.write_class`` and promotes any money-bearing payload, invoice,
    payment, credit note, approval, delete, or external send to ``"class_b"`` fail-closed.
    Class C operations (no write path) are refused.
    """
    base = wc.write_class(action, item=item, rules=rules, workflow=workflow,
                          known_domains=known_domains)
    if base == "C":
        raise pw.Refused([f"{action.get('system')}: class C has no write adapter (entry sheet only)."])
    reasons: list[str] = []
    if base == "B":
        reasons.append("write_classes:B")
    op = str(action.get("operation") or "").strip().lower()
    if op in _ALWAYS_CLASS_B_OPS:
        reasons.append(f"confirm_only_operation:{op}")
    if any(tok in op for tok in ("delete", "approval", "invoice", "payment", "credit", "charge")):
        reasons.append(f"sensitive_operation:{op}")
    if _has_money_fields(action.get("payload") or {}):
        reasons.append("money_field_present")
    if action.get("kind") == "mc_approval":
        reasons.append("approval_requires_confirmation")
    if reasons:
        return "class_b", list(dict.fromkeys(reasons))
    return "class_a", []


def readiness_of(system: str, *, state: Optional[Path] = None,
                 environ: Optional[Mapping[str, str]] = None,
                 readiness_map: Optional[Mapping[str, Any]] = None,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """Honest adapter readiness for ``system`` from the readiness probe or injected map."""
    found = readiness_map
    if found is None and state is not None:
        found = readiness.read(Path(state), os.environ if environ is None else environ, now=now)
    systems = (found or {}).get("systems") if isinstance(found, Mapping) else None
    entry = systems.get(system) if isinstance(systems, Mapping) else None
    if not isinstance(entry, Mapping):
        return {
            "status": "unprobed",
            "live_flag": False,
            "dry_run_only": True,
            "missing": ["no recent write readiness probe"],
        }
    status = str(entry.get("status") or "blocked")
    missing = [str(m) for m in (entry.get("missing") or ()) if m]
    live_flag = bool(entry.get("live_flag"))
    if system in DRY_RUN_ONLY_SYSTEMS:
        # Mission Control stays dry-run only in F8 regardless of any local flag.
        if status == "ready-live":
            status = "ready-dry-run"
        return {
            "status": status,
            "live_flag": False,
            "dry_run_only": True,
            "missing": missing,
        }
    return {
        "status": status,
        "live_flag": live_flag,
        "dry_run_only": status != "ready-live",
        "missing": missing,
    }


def _target_handle(system: str, operation: str, target: Mapping[str, Any],
                   explicit: str, got: Optional[Mapping[str, Any]]) -> str:
    if explicit:
        return explicit
    if isinstance(got, Mapping) and got.get("handle"):
        return str(got["handle"])
    parts = [system]
    for key in ("document_id", "line_id", "record_id", "task_id", "gate_id",
                "event_id", "site_id", "list_id", "item_id", "drive_id", "parent_id"):
        val = target.get(key)
        if val:
            parts.append(f"{key}={val}")
    if len(parts) == 1:
        parts.append(operation or "new")
    return ":".join(parts)


def _check_record_visibility(got: Mapping[str, Any], *, door: Optional[DoorCheck],
                             screen: Optional[ScreenCheck]) -> None:
    if got.get("denied"):
        raise pw.Refused(["Target record is not readable by this user."], 403)
    if got.get("unreadable"):
        raise pw.Refused(["Target record cannot be read now, so before state cannot be shown."], 422)
    markings = {str(m).strip().lower() for m in (got.get("markings") or ()) if str(m).strip()}
    if door is not None:
        if not door(got):
            raise pw.Refused(["Target record did not pass the reader access door."], 403)
    elif markings & _SENSITIVE_MARKINGS:
        raise pw.Refused(["Target record carries sensitive markings and cannot be exposed without door clearance."], 403)
    if screen is not None:
        blob = json.dumps({"name": got.get("name"), "values": got.get("values")},
                          sort_keys=True, default=str)
        why = screen(blob)
        if why:
            raise pw.Refused([f"Target record withheld by screen ({why})."], 403)


def _build_before_after(action: Mapping[str, Any], got: Optional[Mapping[str, Any]]
                        ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    op = str(action.get("operation") or "")
    payload = action.get("payload") if isinstance(action.get("payload"), Mapping) else {}
    values = (got or {}).get("values") if isinstance((got or {}).get("values"), Mapping) else {}
    if op.endswith("_delete"):
        before = {str(k): pw._plain(v, str(k)) for k, v in values.items()}
        after: dict[str, Any] = {"deleted": True}
        diff = [{"field": k, "before": v, "after": None} for k, v in before.items()]
        diff.append({"field": "deleted", "before": False, "after": True})
        return before, after, diff
    if action.get("kind") == "mc_approval":
        before = {
            "status": pw._plain(values.get("status", "pending"), "status"),
            "requested_by": pw._plain((got or {}).get("requested_by"), "requested_by"),
        }
        after = {
            "decision": action.get("recommendation"),
            "scope": pw._plain(payload.get("scope"), "scope"),
        }
        diff = [
            {"field": "status", "before": before["status"], "after": action.get("recommendation")},
            {"field": "scope", "before": None, "after": after["scope"]},
        ]
        return before, after, diff
    before = {}
    after = {}
    diff = []
    for field, val in payload.items():
        key = str(field)
        prev = values.get(key) if values else None
        before[key] = pw._plain(prev, key)
        after[key] = pw._plain(val, key)
        diff.append({"field": key, "before": before[key], "after": after[key]})
    return before, after, diff


def _intent_id(owner: str, system: str, operation: str, target_handle: str,
               after: Mapping[str, Any]) -> str:
    seed = json.dumps(
        {"owner": owner, "system": system, "operation": operation,
         "target_handle": target_handle, "after": after},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return "awi_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def build_write_intent(
    step: Mapping[str, Any],
    owner: pw.Person | str,
    *,
    read_current: Optional[ReadCurrent] = None,
    may: Optional[MayCheck] = None,
    guard: Any = None,
    door: Optional[DoorCheck] = None,
    screen: Optional[ScreenCheck] = None,
    state: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    readiness_map: Optional[Mapping[str, Any]] = None,
    known_domains: Iterable[str] = (),
    rules: Optional[Mapping[str, Any]] = None,
    workflow: str = "",
    origin: str = "assist",
    citations: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Validate and translate one prepared system step into a structured dry-run write intent.

    Never performs an external write (adapters are always created with ``transport=None``).
    Raises ``prepared_writes.Refused`` on any schema, permission, exclusion, visibility, or
    unsupported-system failure.
    """
    person = _normalize_person(owner)
    owner_id = person.email or person.ids[0]
    req = _extract_step(step)
    system = req["system"]
    operation = req["operation"]
    explicit_handle = req.pop("target_handle", "")

    if system in pw.NEVER:
        raise pw.Refused([pw.NEVER[system]])
    if system not in SUPPORTED_SYSTEMS:
        raise pw.Refused([
            f"Unsupported target system {system or '(none)'}: expected one of "
            f"{', '.join(SUPPORTED_SYSTEMS)}."
        ])

    # Screen proposed text before any adapter work or record read.
    if screen is not None:
        proposed_blob = json.dumps({"change": req["change"], "reason": req["reason"]},
                                   sort_keys=True, default=str)
        why_screen = screen(proposed_blob)
        if why_screen:
            raise pw.Refused([f"Proposed write withheld by screen ({why_screen})."], 403)

    env = dict(os.environ if environ is None else environ)
    adapters = dry_run_adapters(env)

    # Probe the action shape so we can check reader permission before reading current state.
    probe_item = pw.item_of(req, owner_id, "awi_probe", origin, citations)
    probe_actions = aw.actions_of(probe_item)
    if len(probe_actions) != 1 or probe_actions[0].get("kind") not in aw.KINDS:
        raise pw.Refused([f"{system}: unsupported operation {operation or '(none)'}."])
    probe_action = probe_actions[0]

    # Fail-closed reader permission check BEFORE reading any target record.
    if not person.full and probe_action.get("kind") not in pw.OWN:
        if may is None:
            raise pw.Refused([
                f"Cannot verify your write access for {system}: nothing is prepared."
            ], 403)
        denied = may(person, probe_action)
        if denied:
            raise pw.Refused([f"You cannot make this {system} change: {denied}."], 403)

    # Read current state (fail-closed on unreadable / denied / sensitive records).
    got: Optional[Mapping[str, Any]] = None
    if read_current is not None:
        try:
            raw_got = read_current(person, probe_action)
        except Exception as exc:  # noqa: BLE001 - fail closed on any read error
            raise pw.Refused([
                f"Current record could not be read from {system} ({type(exc).__name__})."
            ], 422) from None
        if isinstance(raw_got, Mapping):
            _check_record_visibility(raw_got, door=door, screen=screen)
            got = dict(raw_got)

    needs_read = operation in _REQUIRES_CURRENT_READ or bool(req.get("prior"))
    if needs_read and got is None:
        raise pw.Refused([
            f"Current record state from {system} is required before preparing {operation}."
        ], 422)

    requester: Optional[str] = None
    if system == "mc" and operation == "approval_decide":
        requester = str((got or {}).get("requested_by") or "").strip().lower() or None
        if requester is None:
            raise pw.Refused(["Mission Control gate requester could not be read."], 422)
        scope = (got or {}).get("scope")
        if not isinstance(scope, Mapping) or not scope.get("kind"):
            raise pw.Refused(["Mission Control gate shows no scope to decide."], 422)
        req = {**req, "change": {**dict(req.get("change") or {}), "scope": dict(scope)}}
    elif got is not None:
        req, read_problems = pw.Desk._as_read(req, got)
        if read_problems:
            raise pw.Refused(read_problems, 422)

    # Build the final executor item and action, then validate against the dry-run adapter.
    item = pw.item_of(req, owner_id, "awi_item", origin, citations, requested_by=requester)
    actions = aw.actions_of(item)
    if len(actions) != 1 or actions[0].get("kind") not in aw.KINDS:
        raise pw.Refused([f"{system}: malformed action for {operation}."])
    action = actions[0]
    if (system == "mc" and operation == "task_update" and got is not None
            and ("addLabels" in (action.get("payload") or {})
                 or "removeLabels" in (action.get("payload") or {}))):
        vals = got.get("values") if isinstance(got.get("values"), Mapping) else {}
        if "labels" in vals:
            pre = dict(action.get("precondition") or {})
            prior = dict(pre.get("prior") or {})
            prior.setdefault("labels", vals["labels"])
            pre["prior"] = prior
            action = {**action, "precondition": pre}
    adapter = adapters.get(str(action.get("kind")))
    if adapter is None:
        raise pw.Refused([f"No dry-run adapter for {system} ({action.get('kind')})."])

    # People exclusion check (fail-closed: unavailable guard refuses).
    all_people = (
        aw._addresses(item, action)
        + [str(p) for p in adapter.people(action)]
        + [str(p) for p in (got or {}).get("people") or () if p]
    )
    excl_reason = aw.excluded(guard, owner_id, all_people, known_domains)
    if excl_reason:
        raise pw.Refused([f"Write refused by people exclusion ({excl_reason})."], 403)

    # Validate every field against the real adapter schema (transport is None: pure dry-run).
    try:
        request_preview = adapter.request(action)
        undo_preview = adapter.undo_request(action, request_preview)
    except wa.AdapterRefused as exc:
        raise pw.Refused(list(exc.problems), 422) from None
    except (KeyError, TypeError, ValueError) as exc:
        raise pw.Refused([f"{system}: invalid write payload ({type(exc).__name__})."], 422) from None

    # Validate prior/before consistency when prior values were supplied.
    before_rows: list[dict[str, Any]] = []
    before_problems = pw.Desk._before(action, got, False, before_rows)
    if before_problems:
        raise pw.Refused(before_problems, 422)

    # Classify safety and enforce confirm-only / 0 automatic on class B.
    safety_class, safety_reasons = classify_safety(
        action, item=item, rules=rules, workflow=workflow, known_domains=known_domains
    )
    ready = readiness_of(system, state=state, environ=env, readiness_map=readiness_map, now=now)
    proposal_only = bool(request_preview.get("proposal_only"))
    if safety_class == "class_b":
        confirm_only = True
        auto_allowed = False
    else:
        confirm_only = bool(proposal_only or ready["dry_run_only"])
        auto_allowed = bool(not confirm_only and ready["status"] == "ready-live")

    before, after, diff = _build_before_after(action, got)
    handle = _target_handle(system, operation, action.get("target") or {}, explicit_handle, got)
    intent_id = _intent_id(owner_id, system, operation, handle, after)

    return {
        "intent_id": intent_id,
        "system": system,
        "action": operation,
        "kind": str(action.get("kind")),
        "target_handle": handle,
        "target": pw._plain(dict(action.get("target") or {})),
        "record_name": (got or {}).get("name"),
        "before": before,
        "after": after,
        "diff": diff,
        "safety_class": safety_class,
        "safety_reasons": safety_reasons,
        "confirm_only": confirm_only,
        "auto_allowed": auto_allowed,
        "mode": "dry_run",
        "readiness": ready,
        "proposal_only": proposal_only,
        "reason": req["reason"],
        "request_preview": pw._plain(request_preview),
        "undo_note": adapter.undo_note(),
        "undo_preview": pw._plain(undo_preview) if undo_preview is not None else None,
        "prepared_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }


def enforce_execution_policy(
    intent: Mapping[str, Any],
    *,
    auto: bool = False,
    confirmed_by: Optional[str] = None,
    known_domains: Iterable[str] = (),
) -> None:
    """Fail-closed execution policy check.

    * Refuses automatic execution (``auto=True``) on ANY class B, money-bearing,
      confirm-only, or dry-run-only intent (``0 automatic on class B``).
    * Re-derives safety classification from the intent's system, action, and after fields
      so a tampered intent cannot bypass confirm-only protection.
    * Refuses live execution without a named ``confirmed_by`` when ``confirm_only`` holds.
    """
    system = str(intent.get("system") or "").strip().lower()
    operation = str(intent.get("action") or intent.get("operation") or "").strip()
    after = intent.get("after") if isinstance(intent.get("after"), Mapping) else {}
    recheck_action = {
        "system": system,
        "operation": operation,
        "kind": intent.get("kind") or aw.WRITE_SYSTEMS.get(system, ""),
        "payload": after,
        "target": intent.get("target") or {},
    }
    derived_class, _ = classify_safety(recheck_action, known_domains=known_domains)
    declared_class = str(intent.get("safety_class") or "")
    effective_class = "class_b" if (derived_class == "class_b" or declared_class != "class_a") else "class_a"

    if auto:
        if effective_class != "class_a":
            raise pw.Refused([
                f"Automatic execution is forbidden on {effective_class} writes "
                f"({system}.{operation} is confirm-only)."
            ], 403)
        if intent.get("confirm_only") is not False or intent.get("auto_allowed") is not True:
            raise pw.Refused([
                f"Automatic execution is forbidden: {system}.{operation} requires confirmation."
            ], 403)
        readiness_info = intent.get("readiness") if isinstance(intent.get("readiness"), Mapping) else {}
        if readiness_info.get("dry_run_only") is not False or readiness_info.get("status") != "ready-live":
            raise pw.Refused([
                f"Automatic execution is forbidden: {system} adapter is not ready-live."
            ], 403)
    else:
        if (effective_class == "class_b" or intent.get("confirm_only")) and not str(confirmed_by or "").strip():
            raise pw.Refused([
                f"Explicit confirmation by a named person is required for {system}.{operation}."
            ], 403)


def prepare_session_writes(
    proposals: Sequence[Mapping[str, Any]],
    owner: pw.Person | str,
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate a prepared session's system write proposals into ``(intents, refusals)``.

    Non-system proposals (e.g. ``mail`` or ``teams`` drafts) are skipped here because they are
    handled by the message draft path. Any refusal is returned in ``refusals`` with its plain
    reason rather than dropping silently or crashing the session.
    """
    intents: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    for raw in proposals:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("tool") or "").strip().lower() == "plx.prepare_document":
            continue
        args = raw.get("arguments") if isinstance(raw.get("arguments"), Mapping) else raw
        raw_sys = str((args or {}).get("system") or "").strip()
        system = raw_sys.lower()
        if system in pw.DRAFT_SYSTEMS:
            continue
        if system and system not in CANONICAL_SYSTEMS:
            canon_sys = canonicalize_system_write(raw_sys).get("system") or system
            if canon_sys in CANONICAL_SYSTEMS:
                system = canon_sys
        try:
            intents.append(build_write_intent(raw, owner, **kwargs))
        except pw.Refused as exc:
            refusals.append({
                "system": system,
                "action": str((args or {}).get("operation") or (args or {}).get("action") or ""),
                "problems": list(exc.problems),
                "status": exc.status,
            })
    return intents, refusals
