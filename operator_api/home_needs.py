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
import re
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
FINANCIALS_EVENTS = frozenset({"price_below_cost", "customer_price_drop", "customer_demand_drop", "price_below_typical"})
HOLD_EVENTS = frozenset({"lot_on_hold", "quality_hold_aging", "qc_hold", "batch_hold", "order_hold"})
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
    r"^Still open\?\s*Nothing has happened on it since it was due\s+\d{4}-\d{2}-\d{2}\.\s*",
    re.IGNORECASE,
)
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
    if item.get("kind_of_matter") in ("wait", "promise"):
        return "waiting"
    if item.get("kind_of_matter") == "routine":
        return "routine"
    if detail.get("alert"):
        return "alert"
    if item.get("next_act") in REPLY_ACTS or item.get("body"):
        return "reply"
    return "task"


def _matter_action(item: Mapping[str, Any], kind: str) -> tuple[str, str]:
    if kind == "reply":
        return "reply", "Reply"
    if kind == "late_po":
        return "check", "Check"
    if kind == "approve":
        return "approve", "Approve"
    if kind == "question":
        return "answer", "Answer"
    if kind == "waiting":
        return "check", "Check"
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


def _is_stub_note(note: str, detail: Mapping[str, Any], queue_item: Optional[Mapping[str, Any]]) -> bool:
    """True when ``note`` is empty or just a bare workflow label stub (like ``QC hold or fail``)."""
    stripped = _STILL_OPEN_PREFIX_RE.sub("", (note or "").strip()).strip().rstrip(".")
    if not stripped:
        return True
    qbody = (queue_item.get("body") or {}) if isinstance(queue_item, Mapping) else {}
    stubs = {
        str(detail.get("autopilot") or "").strip().lower(),
        str(qbody.get("label") or "").strip().lower(),
        "qc hold or fail",
        "quality hold",
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
    raw_note = str(item.get("note") or "").strip()
    raw_summary = _clean_plain(item.get("summary") or "")
    if item.get("ask"):
        line = _sentence_case(_clean_plain(item["ask"]))
    elif _is_stub_note(raw_note, detail, queue_item):
        # Replace bare workflow label stubs (e.g. "QC hold or fail.") with concrete summary + facts.
        base = prep_summary or raw_summary or _clean_plain(raw_note)
        affected_facts = [f for f in facts if f.startswith("Affected order:")]
        extra_bits = [f for f in affected_facts if f.split(":", 1)[1].strip() not in base]
        if extra_bits:
            joined = "; ".join(b.replace("Affected order: ", "") for b in extra_bits[:3])
            suffix = f"Affected orders: {joined}." if len(extra_bits) > 1 else f"Affected order: {joined}."
            line = f"{base} {suffix}".strip() if base else suffix
        else:
            line = base
    else:
        line = _sentence_case(_clean_plain(raw_note) or prep_summary or raw_summary)
    href = record_url or (links[0].href if links else f"/assist?item={quote(matter_id)}")
    event_type = (
        str(item.get("event_type") or detail.get("event_type") or detail.get("autopilot") or "") or None
    )
    alert_kind = str(item.get("alert_kind") or detail.get("alert_kind") or "") or None
    if not alert_kind and event_type in HOLD_EVENTS:
        alert_kind = "quality_hold"
    section, badge = _item_section_and_badge(event_type=event_type, alert_kind=alert_kind)
    ask = _sentence_case(_clean_plain(item.get("ask") or "") or prep_note) or None
    if ask and ask == line:
        ask = None
    quote_text = clean_text(item.get("message") or prep_quote or "", max_len=280, strip_email_noise=True) or None
    person = clean_person(item.get("sender")) or person_hint or None
    return m.HomeNeedItem(
        id=f"matter:{matter_id}",
        kind=kind,
        source=source,
        source_label=SOURCE_WORDS[source],
        title=title,
        line=line,
        person=person,
        basis_at=_iso(item.get("basis_at")),
        due_at=_iso(item.get("due_at")),
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
    return m.HomeNeedItem(
        id=f"alert:{item.id}",
        kind="alert",
        source="alerts",
        source_label=SOURCE_WORDS["alerts"],
        title=title,
        line=line,
        ask=ask,
        person=clean_person(item.counterpart),
        basis_at=item.first_seen,
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


def _matters(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    from . import open_work_tool
    got = open_work_tool.store(backend, reader)
    raw = [i for i in got.get("items") or () if not _mc_approval_matter(i)]
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


def _late(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="home-needs-late") as pool:
        current = pool.submit(_late_all, backend, reader)
        stale = pool.submit(_late_all, backend, reader, stale=True)
        rows = [_late_item(po) for po in current.result()]
        seen = {r.object_id for r in rows}
        uncertain = []
        for po in stale.result():
            if po.object_id not in seen:
                uncertain.append(_late_item(po, uncertain=True))
    rows += uncertain
    return rows, _part("pos", "ok", total=len(rows))


def _alerts(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    brief = backend.briefing(reader, since_visit=False)
    if brief.status == "not_configured":
        return [], _part("alerts", "off", total=0, note=getattr(brief, "note", "") or "alerts are not configured", as_of=brief.as_of)
    if brief.status != "read":
        return [], _part("alerts", "failed", total=0, note=getattr(brief, "note", "") or "alerts could not be read right now", as_of=brief.as_of)
    found: dict[str, m.BriefingItem] = {}
    failures: list[str] = []
    NEEDS_MY_REPLY_EVENT = "needs_my_reply"
    for item in brief.items or ():
        if item.event_type not in (LATE_PO_EVENT, NEEDS_MY_REPLY_EVENT) and not _mc_approval_alert(item):
            found[item.id] = item
    for group in brief.groups or ():
        if str(group.event_type) in (LATE_PO_EVENT, MC_APPROVAL_EVENT, NEEDS_MY_REPLY_EVENT):
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


def _dedupe(rows: Iterable[m.HomeNeedItem]) -> list[m.HomeNeedItem]:
    """Keep every distinct matter and alert; only join a matter typed as the late PO itself with
    the late-PO read's row so supplier, due date, money and days late enrich the matter."""
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

    out: dict[str, m.HomeNeedItem] = {}
    order: list[str] = []
    for row in merged:
        if row.id not in out:
            order.append(row.id)
        out[row.id] = row
    return [out[k] for k in order]


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
    """Split into ``(current, older)``: items strictly older than ``OLDER_NEEDS_AFTER_DAYS``
    move to ``older`` so they fold cleanly on Home without ever being dropped."""
    now = now or _et_now()[0]
    cutoff = now - timedelta(days=OLDER_NEEDS_AFTER_DAYS)
    current: list[m.HomeNeedItem] = []
    older: list[m.HomeNeedItem] = []
    for row in rows:
        at = _age_moment(row)
        is_old = (
            (at is not None and at < cutoff)
            or (at is None and row.days_late is not None and row.days_late > OLDER_NEEDS_AFTER_DAYS)
        )
        (older if is_old else current).append(row)
    return current, older


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
    current, older = split_older(_dedupe(rows), now=now)
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
