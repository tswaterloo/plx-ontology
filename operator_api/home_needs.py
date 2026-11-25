"""Home's unified Needs-you read: the reader's Assist matters of every age, every late purchase
order, alerts routed to them, and (for the owner only) new feedback and unresolved Ask/Genie
gap alerts, merged server-side into one ranked list.

Items strictly older than 90 days (by due date, or by last activity when undated) are folded into
a separate ``older_items`` group so nothing is ever capped or dropped. Mission Control approvals
are excluded because they are decided in Mission Control itself.
"""
from __future__ import annotations

from datetime import date, datetime, time as day_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote

from . import models as m
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


def _record_ref(values: Iterable[Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(object id, type, app path) of the first typed ontology object id in ``values``."""
    for value in values:
        text = str(value or "")
        if text.count(":") >= 2 and not text.startswith(("http:", "https:", "mail:", "teams:")):
            otype, oid = text.split(":", 1)
            if otype and oid:
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


def _matter_item(item: Mapping[str, Any]) -> m.HomeNeedItem:
    detail = item.get("sources_detail") if isinstance(item.get("sources_detail"), Mapping) else {}
    object_id, object_type, record_url = _record_ref(detail.get("records") or ())
    kind = _matter_kind(item)
    action, action_label = _matter_action(item, kind)
    matter_id = str(item.get("item_id") or "")
    source = _matter_source(item)
    title = str(item.get("subject") or item.get("summary") or "Open work")
    line = str(item.get("note") or item.get("summary") or "")
    if item.get("ask"):
        line = str(item["ask"])
    href = record_url or f"/assist?item={quote(matter_id)}"
    return m.HomeNeedItem(
        id=f"matter:{matter_id}",
        kind=kind,
        source=source,
        source_label=SOURCE_WORDS[source],
        title=title,
        line=line,
        person=str(item.get("sender") or "") or None,
        basis_at=_iso(item.get("basis_at")),
        due_at=_iso(item.get("due_at")),
        state="unverified" if item.get("state") == "unverified" else "open",
        still_open=bool(item.get("still_open")),
        draft_ready=bool(item.get("body")),
        action=action,
        action_label=action_label,
        href=href,
        matter_id=matter_id,
        quote=str(item.get("message") or "") or None,
        ask=str(item.get("ask") or "") or None,
        body=str(item.get("body") or "") or None,
        to=[str(x) for x in item.get("to") or ()],
        next_act=str(item.get("next_act") or "") or None,
        plan_status=str(item.get("plan_status") or "") or None,
        verified_at=_iso(item.get("verified_at")),
        object_id=object_id,
        object_type=object_type,
        rank=1,
    )


def _late_item(po: m.LatePo, *, uncertain: bool = False) -> m.HomeNeedItem:
    object_id, object_type, _url = _record_ref([po.object_id])
    person = po.supplier or None
    reason = str(po.reason or "").strip()
    line = reason or (
        "May already be closed or missed in the data; check it."
        if uncertain
        else "Check the late purchase order."
    )
    return m.HomeNeedItem(
        id=f"late:{po.object_id}",
        kind="late_po",
        source="pos",
        source_label=SOURCE_WORDS["pos"],
        title=po.label,
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
    summary = str(item.summary or "").strip()
    next_action = str(item.next_action.text or "").strip()
    line = summary or next_action
    ask = next_action if (next_action and next_action != line) else None
    return m.HomeNeedItem(
        id=f"alert:{item.id}",
        kind="alert",
        source="alerts",
        source_label=SOURCE_WORDS["alerts"],
        title=item.subject_label or item.label,
        line=line,
        ask=ask,
        person=item.counterpart or None,
        basis_at=item.first_seen,
        due_at=None,
        state="unverified" if item.carried else "open",
        action="decide",
        action_label="Decide",
        href=item.open_url,
        object_id=item.object_id,
        object_type=item.object_type,
        amount=amount,
        days_late=item.days if item.days and item.days > 0 else None,
        rank=1,
    )


def _feedback_item(row: Mapping[str, Any]) -> m.HomeNeedItem:
    label = str(row.get("label") or row.get("question") or row.get("note") or "Feedback")
    note = str(row.get("note") or row.get("expected") or row.get("verdict") or "")
    fid = str(row.get("id") or "")
    return m.HomeNeedItem(
        id=f"feedback:{fid}",
        kind="feedback",
        source="feedback",
        source_label=SOURCE_WORDS["feedback"],
        title=label,
        line=note or "Read the feedback and decide what to do.",
        person=row.get("reader_name"),
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


def _matters(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    from . import open_work_tool
    got = open_work_tool.store(backend, reader)
    rows = [_matter_item(i) for i in got.get("items") or () if not _mc_approval_matter(i)]
    return rows, _part(
        "work",
        str(got.get("status") or "failed"),
        total=len(rows),
        note=str(got.get("reason") or ""),
        as_of=got.get("as_of"),
    )


def _late_all(backend: Any, reader: Any, *, stale: bool = False) -> list[m.LatePo]:
    out: list[m.LatePo] = []
    offset = 0
    while True:
        page = backend.late_list(reader, offset=offset, limit=PAGE, stale=stale)
        out.extend(page.items)
        if not page.more or not page.items:
            break
        offset = int(page.next_offset or offset + len(page.items))
    return out


def _late(backend: Any, reader: Any) -> tuple[list[m.HomeNeedItem], m.HomeNeedPart]:
    rows = [_late_item(po) for po in _late_all(backend, reader)]
    seen = {r.object_id for r in rows}
    uncertain = []
    for po in _late_all(backend, reader, stale=True):
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
    for item in brief.items or ():
        if item.event_type != LATE_PO_EVENT and not _mc_approval_alert(item):
            found[item.id] = item
    for group in brief.groups or ():
        if str(group.event_type) in (LATE_PO_EVENT, MC_APPROVAL_EVENT):
            continue
        if getattr(group, "unavailable", False):
            failures.append(str(group.label or group.event_type))
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
                failures.append(str(group.label or group.event_type))
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
        out = []
        due = _moment(row.due_at)
        if due and due.date() < today:
            out.append("overdue")
        elif due and due.date() == today:
            out.append("due today")
        if row.still_open or row.kind == "waiting":
            out.append("waiting and gone quiet")
        if row.amount is not None and impact(row):
            out.append(f"{row.amount.amount} {row.amount.currency} involved")
        if not out:
            out.append("newest open work")
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
    """Read every source and merge into one payload. One source failing leaves the rest intact."""
    rows: list[m.HomeNeedItem] = []
    parts: list[m.HomeNeedPart] = []
    for name, read in (
        ("work", lambda: _matters(backend, reader)),
        ("pos", lambda: _late(backend, reader)),
        ("alerts", lambda: _alerts(backend, reader)),
        ("feedback", lambda: _feedback(backend, reader, feedback_store)),
        ("gaps", lambda: _gaps(reader)),
    ):
        try:
            got, part = read()
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
