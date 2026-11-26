"""Home's unified Needs-you read: the reader's Assist matters of every age, every late purchase
order, alerts routed to them, and (for the owner only) new feedback and unresolved Ask/Genie
gap alerts, merged server-side into one ranked list.

Items strictly older than 90 days (by due date, or by last activity when undated) are folded into
a separate ``older_items`` group so nothing is ever capped or dropped. Mission Control approvals
are excluded because they are decided in Mission Control itself.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as day_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote, unquote

from . import models as m
from .page_core import (
    clean_alert_label,
    clean_enum,
    clean_money_amount,
    clean_person,
    clean_subject,
    clean_text,
    clip_words,
    fix_po_prefix,
)
from .presentation import counted_words
from .serving import object_url

LOG = logging.getLogger(__name__)
PAGE = 100

# guard-ok: display labels for typed source values; no meaning is guessed from these words.
SOURCE_WORDS = {
    "work": "Open work",
    "mail": "Mail",
    "teams": "Teams",
    "mc": "Mission Control",
    "planner": "Planner",
    "alerts": "Alerts",
    "pos": "POs",
    "feedback": "Feedback",
    "gaps": "Gaps",
    "system": "System",
}
# guard-ok: display labels for typed kind values; no meaning is guessed from these words.
KIND_WORDS = {
    "reply": "Reply",
    "approve": "Approve",
    "question": "Question",
    "task": "Task",
    "waiting": "Waiting",
    "alert": "Alert",
    "late_po": "Late PO",
    "routine": "Routine",
    "feedback": "Feedback",
    "gap": "Gap",
}
# guard-ok: next_act is the matter store's typed action enum; this maps those typed values to the reply kind.
REPLY_ACTS = {"reply", "forward", "attach", "new_message"}
MC_APPROVAL_EVENT = "mc_approval_pending"
MC_APPROVAL_TYPE = "MCApproval"
LATE_PO_EVENT = "po_late"
OLDER_NEEDS_AFTER_DAYS = 90
STALE_ALERT_DAYS = 30
STALE_MC_TASK_DAYS = 21
FRESH_ACTIVITY_DAYS = 14
STALE_DUE_BEFORE_OPENED_DAYS = 7
LEGACY_ZOMBIE_DAYS = 365
FINANCIALS_EVENTS = frozenset({"price_below_cost", "customer_price_drop", "customer_demand_drop", "price_below_typical"})
HOLD_EVENTS = frozenset({"lot_on_hold", "quality_hold_aging", "qc_hold", "batch_hold", "order_hold"})
QC_HOLD_EVENTS = frozenset({"quality_hold", "qc_hold", "lot_on_hold", "batch_hold", "quality_hold_aging"})
_QC_ROLE_RE = re.compile(r"\b(?:qa\s*/\s*qc|qa|qc|quality|lab(?:oratory)?|regulatory)\b", re.I)  # guard-ok: org-chart QA/QC/lab/regulatory department/title check for QC hold routing
_NOREPLY_SENDER_RE = re.compile(  # guard-ok: RFC/operational automated no-reply mailbox addresses excluded from reply-owed items
    r"(?:^(?:no-?reply|do-?not-?reply|updates|newsletter|notifications?|microsoftstore|"
    r"mailer-daemon|postmaster|bounces?|digest|automated|account-security-noreply|security-noreply)@"
    r"|@(?:microsoftstore\.com|(?:email|e|mail|notifications)\.microsoft\.com)$)",
    re.IGNORECASE,
)
_COS_SYSTEM_RE = re.compile(  # guard-ok: operational system monitoring subjects from cos@petrasoap.com excluded from human reply queue
    r"\b(?:health|sync|monitor(?:ing)?|digest|cron|pipeline|ci\s+red|merge[- ]class|relay|"
    r"stale\s+views?|unavailable|status\s+report|on\s+staging|storage[- ]finalize|"
    r"gha\s+dispatch|work[- ]routing)\b",
    re.IGNORECASE,
)
_HEX24_RE = re.compile(r"^[0-9a-f]{24}$")
# guard-ok: display badges for typed hold and financial event values; no meaning is guessed from these words.
HOLD_BADGES = {
    "qc_hold": "QC hold",
    "lot_on_hold": "Batch hold",
    "batch_hold": "Batch hold",
    "order_hold": "Order hold",
    "quality_hold_aging": "Quality hold",
}
FINANCIALS_BADGES = {
    "customer_price_drop": "Price drop",
    "price_below_cost": "Below cost",
    "customer_demand_drop": "Demand drop",
    "price_below_typical": "Below typical price",
}
_BELOW_COST_PCT_RE = re.compile(r"\bpaid\s+(\d+(?:\.\d+)?)\s*%\s+of\s+cost\b", re.IGNORECASE)
COST_SUSPECT_PCT = 20.0   # >5x revenue (matches operating.pg.alerts.COST_WRONG_TIMES)
COST_VERIFY_PCT = 50.0    # >2x revenue: flag unit cost check so staff are not misled


def _iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _moment(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, day_time.min, tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        got = datetime.fromisoformat(text)
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _et_now() -> tuple[datetime, date]:
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - a host without tzdata: UTC is the stated fallback
        local = now
    return now, local.date()


def _part(source: str, status: str, *, total: Optional[int] = None, note: str = "",
          as_of: Any = None) -> m.HomeNeedPart:
    named = {
        "ok": "ok",
        "partial": "partial",
        "stale": "stale",
        "off": "off",
        "read": "ok",
        "not_connected": "failed",
        "refused": "failed",
    }.get(status, "failed")
    return m.HomeNeedPart(
        source=source,
        label=SOURCE_WORDS.get(source, source),
        status=named,
        note=note or None,
        total=total,
        as_of=_iso(as_of),
    )


_OBJECT_HREF_RE = re.compile(r"^/object/([^/?#]+)/([^/?#]+)")
_ONE_SEG_OBJECT_HREF_RE = re.compile(r"^/object/([^/?#]+)/?$")
_STILL_OPEN_PREFIX_RE = re.compile(
    r"^Still open\?\s*Nothing has happened on it since it was due\s+[^.\n]+?\.\s*",
    re.IGNORECASE,
)
_BARE_DATE_HOLD_STUB_RE = re.compile(
    r"^(?:\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{4})?\.\s*)?(?:QC hold or fail|Quality hold|QC hold|Batch hold|Order hold)\.?\s*$",
    re.IGNORECASE,
)
_MC_TASK_STUB_RE = re.compile(r"^Mission Control task\s+[A-Z0-9_-]+\.?\s*$", re.IGNORECASE)
_NO_REPLY_HISTORY_PAREN_RE = re.compile(
    r"\s*\((?:no typical reply time on record yet|nothing in your mail says how long[^)]*)\)",
    re.IGNORECASE,
)
_NO_REPLY_HISTORY_STANDALONE_RE = re.compile(
    r"^(?:no typical reply time on record yet|nothing in your mail says how long[^.\n]*)\.?$",
    re.IGNORECASE,
)
_SCOPE_OWNER_RE = re.compile(
    r"(?:^|\s+)(This is on [^.\n]+?'s list\s*,\s*not yours\.?(?:\s*Only you can see this\.?)?)\s*$",
    re.IGNORECASE,
)
_SCOPE_ONLY_YOU_RE = re.compile(r"(?:^|\s+)(Only you can see this\.?)\s*$", re.IGNORECASE)
_WAIT_TITLE_RE = re.compile(r"^No answer yet from\s+(.+)$", re.IGNORECASE)
_COMMITMENT_PREFIX_RE = re.compile(r"^You said\s*:\s*", re.IGNORECASE)
_BEFORE_DATE_TAIL_RE = re.compile(
    r"\s+(?:before|by)\s+(?:[A-Z][a-z]{2,8}\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}\s+[A-Z][a-z]{2,8}(?:\s+\d{4})?|\d{4}-\d{2}-\d{2})\.?$",
    re.IGNORECASE,
)
_HOLD_TITLE_PREFIX_RE = re.compile(
    r"^(?:QC hold or fail|Quality hold|QC hold|Batch hold|Order hold)\s*:\s*",
    re.IGNORECASE,
)
_HOLD_LOT_STATUS_RE = re.compile(
    r"^(Lot\s+[A-Za-z0-9-]+)(?:\s*\(([^)]+)\))?\s+is\s+(rejected|failed|on[- ]hold)"
    r"(?:\s*;\s*(?:it\s+holds\s+up\s+)?(\d+)\s+open\s+orders?(?:\(s\))?(?:\s+affected)?)?\.?$",
    re.IGNORECASE,
)
_HOLD_ON_HOLD_ORDERS_RE = re.compile(
    r"\s+is\s+(rejected|failed|on[- ]hold)\s*;\s*(?:it\s+holds\s+up\s+)?(\d+)\s+open\s+orders?(?:\(s\))?(?:\s+affected)?\.?$",
    re.IGNORECASE,
)
_HOLD_ON_HOLD_END_RE = re.compile(r"\s+is\s+(rejected|failed|on[- ]hold)\.?$", re.IGNORECASE)
_HOLD_LEAD_SENTENCE_RE = re.compile(
    r"^Lot\s+\S+(?:\s*\([^)]+\))?\s+(?:is\s+)?(?:rejected|failed|on[- ]hold)"
    r"(?:\s*[;·]\s*(?:it\s+holds\s+up\s+)?\d+\s+open\s+orders?(?:\(s\))?(?:\s+affected)?)?\.?\s*",
    re.IGNORECASE,
)
_TITLE_LEAD_STRIP_RE = re.compile(
    r"^(?:(?:re|fw|fwd)\s*:\s*)+|^(?:you said|waiting on [^:]+|no answer yet from [^:]+|qc hold or fail|quality hold|qc hold|batch hold|order hold)\s*:\s*",
    re.IGNORECASE,
)
_NORM_WORD_RE = re.compile(r"[^a-z0-9]+")
_CODE_TOKEN_RE = re.compile(r"\b(?:[a-z]*\d+[a-z0-9-]*)\b")
# guard-ok: English grammatical stop words used only for Jaccard similarity deduplication, never for semantic routing.
_DEDUPE_STOPWORDS = frozenset({
    "the", "a", "an", "to", "for", "of", "on", "in", "is", "are", "was", "were",
    "and", "or", "with", "from", "by", "at", "it", "this", "that", "be", "has", "have",
})
# guard-ok: English pronouns and reporting verbs stripped only when comparing same-second promise/conversation duplicates.
_EVENT_DEDUPE_STOPWORDS = _DEDUPE_STOPWORDS | frozenset({
    "he", "she", "they", "him", "her", "his", "their", "them", "you", "your", "we", "our",
    "will", "would", "can", "could", "should", "may", "might", "had", "been", "being",
    "says", "said", "trying", "try", "going", "about", "into", "still", "also", "just",
})
_SPEAKER_LEAD_RE = re.compile(
    r"^(?:you\s+said\s*:\s*|"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:says|said|has|will|committed\s+to|promised\s+to|is\s+trying\s+to)\s+"
    r"(?:(?:he|she|they)\s+(?:is|was|are|will)\s+)?(?:trying\s+to\s+)?)",
    re.IGNORECASE,
)
_SAME_EVENT_WINDOW_SECONDS = 120.0
_RECORD_LABELS = {
    "Batch": "Open lot",
    "Lot": "Open lot",
    "SalesOrder": "Open order",
    "PurchaseOrder": "Open PO",
    "Customer": "Open customer",
    "Supplier": "Open supplier",
    "MCTask": "Open task",
    "PlannerTask": "Open task",
    "Task": "Open task",
    "Product": "Open product",
    "Invoice": "Open invoice",
    "Shipment": "Open shipment",
}


def _parse_object_href(href: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract ``(object_id, object_type, canonical_href)`` from an ``/object/...`` app URL."""
    text = str(href or "").strip()
    if not text.startswith("/object/"):
        return None, None, None
    two = _OBJECT_HREF_RE.match(text)
    if two:
        otype = unquote(two.group(1)).strip()
        oid = unquote(two.group(2)).strip()
        if otype and oid:
            return oid, otype, object_url(otype, oid)
    one = _ONE_SEG_OBJECT_HREF_RE.match(text)
    if one:
        oid = unquote(one.group(1)).strip()
        if ":" in oid and "://" not in oid:
            otype, rest = oid.split(":", 1)
            if otype and otype[0:1].isupper() and rest:
                return oid, otype, object_url(otype, oid)
    return None, None, None


def _record_ref(values: Iterable[Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(object id, type, app path) of the first typed ontology object id in ``values``.
    Accepts both canonical (``Type:source:key``) and short (``Type:key``) ontology IDs
    as well as ``/object/<Type>/<id>`` links; rejects URI schemes and lowercase prefixes."""
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if text.startswith("/object/"):
            oid, otype, url = _parse_object_href(text)
            if oid and otype and url:
                return oid, otype, url
            continue
        if "://" in text or text.startswith(("http:", "https:", "mail:", "teams:")):
            continue
        if ":" in text:
            otype, rest = text.split(":", 1)
            if otype and otype[0:1].isupper() and rest:
                return text, otype, object_url(otype, text)
    return None, None, None


def _object_type_of(value: Any) -> str:
    """The type prefix of a typed object id, whether canonical (``Type:source:key``) or short
    (``Type:key``); never a URI scheme."""
    text = str(value or "")
    if text.startswith(("http:", "https:", "mail:", "teams:")):
        return ""
    head, sep, rest = text.partition(":")
    return head if sep and rest else ""


def _mc_approval_matter(item: Mapping[str, Any]) -> bool:
    """True when typed facts identify a Mission Control approval. Mission Control approvals are
    decided in Mission Control and stay out of Workbench Home lists."""
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    refs = [detail.get("approval")]
    if item.get("kind_of_matter") == "approval":
        refs.extend(detail.get("records") or ())
    if any(_object_type_of(ref) == MC_APPROVAL_TYPE for ref in refs):
        return True
    return MC_APPROVAL_EVENT in (
        item.get("event_type"),
        item.get("alert_kind"),
        item.get("kind"),
        detail.get("event_type"),
        detail.get("alert_kind"),
        detail.get("autopilot"),
    )


def _mc_approval_alert(item: Any) -> bool:
    """The same typed exclusion for a briefing item."""
    return (
        str(getattr(item, "event_type", "")) == MC_APPROVAL_EVENT
        or str(getattr(item, "alert_kind", "")) == MC_APPROVAL_EVENT
        or str(getattr(item, "object_type", "")) == MC_APPROVAL_TYPE
        or _object_type_of(getattr(item, "object_id", "")) == MC_APPROVAL_TYPE
    )


def _late_po_matter(item: Mapping[str, Any]) -> bool:
    """True when a typed matter fact says this matter is itself the late purchase order."""
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    return (
        item.get("kind_of_matter") == "late_po"
        or item.get("event_type") == LATE_PO_EVENT
        or detail.get("event_type") == LATE_PO_EVENT
        or detail.get("alert_kind") == LATE_PO_EVENT
        or bool(detail.get("late_po"))
    )


def _matter_source(item: Mapping[str, Any]) -> str:
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    records = [str(r) for r in detail.get("records") or ()]
    if _late_po_matter(item):
        return "pos"
    if detail.get("approval") or detail.get("question") or item.get("kind_of_matter") == "approval":
        return "mc"
    typed = item.get("kind_of_matter") != "conversation"
    if typed and any(r.startswith("MC") for r in records):
        return "mc"
    if typed and any(r.startswith("Planner") for r in records):
        return "planner"
    if detail.get("alert"):
        return "alerts"
    if item.get("channel") in ("mail", "teams"):
        return str(item["channel"])
    return "system"


def _matter_kind(item: Mapping[str, Any]) -> str:
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    if _late_po_matter(item):
        return "late_po"
    if detail.get("question"):
        return "question"
    if item.get("kind_of_matter") == "approval":
        return "approve"
    if item.get("kind_of_matter") == "wait":
        return "waiting"
    if item.get("kind_of_matter") in ("task", "promise"):
        return "task"
    if item.get("kind_of_matter") == "routine":
        return "routine"
    if detail.get("alert"):
        return "alert"
    if item.get("next_act") in REPLY_ACTS or item.get("body"):
        return "reply"
    return "task"


def _is_commitment_matter(item: Mapping[str, Any]) -> bool:
    if item.get("kind_of_matter") == "promise":
        return True
    head = str(item.get("subject") or item.get("summary") or "").strip().lower()
    return head.startswith("you said:")


def _matter_action(item: Mapping[str, Any], kind: str) -> tuple[str, str]:
    if _is_commitment_matter(item):
        return "do", "Commitment"
    if kind == "reply":
        return "reply", "Reply"
    if kind == "late_po":
        return "check", "Check"
    if kind == "approve":
        return "approve", "Approve"
    if kind == "question":
        return "answer", "Answer"
    if kind == "waiting":
        return "check", "Waiting"
    if kind == "alert":
        return "decide", "Decide"
    if item.get("next_act") == "system_write":
        return "do", "Enter it"
    return "do", "Do it"


def _below_cost_pct(text: str) -> Optional[float]:
    m_pct = _BELOW_COST_PCT_RE.search(text or "")
    if not m_pct:
        return None
    try:
        return float(m_pct.group(1))
    except ValueError:
        return None


def _item_section_and_badge(
    *,
    event_type: Optional[str],
    alert_kind: Optional[str],
    explicit_section: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    if explicit_section == "financials" or event_type in FINANCIALS_EVENTS:
        return "financials", FINANCIALS_BADGES.get(event_type or "")
    if alert_kind == "quality_hold" or event_type in HOLD_EVENTS or alert_kind in HOLD_EVENTS:
        badge = HOLD_BADGES.get(event_type or "") or HOLD_BADGES.get(alert_kind or "") or "Quality hold"
        return "holds", badge
    return explicit_section or None, None


def _clean_plain(text: Any) -> str:
    if text is None:
        return ""
    raw = str(text).replace("\u2014", " - ").replace("\u2013", " - ").strip()
    if not raw:
        return ""
    cleaned = clean_text(raw, strip_email_noise=True)
    return str(counted_words(cleaned) or "").strip() if cleaned else ""


def _sentence_case(text: str) -> str:
    """Capitalize a lowercase-starting action phrase without touching acronyms or URLs."""
    if not text or not text[0].islower():
        return text
    if text.startswith(("http://", "https://")):
        return text
    first_word = text.split(" ", 1)[0]
    if "@" in first_word or "." in first_word:
        return text
    return text[0].upper() + text[1:]


def _extract_matter_enrichment(
    item: Mapping[str, Any],
    detail: Mapping[str, Any],
    queue_item: Optional[Mapping[str, Any]],
) -> tuple[
    Optional[str],
    Optional[str],
    Optional[str],
    list[str],
    list[m.HomeNeedLink],
    str,
    str,
    str,
    Optional[str],
]:
    """Pull canonical record ref, concrete facts, record links, and human-readable summary/note/
    quote/person from a matter's ``sources_detail`` and (when present) its originating autopilot
    queue item (e.g. ``qc_hold`` with lot/batch evidence and affected sales orders)."""
    from operating import autopilot_queue

    qbody = (queue_item.get("body") or {}) if isinstance(queue_item, Mapping) else {}
    prepared = qbody.get("prepared") if isinstance(qbody.get("prepared"), Mapping) else {}
    happened = qbody.get("happened") if isinstance(qbody.get("happened"), Mapping) else {}
    fields = happened.get("fields") if isinstance(happened.get("fields"), Mapping) else {}

    raw_affected = [
        a for a in (prepared.get("affected") or detail.get("affected") or ())
        if isinstance(a, Mapping)
    ]
    affected: list[Mapping[str, Any]] = []
    seen_orders: set[str] = set()
    for a in raw_affected:
        okey = autopilot_queue.order_key(a.get("order_number"))
        if okey and okey in seen_orders:
            continue
        if okey:
            seen_orders.add(okey)
        affected.append(a)

    raw_evidence = [
        e for e in (prepared.get("evidence") or detail.get("evidence") or ())
        if isinstance(e, Mapping)
    ]
    task_obj = prepared.get("task") if isinstance(prepared.get("task"), Mapping) else (
        detail.get("task") if isinstance(detail.get("task"), Mapping) else None
    )
    raw_links = [
        lnk for lnk in (*(prepared.get("links") or ()), *(detail.get("links") or ()))
        if isinstance(lnk, Mapping)
    ]

    # Collect candidate object IDs / object hrefs in priority order:
    # primary record (lot/batch/task/detail records) first, then affected orders.
    ref_candidates: list[Any] = []
    ref_candidates.extend(detail.get("records") or ())
    for k in ("object_id", "record", "lot_object_id", "batch_object_id", "order_object_id"):
        if detail.get(k):
            ref_candidates.append(detail[k])
    for e in raw_evidence:
        if e.get("object_id"):
            ref_candidates.append(e["object_id"])
    if task_obj and task_obj.get("object_id"):
        ref_candidates.append(task_obj["object_id"])
    if happened.get("object_id"):
        ref_candidates.append(happened["object_id"])
    for a in affected:
        if a.get("object_id"):
            ref_candidates.append(a["object_id"])
    for lnk in raw_links:
        if lnk.get("href"):
            ref_candidates.append(lnk["href"])

    object_id, object_type, record_url = _record_ref(ref_candidates)

    # Build deduplicated canonical record links.
    links: list[m.HomeNeedLink] = []
    seen_hrefs: set[str] = set()

    def add_link(label: str, href: Optional[str]) -> None:
        if not href or href in seen_hrefs:
            return
        seen_hrefs.add(href)
        links.append(m.HomeNeedLink(label=label, href=href))

    for lnk in raw_links:
        _oid, _otype, url = _parse_object_href(lnk.get("href"))
        if url:
            add_link(str(lnk.get("label") or _RECORD_LABELS.get(_otype or "", "Open record")), url)

    order_names = {
        str(a.get("object_id")): str(a.get("order_number") or "").strip()
        for a in affected if a.get("object_id")
    }
    order_count = sum(1 for oid in order_names if oid.startswith("SalesOrder:"))

    for cand in ref_candidates:
        oid, otype, url = _record_ref([cand])
        if not (oid and otype and url):
            continue
        label = _RECORD_LABELS.get(otype, "Open record")
        if otype == "SalesOrder" and order_count > 1 and order_names.get(oid):
            label = f"Open order {order_names[oid]}"
        add_link(label, url)

    # Build concrete, plain-English facts.
    facts: list[str] = []
    lot_no = str(prepared.get("lot") or fields.get("lot") or fields.get("lot_number") or "").strip()
    product = str(
        prepared.get("product") or prepared.get("item") or fields.get("product")
        or fields.get("item_no") or fields.get("item_description") or ""
    ).strip()
    if lot_no and product:
        facts.append(_clean_plain(f"Lot {lot_no} ({product})"))
    elif lot_no:
        facts.append(_clean_plain(f"Lot {lot_no}"))
    elif product:
        facts.append(_clean_plain(f"Product: {product}"))

    affected_labels: list[str] = []
    first_customer: Optional[str] = None
    for a in affected:
        ord_no = str(a.get("order_number") or "").strip()
        cust = str(a.get("customer") or "").strip()
        if cust and not first_customer:
            first_customer = cust
        if ord_no and cust:
            part = f"{ord_no} ({cust})"
        else:
            part = ord_no or cust or "1 open order"
        affected_labels.append(part)
        facts.append(_clean_plain(f"Affected order: {part}"))

    rec = prepared.get("recommendation")
    if isinstance(rec, str) and rec.strip():
        facts.append(_clean_plain(f"Recommended: {rec.strip()}"))
    for reason in prepared.get("reasons") or detail.get("reasons") or ():
        if isinstance(reason, str) and reason.strip():
            facts.append(_clean_plain(reason))

    # Deduplicate facts preserving order (case-insensitive).
    dedup_facts: list[str] = []
    seen_facts: set[str] = set()
    for f in facts:
        if not f:
            continue
        fkey = f.lower()
        if fkey not in seen_facts:
            seen_facts.add(fkey)
            dedup_facts.append(f)
    facts = dedup_facts

    summary = _clean_plain(prepared.get("summary") or "")
    if summary and len(affected) < len(raw_affected):
        n = len(affected)
        said = "1 open order" if n == 1 else f"{n} open orders"
        summary = re.sub(r"holds up \d+ open orders?", "holds up " + said, summary)
        summary = re.sub(r"\d+ open order\(s\) affected", said + " affected", summary)

    action_obj = prepared.get("action") if isinstance(prepared.get("action"), Mapping) else {}
    prep_note = _clean_plain(prepared.get("note") or action_obj.get("note") or "")
    prep_quote = _clean_plain(prepared.get("context") or happened.get("snippet") or "")
    person_hint = clean_person(first_customer) or clean_person(happened.get("sender"))

    return object_id, object_type, record_url, facts, links, summary, prep_note, prep_quote, person_hint


def _split_scope_tail(text: str) -> tuple[str, str]:
    """Separate trailing privacy/owner scope sentences from the main body text."""
    lines = [ln.strip() for ln in str(text or "").split("\n")]
    if not lines:
        return "", ""
    body = lines[0]
    scopes: list[str] = []
    m_owner = _SCOPE_OWNER_RE.search(body)
    if m_owner:
        scopes.append(m_owner.group(1).strip())
        body = body[:m_owner.start()].strip()
    else:
        m_only = _SCOPE_ONLY_YOU_RE.search(body)
        if m_only:
            scopes.append(m_only.group(1).strip())
            body = body[:m_only.start()].strip()
    for ln in lines[1:]:
        if not ln:
            continue
        if _SCOPE_ONLY_YOU_RE.fullmatch(ln) or _SCOPE_OWNER_RE.fullmatch(ln):
            scopes.append(ln)
        elif not body:
            body = ln
        else:
            body = f"{body} {ln}".strip()
    dedup_scopes = list(dict.fromkeys(s if s.endswith(".") else f"{s}." for s in scopes if s))
    return body.strip(), "\n".join(dedup_scopes)


def _strip_boilerplate(text: str) -> str:
    """Strip repetitive waiting/history boilerplate, bare hold stubs, and 'Still open?' lead-ins."""
    s = _STILL_OPEN_PREFIX_RE.sub("", str(text or "").strip()).strip()
    s = _NO_REPLY_HISTORY_PAREN_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip().rstrip(";, ")
    if not s or _NO_REPLY_HISTORY_STANDALONE_RE.fullmatch(s) or _BARE_DATE_HOLD_STUB_RE.fullmatch(s):
        return ""
    return s


def _strip_person_prefix(text: str, person: Optional[str]) -> str:
    """Remove redundant leading 'Ricardo:' when 'Ricardo' is already the row's person."""
    if not text or not person:
        return text
    cleaned = text.strip()
    name = person.strip()
    if not name:
        return cleaned
    first = name.split()[0]
    candidates = (name, first) if first and first != name else (name,)
    for cand in candidates:
        rx = re.compile(rf"^{re.escape(cand)}\s*:\s*", re.IGNORECASE)
        if rx.match(cleaned):
            return rx.sub("", cleaned, count=1).strip()
    return cleaned


def _norm_words(text: str) -> str:
    return _NORM_WORD_RE.sub(" ", (text or "").lower()).strip()


def _clean_hold_title_and_impact(title: str) -> tuple[str, Optional[str]]:
    """Turn verbose 'QC hold or fail: Lot BC-25295D (AA-NA650) is rejected; it holds up 1 open order.'
    into crisp headline 'Lot BC-25295D rejected (AA-NA650)' and concise impact '1 open order affected'."""
    t = _HOLD_TITLE_PREFIX_RE.sub("", (title or "").strip()).strip()
    impact: Optional[str] = None
    m_lot = _HOLD_LOT_STATUS_RE.match(t)
    if m_lot:
        lot_part = m_lot.group(1).strip()
        product_part = (m_lot.group(2) or "").strip()
        raw_status = m_lot.group(3).lower().replace("-", " ")
        status_word = "rejected" if raw_status in ("rejected", "failed") else "on hold"
        if m_lot.group(4):
            count = int(m_lot.group(4))
            impact = "1 open order affected" if count == 1 else f"{count} open orders affected"
        headline = f"{lot_part} {status_word} ({product_part})" if product_part else f"{lot_part} {status_word}"
        return re.sub(r"\s{2,}", " ", headline).strip(), impact
    m = _HOLD_ON_HOLD_ORDERS_RE.search(t)
    if m:
        raw_status = m.group(1).lower().replace("-", " ")
        status_word = "rejected" if raw_status in ("rejected", "failed") else "on hold"
        count = int(m.group(2))
        impact = "1 open order affected" if count == 1 else f"{count} open orders affected"
        t = f"{t[:m.start()].strip()} {status_word}"
    elif _HOLD_ON_HOLD_END_RE.search(t):
        m_end = _HOLD_ON_HOLD_END_RE.search(t)
        raw_status = m_end.group(1).lower().replace("-", " ") if m_end else "on hold"
        status_word = "rejected" if raw_status in ("rejected", "failed") else "on hold"
        t = f"{_HOLD_ON_HOLD_END_RE.sub('', t).strip()} {status_word}"
    return re.sub(r"\s{2,}", " ", t).strip(), impact


def _clean_waiting_title_and_line(
    title: str,
    body: str,
    person: Optional[str],
) -> tuple[str, str, Optional[str], str]:
    """Promote the real topic out of generic 'No answer yet from Ricardo' into the headline
    (e.g. 'Portal write endpoints and key in staging') and surface 'Waiting on Ricardo' in action_label."""
    m = _WAIT_TITLE_RE.match((title or "").strip())
    if not m:
        who = person or ""
        return title, body, person, f"Waiting on {who}" if who else "Waiting"
    who_raw = m.group(1).strip(" .:")
    who_display = (person or "") if who_raw.lower() == "them" else who_raw
    if not person and who_display:
        person = clean_person(who_display) or who_display
    wait_label = f"Waiting on {who_display}" if who_display else "Waiting on reply"
    topic = _strip_person_prefix(body, person or who_display).rstrip(".").strip()
    if topic:
        clipped = _sentence_case(clip_words(topic, 120))
        new_body = "" if len(topic) <= 120 else topic
        return clipped, new_body, person, wait_label
    fallback_title = f"Follow up with {who_display}" if who_display else "Waiting on reply"
    return fallback_title, "", person, wait_label


def _clean_commitment_title(title: str, *, has_due: bool) -> str:
    """Turn 'You said: renew the workbench-host-vince Portal access before Dec 14, 2026'
    into crisp 'Renew workbench-host-vince Portal access' when due_at already shows the date."""
    t = _COMMITMENT_PREFIX_RE.sub("", (title or "").strip()).strip()
    if has_due:
        t = _BEFORE_DATE_TAIL_RE.sub("", t).strip()
    t = re.sub(r"^([A-Za-z]+)\s+the\s+(?=[a-z0-9]+-[a-z0-9-]+)", r"\1 ", t, flags=re.IGNORECASE)
    return _sentence_case(t.rstrip(".")) or title


def _dedupe_line_against_title(
    title: str,
    body: str,
    *,
    is_hold: bool = False,
    is_commitment: bool = False,
) -> str:
    """Drop subtitle text that simply echoes the title, preserving any additional concrete info."""
    text = (body or "").strip()
    if not text:
        return ""
    if is_hold:
        text = _HOLD_TITLE_PREFIX_RE.sub("", text).strip()
        text = _HOLD_LEAD_SENTENCE_RE.sub("", text).strip()
        if not text:
            return ""
    cmp_text = _BEFORE_DATE_TAIL_RE.sub("", text).strip() if is_commitment else text
    norm_title_full = _norm_words(title)
    norm_title_body = _norm_words(_TITLE_LEAD_STRIP_RE.sub("", title))
    norm_body = _norm_words(cmp_text)
    if (
        not norm_body
        or norm_body == norm_title_full
        or norm_body == norm_title_body
        or (len(norm_title_body) >= 12 and (norm_title_full.find(norm_body) != -1 or norm_body.find(norm_title_body) != -1))
    ):
        return ""
    tok_title = _topic_tokens(norm_title_body or norm_title_full)
    tok_body = _topic_tokens(norm_body)
    if tok_body and tok_title and len(tok_body - tok_title) == 0:
        return ""
    return text


def _is_stub_note(note: str, detail: Mapping[str, Any], queue_item: Optional[Mapping[str, Any]]) -> bool:
    """True when ``note`` is empty or just a bare workflow label stub (like ``QC hold or fail``)."""
    body, _scope = _split_scope_tail(note or "")
    stripped = _strip_boilerplate(body).strip().rstrip(".")
    if not stripped:
        return True
    qbody = (queue_item.get("body") or {}) if isinstance(queue_item, Mapping) else {}
    stubs = {
        str(detail.get("autopilot") or "").strip().lower(),
        str(qbody.get("label") or "").strip().lower(),
        "qc hold or fail",
        "quality hold",
        "qc hold",
        "batch hold",
        "order hold",
    }
    stubs.discard("")
    return stripped.lower() in stubs


def _matter_item(
    item: Mapping[str, Any],
    *,
    queue_item: Optional[Mapping[str, Any]] = None,
) -> m.HomeNeedItem:
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    (
        object_id,
        object_type,
        record_url,
        facts,
        links,
        prep_summary,
        prep_note,
        prep_quote,
        person_hint,
    ) = _extract_matter_enrichment(item, detail, queue_item)
    kind = _matter_kind(item)
    action, action_label = _matter_action(item, kind)
    matter_id = str(item.get("item_id") or "")
    source = _matter_source(item)
    title = clean_subject(
        item.get("subject") or item.get("summary") or "Open work",
        fallback="Open work",
        max_len=160,
    )
    raw_note_body, scope_tail = _split_scope_tail(str(item.get("note") or "").strip())
    clean_note_body = _strip_boilerplate(_clean_plain(raw_note_body))
    raw_summary = _strip_boilerplate(_clean_plain(item.get("summary") or ""))
    clean_prep_summary = _strip_boilerplate(prep_summary)
    if item.get("ask"):
        body_line = _sentence_case(_strip_boilerplate(_clean_plain(item["ask"])))
    elif _is_stub_note(raw_note_body, detail, queue_item):
        # Replace bare workflow label stubs (e.g. "QC hold or fail.") with concrete summary + facts.
        base = clean_prep_summary or raw_summary
        affected_facts = [f for f in facts if f.startswith("Affected order:")]
        extra_bits = [f for f in affected_facts if f.split(":", 1)[1].strip() not in base]
        if extra_bits:
            joined = "; ".join(b.replace("Affected order: ", "") for b in extra_bits[:3])  # guard-ok: all affected orders are listed in item.facts
            suffix = f"Affected orders: {joined}." if len(extra_bits) > 1 else f"Affected order: {joined}."
            body_line = f"{base} {suffix}".strip() if base else suffix
        else:
            body_line = base
    else:
        body_line = _sentence_case(clean_note_body or clean_prep_summary or raw_summary)
    href = record_url or (links[0].href if links else f"/assist?item={quote(matter_id)}")
    event_type = (
        str(item.get("event_type") or detail.get("event_type") or detail.get("autopilot") or "") or None
    )
    alert_kind = str(item.get("alert_kind") or detail.get("alert_kind") or "") or None
    if not alert_kind and event_type in HOLD_EVENTS:
        alert_kind = "quality_hold"
    section, badge = _item_section_and_badge(event_type=event_type, alert_kind=alert_kind)
    person = clean_person(item.get("sender")) or person_hint or None

    is_hold = section == "holds" or bool(_HOLD_TITLE_PREFIX_RE.match(title))
    hold_impact: Optional[str] = None
    if is_hold:
        title, hold_impact = _clean_hold_title_and_impact(title)

    raw_due_at = item.get("due_at")
    effective_due_at = raw_due_at
    if raw_due_at and (item.get("kind_of_matter") == "conversation" or kind in ("reply", "waiting")):
        due_dt = _moment(raw_due_at)
        basis_dt = _moment(item.get("basis_at"))
        opened_dt = _moment(item.get("opened_at") or item.get("created_at"))
        ref_dt = max((t for t in (basis_dt, opened_dt) if t is not None), default=None)
        if due_dt is not None and ref_dt is not None and (
            due_dt.date() < ref_dt.date() or (ref_dt - due_dt) > timedelta(hours=12)
        ):
            effective_due_at = None

    is_commitment = _is_commitment_matter(item)
    if is_commitment:
        title = _clean_commitment_title(title, has_due=bool(effective_due_at))

    if kind == "waiting" or _WAIT_TITLE_RE.match(title):
        title, body_line, person, action_label = _clean_waiting_title_and_line(title, body_line, person)

    body_line = _strip_person_prefix(body_line, person)
    body_line = _dedupe_line_against_title(title, body_line, is_hold=is_hold, is_commitment=is_commitment)
    if _MC_TASK_STUB_RE.fullmatch(body_line):
        body_line = ""
    if is_hold and hold_impact:
        if body_line:
            if hold_impact.lower() not in body_line.lower():
                body_line = f"{hold_impact} · {body_line}"
        else:
            body_line = hold_impact
    if body_line:
        body_line = _sentence_case(body_line)

    line = f"{body_line}\n{scope_tail}".strip() if scope_tail else body_line

    ask = _sentence_case(_strip_boilerplate(_clean_plain(item.get("ask") or "") or prep_note)) or None
    if ask and (ask == body_line or _norm_words(ask) == _norm_words(title)):
        ask = None
    quote_text = clean_text(item.get("message") or prep_quote or "", max_len=280, strip_email_noise=True) or None
    return m.HomeNeedItem(
        id=f"matter:{matter_id}",
        kind=kind,
        source=source,
        source_label=SOURCE_WORDS[source],
        title=title,
        line=line,
        person=person,
        basis_at=_iso(item.get("basis_at")),
        due_at=_iso(effective_due_at),
        state="unverified" if item.get("state") == "unverified" else "open",
        still_open=bool(item.get("still_open")),
        draft_ready=bool(item.get("body")),
        action=action,
        action_label=action_label,
        href=href,
        matter_id=matter_id,
        quote=quote_text,
        ask=ask,
        body=str(item.get("body") or "") or None,
        to=[str(x) for x in item.get("to") or ()],
        next_act=str(item.get("next_act") or "") or None,
        plan_status=str(item.get("plan_status") or "") or None,
        verified_at=_iso(item.get("verified_at")),
        object_id=object_id,
        object_type=object_type,
        event_type=event_type,
        alert_kind=alert_kind,
        section=section,
        badge=badge,
        facts=facts,
        links=links,
        rank=1,
    )


def _late_item(po: m.LatePo, *, uncertain: bool = False) -> m.HomeNeedItem:
    object_id, object_type, _url = _record_ref([po.object_id])
    person = clean_text(po.supplier) or None
    reason = _clean_plain(po.reason)
    line = reason or (
        "May already be closed or missed in the data; check it."
        if uncertain
        else "Check the late purchase order."
    )
    title = clean_text(fix_po_prefix("PO", po.label)) or "Late PO"
    return m.HomeNeedItem(
        id=f"late:{po.object_id}",
        kind="late_po",
        source="pos",
        source_label=SOURCE_WORDS["pos"],
        title=title,
        line=line,
        person=person,
        basis_at=po.due_date,
        due_at=po.due_date,
        state="uncertain" if uncertain else "open",
        action="check",
        action_label="Check",
        href=po.open_url or object_url(object_type or "PurchaseOrder", po.object_id),
        object_id=object_id or po.object_id,
        object_type=object_type or "PurchaseOrder",
        amount=None if uncertain else po.amount,
        days_late=po.days_late,
        rank=1,
    )


def _alert_item(item: m.BriefingItem) -> m.HomeNeedItem:
    amount = (
        item.amount
        if item.amount is not None and not item.amount_suspect and not item.amount_disagree
        else None
    )
    if amount is not None:
        cleaned_amt = clean_money_amount(amount.amount)
        if cleaned_amt is not None and cleaned_amt != amount.amount:
            amount = m.Money(amount=cleaned_amt, currency=amount.currency)
    summary = _clean_plain(item.summary)
    next_action = _clean_plain(item.next_action.text)
    line = summary or next_action
    ask = next_action if (next_action and next_action != line) else None
    event_type = str(item.event_type or "") or None
    alert_kind = str(item.alert_kind or "") or None
    section, badge = _item_section_and_badge(
        event_type=event_type,
        alert_kind=alert_kind,
        explicit_section=str(item.section or "") or None,
    )
    state = "unverified" if item.carried else "open"
    if event_type == "price_below_cost":
        pct = _below_cost_pct(summary)
        if pct is not None and pct < COST_SUSPECT_PCT:
            amount = None
            badge = "Verify unit cost"
            if state == "open":
                state = "uncertain"
            sanity = (
                "Booked cost is over 5x revenue; verify unit cost and unit of measure before "
                "treating as margin loss."
            )
            ask = f"{sanity} {ask}" if ask else sanity
        elif pct is not None and pct < COST_VERIFY_PCT:
            badge = "Verify unit cost"
            sanity = "Booked cost is over 2x revenue; verify unit cost before treating as margin loss."
            if "unit cost" not in line.lower() and "unit cost" not in (ask or "").lower():
                ask = f"{sanity} {ask}" if ask else sanity
        elif item.amount_suspect:
            badge = "Verify unit cost"
            if state == "open":
                state = "uncertain"
    alert_links: list[m.HomeNeedLink] = []
    if item.open_url:
        alert_links.append(
            m.HomeNeedLink(
                label=_RECORD_LABELS.get(str(item.object_type or ""), "Open record"),
                href=item.open_url,
            )
        )
    fallback_title = clean_alert_label(event_type, item.label, alert_kind)
    title = clean_subject(item.subject_label or fallback_title, fallback=fallback_title, max_len=160)
    if section == "holds" or _HOLD_TITLE_PREFIX_RE.match(title):
        title, hold_impact = _clean_hold_title_and_impact(title)
        line = _dedupe_line_against_title(title, line, is_hold=True)
        if hold_impact:
            if line and hold_impact.lower() not in line.lower():
                line = f"{hold_impact} · {line}"
            elif not line:
                line = hold_impact
    since_raw = getattr(item, "since", None)
    since_dt = _moment(since_raw)
    first_dt = _moment(item.first_seen)
    basis_at = (
        _iso(since_raw)
        if since_dt is not None and (first_dt is None or since_dt < first_dt)
        else item.first_seen
    )
    return m.HomeNeedItem(
        id=f"alert:{item.id}",
        kind="alert",
        source="alerts",
        source_label=SOURCE_WORDS["alerts"],
        title=title,
        line=line,
        ask=ask,
        person=clean_person(item.counterpart),
        basis_at=basis_at,
        due_at=None,
        state=state,
        action="decide",
        action_label="Decide",
        href=item.open_url,
        object_id=item.object_id,
        object_type=item.object_type,
        amount=amount,
        days_late=item.days if item.days and item.days > 0 else None,
        event_type=event_type,
        alert_kind=alert_kind,
        section=section,
        badge=badge,
        links=alert_links,
        rank=1,
    )


def _feedback_item(row: Mapping[str, Any]) -> m.HomeNeedItem:
    label = clean_subject(
        row.get("label") or row.get("question") or row.get("note") or "Feedback",
        fallback="Feedback",
        max_len=160,
    )
    note = _clean_plain(row.get("note") or row.get("expected") or row.get("verdict") or "")
    fid = str(row.get("id") or "")
    return m.HomeNeedItem(
        id=f"feedback:{fid}",
        kind="feedback",
        source="feedback",
        source_label=SOURCE_WORDS["feedback"],
        title=label,
        line=note or "Read the feedback and decide what to do.",
        person=clean_person(row.get("reader_name")),
        basis_at=_iso(row.get("at")),
        action="decide",
        action_label="Triage",
        href=f"/feedback#{fid}",
        object_id=row.get("object_id"),
        object_type=row.get("object_type"),
        rank=1,
    )


def _gap_item(row: Mapping[str, Any]) -> m.HomeNeedItem:
    kind = str(row.get("kind") or "measure")
    name = str(row.get("name") or "")
    asked = int(row.get("asked") or 0)
    times = "once" if asked == 1 else f"{asked} times"
    return m.HomeNeedItem(
        id=f"gap:{kind}:{name}",
        kind="gap",
        source="gaps",
        source_label=SOURCE_WORDS["gaps"],
        title=f"{name} ({kind})",
        line=f"Asked {times}, no definition.",
        basis_at=_iso(row.get("last")),
        action="decide",
        action_label="Define",
        href="/data-health#answers-gaps",
        rank=1,
    )


def _queue_items_for_matters(backend: Any, items: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Batch-read originating autopilot queue items referenced by matters (one SQLite open)."""
    qids: list[str] = []
    for item in items:
        detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
        qid = detail.get("queue_item")
        if isinstance(qid, str) and qid:
            qids.append(qid)
    if not qids:
        return {}
    path_fn = getattr(backend, "_ap_queue_path", None)
    path = path_fn() if callable(path_fn) else None
    if path is None or not path.exists():
        return {}
    try:
        from operating import autopilot as ap
        queue = ap.Queue(path)
        try:
            out: dict[str, dict[str, Any]] = {}
            for qid in dict.fromkeys(qids):
                found = queue.item(qid)
                if found is not None:
                    out[qid] = found
            return out
        finally:
            queue.close()
    except Exception:  # noqa: BLE001 - best-effort enrichment; never breaks Home
        LOG.debug("home needs: queue enrichment skipped", exc_info=True)
        return {}


LEGACY_ZOMBIE_TASK_DAYS = 180
LEGACY_ZOMBIE_WAIT_DAYS = 120


def _is_legacy_zombie_matter(item: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
    """True for stale legacy Planner/unupdated task matters (>180d undated Planner or
    >365d unupdated task with no future due_at) and ancient undated waits/promises (>120d
    without an upcoming due_at)."""
    kind = str(item.get("kind_of_matter") or "").strip().lower()
    if kind not in ("task", "wait", "promise"):
        return False
    ref = now or _et_now()[0]
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    due = _moment(item.get("due_at"))
    if due is not None and due >= ref:
        return False
    basis = _moment(item.get("basis_at"))
    touched = _moment(item.get("touched_at"))
    last = max((t for t in (basis, touched) if t is not None), default=None)
    if last is None:
        return False
    if kind in ("wait", "promise"):
        if due is None and last < ref - timedelta(days=LEGACY_ZOMBIE_WAIT_DAYS):
            return True
        return False
    # task
    src = str(item.get("source") or item.get("channel") or "").lower()
    note = str(item.get("note") or item.get("detail") or "").lower()
    subj = str(item.get("subject") or "").lower()
    is_planner = "planner" in src or "planner" in note or subj.startswith("plannertask:")
    if is_planner and due is None and last < ref - timedelta(days=LEGACY_ZOMBIE_TASK_DAYS):
        return True
    max_cutoff = ref - timedelta(days=LEGACY_ZOMBIE_DAYS)
    if last < max_cutoff and (due is None or due < max_cutoff):
        return True
    return False


def _filtered_reply_matter(item: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
    """True when a matter is a stale (>14 days) ``needs_my_reply`` / ``reply_owed`` alert,
    an automated Microsoft / Teams / Planner notification, an agent self-notification,
    an automated IT / system monitoring alert, or a stale legacy zombie task/wait."""
    from operating.assist_readings import is_automated_system_alert
    from operating.assist_waits import is_agent_notification
    from operating.pg.alerts import (
        REPLY_ALERT_NAMES,
        is_automated_reply_alert,
        is_stale_reply_alert,
    )
    if is_automated_system_alert(
        sender=item.get("sender") or "",
        subject=item.get("subject") or "",
        summary=item.get("summary") or "",
        note=item.get("note") or "",
        message=item.get("message") or "",
    ):
        return True
    if is_agent_notification(
        item.get("summary") or "",
        item.get("note") or "",
        item.get("message") or "",
        item.get("subject") or "",
    ):
        return True
    if _is_legacy_zombie_matter(item, now=now):
        return True
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    alert_ref = str(detail.get("alert") or "")
    alert_rule = alert_ref.split("|", 1)[0].strip().lower() if alert_ref else ""
    markers = {
        str(item.get("event_type") or "").strip().lower(),
        str(item.get("alert_kind") or "").strip().lower(),
        str(item.get("kind") or "").strip().lower(),
        str(detail.get("event_type") or "").strip().lower(),
        str(detail.get("alert_kind") or "").strip().lower(),
        alert_rule,
    }
    is_reply_alert = bool(markers & REPLY_ALERT_NAMES)
    if is_reply_alert and is_stale_reply_alert(item.get("basis_at"), now=now):
        return True
    if is_reply_alert or item.get("channel") in ("mail", "teams") or _matter_kind(item) in ("reply", "alert"):
        sender_raw = str(item.get("sender") or "").strip().lower()
        if sender_raw and _NOREPLY_SENDER_RE.search(sender_raw):
            return True
        if sender_raw == "cos@petrasoap.com":
            cos_text = " ".join(str(x) for x in (item.get("subject"), item.get("summary"), item.get("note")) if x)
            if _COS_SYSTEM_RE.search(cos_text):
                return True
        text = " ".join(
            str(x) for x in (item.get("summary"), item.get("note"), item.get("message")) if x
        )
        if is_automated_reply_alert(
            text,
            sender=item.get("sender") or "",
            subject=item.get("subject") or "",
        ):
            return True
    return False


def _reader_identities(reader: Any) -> set[str]:
    session = getattr(reader, "session", None)
    raw = [
        getattr(reader, "email", None),
        getattr(reader, "key", None),
        getattr(session, "subject_oid", None),
        *(getattr(session, "identities", ()) or ()),
    ]
    return {str(v).strip().lower() for v in raw if v and str(v).strip().lower() != "unknown"}


def _load_staff_rows(backend: Any) -> list[dict[str, Any]]:
    from operating import autopilot_roster as roster
    rows: list[dict[str, Any]] = []
    if backend is not None and getattr(backend, "pg_dsn", None) and callable(getattr(backend, "pg", None)):
        try:
            with backend.pg().connection() as conn:
                if conn.execute("SELECT to_regclass('biz.staff') IS NOT NULL").fetchone()[0]:
                    for pid, email, dept, title, reports in conn.execute(
                        "SELECT person_id, email, to_jsonb(s)->>'department', "
                        "to_jsonb(s)->>'title', "
                        "COALESCE((to_jsonb(s)->>'reports_count')::int, 0) "
                        "FROM biz.staff s WHERE person_id IS NOT NULL"
                    ).fetchall():
                        rows.append({
                            "person_id": str(pid or "").strip().lower(),
                            "email": str(email or "").strip().lower(),
                            "department": str(dept or "").strip(),
                            "title": str(title or "").strip(),
                            "reports_count": int(reports or 0),
                        })
        except Exception:  # guard-ok: best-effort staff lookup falls back to org capture
            rows = []
    return rows or roster._load_staff_rows()


def _reader_receives_qc_holds(backend: Any, reader: Any) -> bool:
    """True when ``reader`` is the company owner/executive (``owner_open``, ``EXEC`` dept,
    or ``reports_count >= 5``) or explicit Quality/QA/QC/Lab/Regulatory staff. Non-owner
    IT/Production/Engineering/Admin/Sales/Finance staff never receive broadcast QC holds."""
    from operating import autopilot_roster as roster
    from . import feedback as fb
    if fb.owner_open(getattr(reader, "session", None)):
        return True
    idents = _reader_identities(reader)
    if not idents:
        return True
    staff = _load_staff_rows(backend)
    matched = [
        r for r in staff
        if (r.get("person_id") and str(r["person_id"]).strip().lower() in idents)
        or (r.get("email") and str(r["email"]).strip().lower() in idents)
    ]
    if not matched:
        return True
    for r in matched:
        dept = str(r.get("department") or "").strip()
        title = str(r.get("title") or "").strip()
        reports_count = int(r.get("reports_count") or 0)
        if dept.lower() in ("exec", "executive") or reports_count >= 5:
            return True
        if not roster.is_non_operational_staff(department=dept, title=title) and (
            roster.staff_matches_role("quality", department=dept, title=title)
            or bool(_QC_ROLE_RE.search(f"{dept} {title}"))
        ):
            return True
    return False


def _is_qc_hold_matter(item: Mapping[str, Any]) -> bool:
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    markers = {
        str(item.get("event_type") or "").strip().lower(),
        str(item.get("alert_kind") or "").strip().lower(),
        str(detail.get("event_type") or "").strip().lower(),
        str(detail.get("alert_kind") or "").strip().lower(),
        str(detail.get("autopilot") or "").strip().lower(),
    }
    return bool(markers & QC_HOLD_EVENTS)


def _is_qc_hold_alert(item: Any) -> bool:
    ev = str(getattr(item, "event_type", "") or "").strip().lower()
    ak = str(getattr(item, "alert_kind", "") or "").strip().lower()
    return ev in QC_HOLD_EVENTS or ak in QC_HOLD_EVENTS


def _explicitly_addressed_to_reader(item: Any, reader: Any) -> bool:
    idents = _reader_identities(reader)
    if isinstance(item, Mapping):
        if item.get("handed"):
            return True
        detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
        vals = [item.get("mailbox"), item.get("concerns"), detail.get("mailbox"), detail.get("assigned_to")]
    else:
        vals = [getattr(item, "mailbox", None), getattr(item, "concerns", None)]
    return any(str(v).strip().lower() in idents for v in vals if v)


def _matters(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    from . import open_work_tool
    got = open_work_tool.store(backend, reader)
    now = _et_now()[0]
    qc_ok = _reader_receives_qc_holds(backend, reader)
    raw = [
        i for i in got.get("items") or ()
        if not _mc_approval_matter(i)
        and not _filtered_reply_matter(i, now=now)
        and (qc_ok or not _is_qc_hold_matter(i) or _explicitly_addressed_to_reader(i, reader))
    ]
    qmap = _queue_items_for_matters(backend, raw)
    rows = [
        _matter_item(
            i,
            queue_item=qmap.get(str((i.get("sources_detail") or {}).get("queue_item") or "")),
        )
        for i in raw
    ]
    return rows, _part(
        "work",
        str(got.get("status") or "failed"),
        total=len(rows),
        note=str(got.get("reason") or ""),
        as_of=got.get("as_of"),
    )


def _late_all(backend: Any, reader: Any, *, stale: bool = False) -> list[m.LatePo]:
    """Every late row of one certainty group. The first page carries the server's total;
    the rest is then asked for in one read sized by that total, with keyset paging still
    covering a list that moved while it was read (nothing is cut)."""
    first = backend.late_list(reader, offset=0, limit=PAGE, stale=stale)
    out: list[m.LatePo] = list(first.items)
    offset = int(first.next_offset or len(first.items))
    total = int(first.total or 0)
    more = bool(first.more)
    while more:
        page = backend.late_list(reader, offset=offset, limit=max(total - offset, PAGE), stale=stale)
        out.extend(page.items)
        if not page.items:
            break
        more = bool(page.more)
        offset = int(page.next_offset or offset + len(page.items))
    return out


def _is_legacy_zombie_po(po: m.LatePo, *, now: Optional[datetime] = None) -> bool:
    """True for ancient multi-year unclosed legacy ERP purchase orders (>365 days past due),
    which stay searchable in Records/Ask and Late POs stale rather than cluttering Home."""
    days = po.days_late
    due = _moment(po.due_date)
    ref = now or _et_now()[0]
    cutoff = ref - timedelta(days=LEGACY_ZOMBIE_DAYS)
    if days is not None and days > LEGACY_ZOMBIE_DAYS and (due is None or due < cutoff):
        return True
    if due is not None and due < cutoff and (days is None or days > LEGACY_ZOMBIE_DAYS):
        return True
    return False


def _late(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    now = _et_now()[0]
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="home-needs-late") as pool:
        current = pool.submit(_late_all, backend, reader)
        stale = pool.submit(_late_all, backend, reader, stale=True)
        rows = [_late_item(po) for po in current.result() if not _is_legacy_zombie_po(po, now=now)]
        seen = {r.object_id for r in rows}
        uncertain = []
        for po in stale.result():
            if po.object_id not in seen and not _is_legacy_zombie_po(po, now=now):
                uncertain.append(_late_item(po, uncertain=True))
    rows += uncertain
    return rows, _part("pos", "ok", total=len(rows))


def _filtered_reply_alert_item(item: Any, *, now: Optional[datetime] = None) -> bool:
    from operating.assist_readings import is_automated_system_alert
    from operating.pg.alerts import (
        REPLY_ALERT_NAMES,
        is_filtered_reply_alert,
        is_legacy_stale_order_alert,
    )
    if is_automated_system_alert(
        sender=getattr(item, "counterpart", "") or "",
        subject=getattr(item, "subject_label", "") or "",
        summary=getattr(item, "summary", "") or "",
    ):
        return True
    ev = str(getattr(item, "event_type", "") or "").strip().lower()
    ak = str(getattr(item, "alert_kind", "") or "").strip().lower()
    if ev in REPLY_ALERT_NAMES or ak in REPLY_ALERT_NAMES:
        return True
    if is_legacy_stale_order_alert(
        rule=ev,
        kind=ak,
        since=getattr(item, "first_seen", None) or getattr(item, "since", None),
        days=getattr(item, "days", None),
        text=getattr(item, "summary", "") or "",
        now=now,
    ):
        return True
    return is_filtered_reply_alert(
        rule=ev,
        kind=ak,
        since=getattr(item, "first_seen", None),
        days=getattr(item, "days", None),
        text=getattr(item, "summary", "") or "",
        subject=getattr(item, "subject_label", "") or "",
        now=now,
    )


def _alerts(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    brief = backend.briefing(reader, since_visit=False)
    if brief.status == "not_configured":
        return [], _part("alerts", "off", total=0, note=getattr(brief, "note", "") or "alerts are not configured", as_of=brief.as_of)
    if brief.status != "read":
        return [], _part("alerts", "failed", total=0, note=getattr(brief, "note", "") or "alerts could not be read right now", as_of=brief.as_of)
    found: dict[str, m.BriefingItem] = {}
    failures: list[str] = []
    now = _et_now()[0]
    qc_ok = _reader_receives_qc_holds(backend, reader)
    SKIP_ALERT_EVENTS = frozenset({LATE_PO_EVENT, MC_APPROVAL_EVENT, "needs_my_reply", "reply_owed"})
    for item in brief.items or ():
        if (
            item.event_type not in SKIP_ALERT_EVENTS
            and not _mc_approval_alert(item)
            and not _filtered_reply_alert_item(item, now=now)
            and (qc_ok or not _is_qc_hold_alert(item) or _explicitly_addressed_to_reader(item, reader))
        ):
            found[item.id] = item
    for group in brief.groups or ():
        if str(group.event_type) in SKIP_ALERT_EVENTS:
            continue
        if not qc_ok and str(group.event_type or "").strip().lower() in QC_HOLD_EVENTS:
            continue
        if getattr(group, "unavailable", False):
            failures.append(clean_alert_label(group.event_type, group.label))
            continue
        if not getattr(group, "count", 0):
            continue
        offset = 0
        while True:
            try:
                page = backend.briefing_items(reader, group.event_type, offset=offset, limit=PAGE)
            except Exception as exc:  # noqa: BLE001 - this one type is partial, the rest stand
                LOG.warning(
                    "home needs: alert type %s unreadable: %s",
                    group.event_type,
                    type(exc).__name__,
                )
                failures.append(clean_alert_label(group.event_type, group.label))
                break
            for item in page.items:
                if not _filtered_reply_alert_item(item, now=now) and (
                    qc_ok or not _is_qc_hold_alert(item) or _explicitly_addressed_to_reader(item, reader)
                ):
                    found.setdefault(item.id, item)
            if not page.more or not page.items:
                break
            offset += len(page.items)
    rows = [_alert_item(i) for i in found.values() if not _mc_approval_alert(i)]
    stale = bool(brief.refreshing or getattr(brief, "reading", False))
    status = "partial" if failures else ("stale" if stale else "ok")
    note = "; ".join(
        (["some alert types could not be read: " + ", ".join(failures)] if failures else [])
        + ([brief.checked_on] if stale and brief.checked_on else [])
    )
    return rows, _part("alerts", status, total=len(rows), note=note, as_of=brief.as_of)


def _feedback(backend: Any, reader: Any, store: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    from . import feedback as fb
    if store is None or getattr(reader.session, "view_as", False) or not fb.owner_open(reader.session):
        return [], _part("feedback", "off", total=0, note="feedback triage is for the owner")
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page, _total, _counts = store.list(reader=None, status="new", limit=200, offset=offset)
        rows.extend(page)
        if len(page) < 200:
            break
        offset += len(page)
    viewer = fb.Viewer(backend, reader, rows)
    return (
        [_feedback_item(viewer.entry(r)) for r in rows],
        _part("feedback", "ok", total=len(rows)),
    )


def _gaps(reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    """Unresolved Ask/Genie gap alerts ("Asked N times, no definition.") surfaced on the owner's
    Needs-you list; hidden from non-owners and view-as sessions."""
    from operating import gap_list
    from . import feedback as fb
    if getattr(reader.session, "view_as", False) or not fb.owner_open(reader.session):
        return [], _part("gaps", "off", total=0, note="gap alerts are for the owner")
    if gap_list.path() is None:
        return [], _part("gaps", "off", total=0, note="the gap list is not switched on")
    rows = [_gap_item(g) for g in gap_list.counts() if not gap_list.defined(g["kind"], g["name"])]
    return rows, _part("gaps", "ok", total=len(rows))


def _item_specific_href(row: m.HomeNeedItem) -> Optional[str]:
    href = (row.href or "").strip()
    if href.startswith(("/assist?item=", "/feedback#", "/source?")):
        return href
    return None


def _core_topic(row: m.HomeNeedItem) -> str:
    text = (row.title or "").strip()
    while True:
        stripped = _TITLE_LEAD_STRIP_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return _norm_words(text)


def _topic_tokens(core: str) -> frozenset[str]:
    return frozenset(w for w in core.split() if len(w) >= 2 and w not in _DEDUPE_STOPWORDS)


def _code_tokens(core: str) -> frozenset[str]:
    return frozenset(_CODE_TOKEN_RE.findall(core))


def _stem_token(word: str) -> str:
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _event_tokens(text: str) -> frozenset[str]:
    """Content tokens for same-event cross-kind deduplication after stripping lead-ins like
    'You said:', '<Name> says he is trying to', '<Name> has', and light plural normalization."""
    s = (text or "").strip()
    while True:
        stripped = _TITLE_LEAD_STRIP_RE.sub("", s).strip()
        stripped = _SPEAKER_LEAD_RE.sub("", stripped).strip()
        if stripped == s:
            break
        s = stripped
    words = _norm_words(s).split()
    return frozenset(
        _stem_token(w)
        for w in words
        if len(w) >= 2 and w not in _EVENT_DEDUPE_STOPWORDS
    )


def _same_event_overlap(text_a: str, text_b: str) -> bool:
    """True when two same-timestamp items describe the same underlying event (>= 0.35 Jaccard
    or >= 0.70 containment across >= 2 shared content tokens, with no conflicting numeric codes)."""
    norm_a = _norm_words(text_a)
    norm_b = _norm_words(text_b)
    if not norm_a or not norm_b:
        return False
    codes_a = _code_tokens(norm_a)
    codes_b = _code_tokens(norm_b)
    if codes_a and codes_b and codes_a.isdisjoint(codes_b):
        return False
    tok_a = _event_tokens(text_a)
    tok_b = _event_tokens(text_b)
    if len(tok_a) < 2 or len(tok_b) < 2:
        return False
    inter = tok_a & tok_b
    if len(inter) < 2:
        return False
    union = tok_a | tok_b
    jaccard = len(inter) / len(union) if union else 0.0
    containment = len(inter) / min(len(tok_a), len(tok_b))
    return jaccard >= 0.35 or containment >= 0.70


def are_same_event_raw_matters(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """True when two raw Assist matters (e.g. a 'promise' and a 'conversation' created from
    the same message within 120 seconds) describe the same event."""
    owner_a = str(a.get("owner") or "").strip().lower()
    owner_b = str(b.get("owner") or "").strip().lower()
    if owner_a and owner_b and owner_a != owner_b:
        return False
    kind_a = str(a.get("kind_of_matter") or "").strip().lower()
    kind_b = str(b.get("kind_of_matter") or "").strip().lower()
    if {kind_a, kind_b} != {"promise", "conversation"}:
        return False
    ts_a = _moment(a.get("basis_at"))
    ts_b = _moment(b.get("basis_at"))
    if ts_a is None or ts_b is None:
        return False
    if abs((ts_a - ts_b).total_seconds()) > _SAME_EVENT_WINDOW_SECONDS:
        return False
    text_a = " ".join(str(x) for x in (a.get("subject"), a.get("summary")) if x)
    text_b = " ".join(str(x) for x in (b.get("subject"), b.get("summary")) if x)
    return _same_event_overlap(text_a, text_b)


def raw_matter_richness(item: Mapping[str, Any]) -> tuple[int, int, int, int, int, int]:
    """Preference score when collapsing duplicate raw matters: prefer draft_ready / planned
    conversation items over bare promise items."""
    plan_status = str(item.get("plan_status") or "").strip().lower()
    return (
        1 if str(item.get("body") or "").strip() else 0,
        1 if plan_status in ("drafted", "planned") else 0,
        1 if str(item.get("kind_of_matter") or "").strip().lower() != "promise" else 0,
        1 if item.get("facts") else 0,
        1 if str(item.get("ask") or "").strip() else 0,
        len(str(item.get("summary") or "")) + len(str(item.get("note") or "")),
    )


def _row_actionability_score(row: m.HomeNeedItem) -> tuple[int, int, int, int, int, int, float, int]:
    basis = _moment(row.basis_at) or datetime.min.replace(tzinfo=timezone.utc)
    plan_status = str(row.plan_status or "").strip().lower()
    return (
        1 if row.draft_ready else 0,
        1 if plan_status in ("drafted", "planned") else 0,
        1 if row.facts else 0,
        1 if row.ask else 0,
        1 if row.object_id else 0,
        1 if (row.action_label or "").lower() != "commitment" else 0,
        basis.timestamp(),
        len(row.line or "") + len(row.title or ""),
    )


def _is_cross_kind_same_event(a: m.HomeNeedItem, b: m.HomeNeedItem) -> bool:
    """True when a task/promise item and a reply/conversation item (or a commitment + sibling
    task) were created from the same event within 120 seconds with overlapping topic tokens."""
    if a.object_id and b.object_id and a.object_id != b.object_id:
        return False
    is_promise_conv_pair = {a.kind, b.kind} == {"task", "reply"} or (
        a.kind == "task"
        and b.kind == "task"
        and (
            (a.action_label or "").lower() == "commitment"
            or (b.action_label or "").lower() == "commitment"
        )
    )
    if not is_promise_conv_pair:
        return False
    ts_a = _moment(a.basis_at)
    ts_b = _moment(b.basis_at)
    if ts_a is None or ts_b is None:
        return False
    if abs((ts_a - ts_b).total_seconds()) > _SAME_EVENT_WINDOW_SECONDS:
        return False
    if _same_event_overlap(a.title or "", b.title or ""):
        return True
    text_a = f"{a.title or ''} {a.line or ''}".strip()
    text_b = f"{b.title or ''} {b.line or ''}".strip()
    return _same_event_overlap(text_a, text_b)


def _are_duplicate_needs(a: m.HomeNeedItem, b: m.HomeNeedItem) -> bool:
    """True when two Home items represent the same notification or overlapping duplicate."""
    if a.id == b.id:
        return True
    if a.matter_id and b.matter_id and a.matter_id == b.matter_id:
        return True
    href_a, href_b = _item_specific_href(a), _item_specific_href(b)
    if href_a and href_b and href_a == href_b:
        return True

    both_holds = a.section == "holds" and b.section == "holds"
    if a.object_id and b.object_id and a.object_id == b.object_id:
        if both_holds:
            return True
        if a.kind == b.kind and a.kind == "late_po":
            return True
        if a.kind == b.kind and a.kind == "alert" and (a.event_type or "") == (b.event_type or ""):
            return True

    if _is_cross_kind_same_event(a, b):
        return True

    if a.kind != b.kind and not both_holds:
        return False

    # Never collapse two items that explicitly reference different typed records.
    if a.object_id and b.object_id and a.object_id != b.object_id:
        return False

    cp_a = _norm_words(a.person or "")
    cp_b = _norm_words(b.person or "")
    if cp_a or cp_b:
        if cp_a != cp_b:
            return False
    elif a.source != b.source:
        return False

    core_a = _core_topic(a)
    core_b = _core_topic(b)
    if not core_a or not core_b:
        return False
    if core_a == core_b:
        return True

    codes_a = _code_tokens(core_a)
    codes_b = _code_tokens(core_b)
    if (codes_a or codes_b) and codes_a != codes_b:
        return False

    if len(core_a) >= 12 and len(core_b) >= 12 and core_a[:48] == core_b[:48]:  # guard-ok: 48-char fingerprint prefix for deduplication only
        return True

    tok_a = _topic_tokens(core_a)
    tok_b = _topic_tokens(core_b)
    if len(tok_a) >= 2 and len(tok_b) >= 2:
        union = tok_a | tok_b
        if union and (len(tok_a & tok_b) / len(union)) >= 0.65:
            return True
    return False


def _merge_duplicate_pair(existing: m.HomeNeedItem, incoming: m.HomeNeedItem) -> m.HomeNeedItem:
    winner, loser = (
        (incoming, existing)
        if _row_actionability_score(incoming) > _row_actionability_score(existing)
        else (existing, incoming)
    )
    updates: dict[str, Any] = {}
    if not winner.person and loser.person:
        updates["person"] = loser.person
    if not winner.due_at and loser.due_at:
        updates["due_at"] = loser.due_at
    if not winner.object_id and loser.object_id:
        updates["object_id"] = loser.object_id
        updates["object_type"] = winner.object_type or loser.object_type
    if loser.facts:
        seen_f = {f.lower() for f in (winner.facts or ())}
        extra = [f for f in loser.facts if f.lower() not in seen_f]
        if extra:
            updates["facts"] = [*(winner.facts or ()), *extra]
    return winner.model_copy(update=updates) if updates else winner


def _dedupe(rows: Iterable[m.HomeNeedItem]) -> list[m.HomeNeedItem]:
    """Keep every distinct matter and alert while collapsing duplicate notifications (exact id,
    matter_id, item href, duplicate hold/alert on same record, or overlapping same-counterparty
    topic fingerprint with Jaccard >= 0.65). Distinct replies on the same PO stay separate."""
    rows = list(rows)
    late = {
        row.object_id: row
        for row in rows
        if row.id.startswith("late:") and row.kind == "late_po" and row.object_id
    }
    late_matters = {
        row.object_id
        for row in rows
        if row.id.startswith("matter:") and row.kind == "late_po" and row.object_id in late
    }
    merged: list[m.HomeNeedItem] = []
    for row in rows:
        if row.id.startswith("late:") and row.object_id in late_matters:
            continue
        if row.id.startswith("matter:") and row.kind == "late_po" and row.object_id in late:
            po = late[row.object_id]
            row = row.model_copy(update={
                "person": po.person or row.person,
                "due_at": po.due_at or row.due_at,
                "amount": po.amount or row.amount,
                "days_late": po.days_late,
                "object_id": row.object_id or po.object_id,
                "object_type": row.object_type or po.object_type,
            })
        merged.append(row)

    clusters: list[list[Any]] = []
    for row in merged:
        matched_idx: Optional[int] = None
        for idx, cluster in enumerate(clusters):
            if _are_duplicate_needs(cluster[0], row):
                matched_idx = idx
                break
        if matched_idx is None:
            clusters.append([row, 0])
        else:
            clusters[matched_idx][0] = _merge_duplicate_pair(clusters[matched_idx][0], row)
            clusters[matched_idx][1] += 1

    out: list[m.HomeNeedItem] = []
    for winner, dup_count in clusters:
        if dup_count > 0:
            label = f"+{dup_count} {'update' if dup_count == 1 else 'updates'} collapsed"
            facts = [*(winner.facts or ())]
            if label not in facts:
                facts.append(label)
            winner = winner.model_copy(update={"facts": facts})
        out.append(winner)
    return out


def _impact(row: m.HomeNeedItem) -> Decimal:
    if row.amount is None:
        return Decimal(0)
    try:
        return abs(Decimal(str(row.amount.amount)))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def rank(rows: list[m.HomeNeedItem], *, now: Optional[datetime] = None) -> list[m.HomeNeedItem]:
    """Deterministic ordering by typed urgency: overdue first (oldest due first), then due today,
    then waiting/gone quiet, then newest; impact breaks ties within the same currency."""
    now, today = _et_now() if now is None else (now, now.date())
    by_currency: dict[str, list[Decimal]] = {}
    for row in rows:
        if row.amount is not None:
            by_currency.setdefault(row.amount.currency, []).append(_impact(row))
    tops = {cur: max(values) for cur, values in by_currency.items() if values}

    def impact(row: m.HomeNeedItem) -> Decimal:
        if row.amount is None or not tops.get(row.amount.currency):
            return Decimal(0)
        return _impact(row) / tops[row.amount.currency]

    def key(row: m.HomeNeedItem) -> tuple:
        due = _moment(row.due_at)
        basis = _moment(row.basis_at) or datetime.min.replace(tzinfo=timezone.utc)
        due_day = due.date() if due else None
        overdue = due_day is not None and due_day < today
        due_today = due_day == today
        quiet = bool(row.still_open or row.kind == "waiting") and not overdue and not due_today
        if overdue:
            bucket, first = 0, due.timestamp() if due else 0.0
        elif due_today:
            bucket, first = 1, due.timestamp() if due else 0.0
        elif quiet:
            bucket, first = 2, basis.timestamp()
        else:
            bucket, first = 3, -basis.timestamp()
        return (bucket, first, -float(impact(row)), -basis.timestamp(), row.title.lower(), row.id)

    def reasons(row: m.HomeNeedItem) -> list[str]:
        out: list[str] = []
        due = _moment(row.due_at)
        if due and due.date() < today:
            days = (today - due.date()).days
            out.append(f"overdue by {days} {'day' if days == 1 else 'days'}" if days > 0 else "overdue")
        elif due and due.date() == today:
            out.append("due today")
        elif row.still_open or row.kind == "waiting":
            out.append("waiting on a follow-up")
        elif row.days_late and row.days_late > 0:
            out.append(f"{row.days_late} {'day' if row.days_late == 1 else 'days'} late")
        if row.amount is not None and impact(row):
            try:
                amt_fmt = f"{Decimal(str(row.amount.amount)):,.2f}"
            except (InvalidOperation, ValueError):
                amt_fmt = str(row.amount.amount)
            out.append(f"{amt_fmt} {row.amount.currency} involved")
        return out

    rows = sorted(rows, key=key)
    return [
        row.model_copy(update={"rank": n, "rank_reasons": reasons(row)})
        for n, row in enumerate(rows, 1)
    ]


def _age_moment(row: m.HomeNeedItem) -> Optional[datetime]:
    """The timestamp an item is aged by: its due date when present, otherwise last activity."""
    return _moment(row.due_at) or _moment(row.basis_at)


def split_older(
    rows: list[m.HomeNeedItem],
    *,
    now: Optional[datetime] = None,
) -> tuple[list[m.HomeNeedItem], list[m.HomeNeedItem]]:
    """Split into ``(current, older)``: items strictly older than ``OLDER_NEEDS_AFTER_DAYS``,
    plus stale alerts/replies/waits older than ``STALE_ALERT_DAYS`` and stale MC tasks older
    than ``STALE_MC_TASK_DAYS`` without fresh human activity, move to ``older``."""
    now = now or _et_now()[0]
    cutoff = now - timedelta(days=OLDER_NEEDS_AFTER_DAYS)
    alert_cutoff = now - timedelta(days=STALE_ALERT_DAYS)
    mc_cutoff = now - timedelta(days=STALE_MC_TASK_DAYS)
    fresh_cutoff = now - timedelta(days=FRESH_ACTIVITY_DAYS)
    current: list[m.HomeNeedItem] = []
    older: list[m.HomeNeedItem] = []
    for row in rows:
        at = _age_moment(row)
        basis = _moment(row.basis_at)
        due = _moment(row.due_at)
        is_old = (
            (at is not None and at < cutoff)
            or (at is None and row.days_late is not None and row.days_late > OLDER_NEEDS_AFTER_DAYS)
        )
        if not is_old and (row.kind in ("alert", "late_po") or row.source == "alerts"):
            is_old = (
                (row.days_late is not None and row.days_late > STALE_ALERT_DAYS)
                or (
                    row.kind == "alert"
                    and basis is not None
                    and basis < alert_cutoff
                    and (due is None or due < alert_cutoff)
                )
            )
        elif not is_old and row.kind == "task" and row.source == "mc":
            is_old = due is not None and due < mc_cutoff and (basis is None or basis < fresh_cutoff)
        elif not is_old and row.kind in ("reply", "waiting"):
            is_old = basis is not None and basis < alert_cutoff and (due is None or due < alert_cutoff)
        (older if is_old else current).append(row)
    return current, older


class HomeItemStateStore:
    """Persistent per-reader close/dismiss/snooze state for every Home Needs-you item."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._lock = threading.RLock()
        if path is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        else:
            from operating.file_modes import keep_private
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            keep_private(path.parent, 0o700)
            self._conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode = WAL")
            keep_private(path, 0o600)
            for side in ("-wal", "-shm"):
                extra = path.with_name(path.name + side)
                if extra.exists():
                    keep_private(extra, 0o600)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS home_item_actions ("
            "reader_key TEXT NOT NULL, item_id TEXT NOT NULL, state TEXT NOT NULL, "
            "until TEXT, at TEXT NOT NULL, reason TEXT, PRIMARY KEY (reader_key, item_id))"
        )

    def set_state(
        self,
        reader_key: str,
        item_id: str,
        state: str,
        *,
        until: Optional[str] = None,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        at_iso = ts.isoformat(timespec="seconds").replace("+00:00", "Z")
        until_iso: Optional[str] = None
        if state == "snoozed":
            parsed = _moment(until) if until else ts + timedelta(hours=24)
            if parsed is None or parsed <= ts or parsed > ts + timedelta(days=90):
                from .errors import UserFacing
                raise UserFacing("bad_request", "Snooze needs a time in the next 90 days.", 422)
            until_iso = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._lock:
            if state == "open":
                self._conn.execute(
                    "DELETE FROM home_item_actions WHERE reader_key = ? AND item_id = ?",
                    (reader_key, item_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO home_item_actions (reader_key, item_id, state, until, at, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(reader_key, item_id) DO UPDATE SET "
                    "state = excluded.state, until = excluded.until, at = excluded.at, reason = excluded.reason",
                    (reader_key, item_id, state, until_iso, at_iso, (reason or "").strip() or None),
                )
        return {"item_id": item_id, "state": state, "until": until_iso, "at": at_iso}

    def hidden_ids(self, reader_key: str, *, now: Optional[datetime] = None) -> set[str]:
        ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, state, until FROM home_item_actions WHERE reader_key = ?",
                (reader_key,),
            ).fetchall()
        out: set[str] = set()
        for item_id, state, until in rows:
            if state == "closed":
                out.add(str(item_id))
            elif state == "snoozed":
                due = _moment(until)
                if due is None or due > ts:
                    out.add(str(item_id))
        return out


_STORE_LOCK = threading.Lock()


def item_state_store(backend: Any) -> HomeItemStateStore:
    with _STORE_LOCK:
        got = getattr(backend, "_home_item_actions_store", None)
        if got is not None:
            return got
        base = getattr(backend, "state_dir", None) or getattr(backend, "data_dir", None)
        path = Path(base) / "home-item-actions.sqlite" if base else None
        store = HomeItemStateStore(path)
        setattr(backend, "_home_item_actions_store", store)
        return store


def _propagate_item_state(
    backend: Any,
    reader: Any,
    req: m.HomeItemStateRequest,
    until: Optional[str],
    *,
    item_id: Optional[str] = None,
) -> bool:
    """Best-effort propagation to Assist/Autopilot/Watcher when the Home item backs one."""
    if req.state not in ("closed", "snoozed", "open"):
        return False
    clean_id = (item_id if item_id is not None else req.item_id).strip()
    matter_id = (req.matter_id or "").strip() or (clean_id[len("matter:"):] if clean_id.startswith("matter:") else "")
    if matter_id:
        if req.state == "open":
            if _HEX24_RE.fullmatch(matter_id):
                try:
                    from . import assist_routes
                    return bool(assist_routes.reopen_matter(backend, reader, matter_id, reason=req.reason))
                except Exception:  # guard-ok: universal home restore remains authoritative if matter store unavailable
                    return False
            reopen = getattr(backend, "autopilot_reopen", None)
            if callable(reopen):
                try:
                    return bool(reopen(reader, matter_id, reason=req.reason))
                except Exception:  # guard-ok: universal home restore remains authoritative if queue store unavailable
                    return False
            return False
        prop_until = until
        if req.state == "snoozed" and until:
            from .autopilot_area import SNOOZE_DAYS, _iso as _apa_iso
            parsed_until = _moment(until)
            max_until = datetime.now(timezone.utc) + timedelta(days=SNOOZE_DAYS) - timedelta(seconds=60)
            if parsed_until is not None and parsed_until > max_until:
                prop_until = _apa_iso(max_until)
        decision = "reject" if req.state == "closed" else "snooze"
        body = m.AutopilotDecisionRequest(decision=decision, until=prop_until, note=req.reason)
        if _HEX24_RE.fullmatch(matter_id):
            try:
                from . import assist_routes
                assist_routes.decide_matter(backend, reader, matter_id, body)
                return True
            except Exception:  # guard-ok: universal home close remains authoritative if matter store unavailable
                return False
        decide = getattr(backend, "autopilot_decide", None)
        if callable(decide):
            try:
                decide(reader, matter_id, body)
                return True
            except Exception:  # guard-ok: universal home close remains authoritative if queue item missing
                return False
    watcher_id = (
        clean_id[len("alert:"):]
        if clean_id.startswith("alert:watcher:")
        else (clean_id if clean_id.startswith("watcher:") else "")
    )
    if watcher_id:
        watcher_fn = getattr(backend, "watcher_item_state", None)
        if callable(watcher_fn):
            try:
                wstate = "dismissed" if req.state == "closed" else ("snoozed" if req.state == "snoozed" else "open")
                wuntil: Optional[str] = None
                if wstate == "snoozed" and until:
                    today_utc = datetime.now(timezone.utc).date()
                    cand = until[:10]
                    try:
                        cand_day = date.fromisoformat(cand)
                        if cand_day <= today_utc:
                            cand_day = today_utc + timedelta(days=1)
                        wuntil = cand_day.isoformat()
                    except ValueError:
                        wuntil = (today_utc + timedelta(days=1)).isoformat()
                watcher_fn(reader, watcher_id, m.WatcherItemStateRequest(state=wstate, until=wuntil, reason=req.reason))
                return True
            except Exception:  # guard-ok: universal home close remains authoritative if watcher unavailable
                return False
    return False


def set_home_item_state(backend: Any, reader: Any, req: m.HomeItemStateRequest) -> m.HomeItemStateResponse:
    """Persist a reader's close/snooze/restore action on any Home item and invalidate their
    cached ``home_needs`` screen immediately."""
    item_id = req.item_id.strip()
    store = item_state_store(backend)
    saved = store.set_state(reader.key, item_id, req.state, until=req.until, reason=req.reason)
    propagated = _propagate_item_state(backend, reader, req, saved["until"], item_id=item_id)
    drop = getattr(backend, "_drop_good", None)
    if callable(drop):
        mode = getattr(reader, "mode", None)
        drop((reader.key, mode, ("home_needs",)))
        drop((reader.key, mode, ("home",)))
        drop((reader.key, mode, ("home", "alerts")))
    wrote = getattr(backend, "reader_wrote", None)
    if callable(wrote):
        wrote(reader, ("home_needs", "home", "briefing", "briefing_items"))
    return m.HomeItemStateResponse(
        item_id=saved["item_id"],
        state=saved["state"],
        until=saved["until"],
        at=saved["at"],
        propagated=propagated,
    )


_PART_PREFIX = {
    "work": "matter:",
    "pos": "late:",
    "alerts": "alert:",
    "feedback": "feedback:",
    "gaps": "gap:",
}


def build(backend: Any, reader: Any, feedback_store: Any = None) -> m.HomeNeeds:
    """Read every source and merge into one payload. The stores are independent and the
    backend already serves concurrent requests, so they are read side by side; each
    source's failure is partial, the others stand, and the merged order stays the one below."""
    rows: list[m.HomeNeedItem] = []
    parts: list[m.HomeNeedPart] = []
    reads = (
        ("work", lambda: _matters(backend, reader)),
        ("pos", lambda: _late(backend, reader)),
        ("alerts", lambda: _alerts(backend, reader)),
        ("feedback", lambda: _feedback(backend, reader, feedback_store)),
        ("gaps", lambda: _gaps(reader)),
    )
    with ThreadPoolExecutor(max_workers=len(reads), thread_name_prefix="home-needs") as pool:
        running = {name: pool.submit(read) for name, read in reads}
        for name, _read in reads:
            try:
                got, part = running[name].result()
            except Exception as exc:  # noqa: BLE001 - one source failed; the rest stand
                LOG.warning("home needs: %s unreadable: %s", name, type(exc).__name__, exc_info=True)
                got, part = [], _part(
                    name,
                    "failed",
                    note=f"{SOURCE_WORDS.get(name, name)} could not be read now",
                )
            rows.extend(got)
            parts.append(part)
    now = _et_now()[0]
    hidden = item_state_store(backend).hidden_ids(reader.key, now=now) if backend is not None else set()
    all_deduped = _dedupe(rows)
    if hidden:
        deduped: list[m.HomeNeedItem] = []
        hidden_by_part: dict[str, int] = {}
        for r in all_deduped:
            if r.id in hidden:
                for src, pref in _PART_PREFIX.items():
                    if r.id.startswith(pref):
                        hidden_by_part[src] = hidden_by_part.get(src, 0) + 1
                        break
            else:
                deduped.append(r)
        parts = [
            p.model_copy(update={"total": max(0, p.total - hidden_by_part.get(p.source, 0))})
            if p.total is not None and hidden_by_part.get(p.source, 0)
            else p
            for p in parts
        ]
    else:
        deduped = all_deduped
    current, older = split_older(deduped, now=now)
    rows, older_rows = rank(current, now=now), rank(older, now=now)

    def facets(field: str, words: Mapping[str, str]) -> list[m.HomeNeedFacet]:
        counts: dict[str, int] = {}
        for row in rows:
            value = str(getattr(row, field) or "")
            counts[value] = counts.get(value, 0) + 1
        return [
            m.HomeNeedFacet(value=v, label=words.get(v, v), count=counts[v])
            for v in sorted(counts, key=lambda x: (-counts[x], words.get(x, x)))
        ]

    people_counts: dict[str, int] = {}
    for row in rows:
        if row.person:
            people_counts[row.person] = people_counts.get(row.person, 0) + 1
    people = [
        m.HomeNeedFacet(value=p, label=p, count=n)
        for p, n in sorted(people_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    ]

    bad = [
        p
        for p in parts
        if p.status not in ("ok", "off") or (p.status == "off" and p.source == "work")
    ]
    if not rows and parts and all(p.status == "failed" for p in parts):
        status, why = "failed", "None of the needs-you sources could be read now."
    elif bad:
        status = "partial"
        why = "; ".join(
            f"{p.label}: {p.note or p.status}" for p in bad if p.note or p.status != "ok"
        )
    else:
        status, why = "ok", None
    as_of = sorted((p.as_of for p in parts if p.as_of)) or None
    return m.HomeNeeds(
        status=status,
        why=why or None,
        total=len(rows),
        items=rows,
        older_total=len(older_rows),
        older_items=older_rows,
        older_after_days=OLDER_NEEDS_AFTER_DAYS,
        sources=facets("source", SOURCE_WORDS),
        kinds=facets("kind", KIND_WORDS),
        people=people,
        parts=parts,
        as_of=as_of[0] if as_of else None,
    )


def reading() -> m.HomeNeeds:
    return m.HomeNeeds(
        status="unknown",
        why="Reading your needs-you list.",
        total=0,
        older_total=0,
        older_after_days=OLDER_NEEDS_AFTER_DAYS,
        reading=True,
    )
