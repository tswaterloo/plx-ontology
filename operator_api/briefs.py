"""The app's briefs (``operating.briefs``): a meeting prep brief per calendar event, meeting,
customer or supplier, the upcoming meetings to prepare, and the weekly review, each read
as the signed-in reader (their own tool calls, their own trimmed alert list, their
markings) on the read worker. No model call; an optional summary is a question for the
answer path (``/api/ask``) with its claim checks, never written here."""

from __future__ import annotations

from datetime import date, timedelta
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote

from operating import briefs as br
from operating import card_actions as ca

from . import models as m
from .auth import Reader
from .errors import UserFacing

from .page_core import (
    ENRICH_PRIORITY,
    clean_enum,
    clean_location,
    clean_money_amount,
    clean_person,
    clean_subject,
    clean_text,
    fix_po_prefix,
)

BRIEF_TIMEOUT_SECONDS = 90
# The weekly review is read whole (owner 11-23, no limit on what a page reads): no time
# budget cuts it, so a finished week is always kept for its reader (``WeeklyArchive``) and
# every later visit is the archive row. It was cut at 45 s (a visit) and 300 s (the read-ahead),
# a cut page was never kept, and the page took 53-60 s on every visit.


def _sources(raw: Any) -> list[m.Source]:
    from .backend import suggestion_source
    out = []
    for item in raw or ():
        source = suggestion_source(item)
        if source is not None:
            out.append(source)
    return out


def _ref(object_id: Optional[str], label: Optional[str] = None) -> Optional[m.ObjectRef]:
    from .backend import object_url
    if not isinstance(object_id, str) or not object_id:
        return None
    kind = br.type_of(object_id)
    fallback = clean_enum(kind, "object_type") or kind
    clean_lbl = clean_subject(label, fallback=fallback, max_len=200) if label else fallback
    return m.ObjectRef(object_id=object_id, type=kind, label=clean_lbl,
                       open_url=object_url(kind, object_id))


def brief_url(object_id: str) -> str:
    return f"/meetings/brief/{quote(br.type_of(object_id), safe='')}/{quote(object_id, safe='')}"


LATE_ROWS = 100   # the late list's page; the rest open from the section's link (Home's Late list)


def late_from_home(brief: m.Brief, late: Any) -> m.Brief:
    """The review's "Late purchase orders (open now)" as Home's Late list counts them (the
    same reader-scoped read, ``late_list``): the review's own section read another alert
    store and said nothing while Home listed late orders. ``late`` failing leaves the
    section as the review had it."""
    try:
        got = late()
    except Exception:  # noqa: BLE001 - the section keeps what the review read
        return brief
    if getattr(got, "reading", False):
        # Home's first read for this reader is still going on: its empty list is no
        # finding, so the section never says "nothing late" (or a 0) meanwhile
        section = m.BriefSection(
            key="late_pos", title="Late purchase orders (open now)", state="unread",
            note="Still reading the late purchase orders; open the review again in a moment.")
        sections = [section if s.key == "late_pos" else s for s in brief.sections]
        if not any(s.key == "late_pos" for s in brief.sections):
            sections.insert(1, section)
        return brief.model_copy(update={"sections": sections})
    rows = []
    for po in got.items:
        days_word = "1 day late" if po.days_late == 1 else f"{po.days_late} days late"
        bits = [clean_text(po.supplier) if po.supplier else "",
                days_word if po.days_late else "",
                f"needed {po.due_date}" if po.due_date else ""]
        po_label = clean_text(fix_po_prefix("PO", po.label)) or po.label
        rows.append(m.BriefRow(
            label=po_label, detail=", ".join(b for b in bits if b) or None, date=po.due_date,
            amount=po.amount,
            object=m.ObjectRef(object_id=po.object_id, type=po.object_id.split(":", 1)[0],
                               label=po_label, open_url=po.open_url),
            sources=po.sources))
    section = m.BriefSection(
        key="late_pos", title="Late purchase orders (open now)",
        state="shown" if rows else "empty", rows=rows, more=max(0, got.total - len(rows)),
        more_href="/late",
        note="The same list as Home's Late, longest late first.",
        figures=[m.BriefFigure(label="Late purchase orders", value=str(got.total),
                               unit="records")] if got.total else [])
    sections = [section if s.key == "late_pos" else s for s in brief.sections]
    if not any(s.key == "late_pos" for s in brief.sections):
        sections.insert(1, section)
    return brief.model_copy(update={"sections": sections})


_AGED_BUCKET_RE = re.compile(
    r"^aged\s+(receivable|payable)\s*\(([^)]+)\)$", re.IGNORECASE
)
_STATUS_BIT_RE = re.compile(r"\bstatus\s+(open|closed|draft|unknown|active)\b", re.IGNORECASE)
_ORGANISED_BY_GROUP_RE = re.compile(
    r"^Organised by\s+[^@\s]+@group\.calendar\.google\.com$", re.IGNORECASE
)


def _clean_brief_row_label(raw: Any) -> str:
    text = clean_text(raw, strip_email_noise=False)
    if text == "PO PurchaseOrder":
        return "Purchase order"
    if text == "Order SalesOrder":
        return "Sales order"
    if text.startswith(("Mail: ", "Meeting: ", "Teams: ")):
        prefix, _, rest = text.partition(": ")
        cleaned_rest = clean_subject(rest, fallback="(no subject)" if prefix != "Teams" else "Teams chat", max_len=200)
        return f"{prefix}: {cleaned_rest}"
    text = re.sub(r"^(Decision|Action item|Issue|Note)\s+\(other\):\s*", r"\1: ", text)
    return text or "Item"


def _clean_brief_row_detail(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if _ORGANISED_BY_GROUP_RE.match(text):
        return "Organised by shared calendar"
    aged = _AGED_BUCKET_RE.match(text)
    if aged:
        side = aged.group(1).lower()
        bucket = clean_enum(aged.group(2), "aging_bucket") or aged.group(2)
        return f"Aged {side} ({bucket})"
    text = _STATUS_BIT_RE.sub(lambda m_: clean_enum(m_.group(1), "status") or m_.group(1), text)
    cleaned = clean_text(text, max_len=300, strip_email_noise=True)
    return cleaned or None


def brief_model(raw: Mapping[str, Any]) -> m.Brief:
    sections = []
    for s in raw.get("sections") or ():
        rows = []
        for r in s.get("rows") or ():
            amt = None
            if r.get("amount"):
                a = dict(r["amount"])
                cleaned_amt = clean_money_amount(a.get("amount"))
                if cleaned_amt is not None:
                    a["amount"] = cleaned_amt
                amt = m.Money(**a)
            who = r.get("who")
            who_clean = "Your note" if who == "Your note" else clean_person(who)
            rows.append(m.BriefRow(
                label=_clean_brief_row_label(r["label"]),
                detail=_clean_brief_row_detail(r.get("detail")),
                date=r.get("date"),
                who=who_clean,
                amount=amt,
                object=_ref(r.get("object_id"), None),
                sources=_sources(r.get("sources")),
            ))
        figures = [m.BriefFigure(**{**f, "sources": _sources(f.get("sources"))})
                   for f in s.get("figures") or ()]
        note = clean_text(s.get("note")) if s.get("note") else None
        sections.append(m.BriefSection(key=s["key"], title=clean_text(s["title"]) or s["title"],
                                       state=s["state"], note=note or None, rows=rows,
                                       more=s.get("more", 0), figures=figures))
    subject = raw.get("subject") or {}
    clean_title = clean_subject(raw.get("title") or "", fallback="", max_len=200)
    subj_label = clean_subject(subject.get("label"), fallback=clean_title, max_len=200) if subject.get("label") else None
    question = None
    if raw.get("kind") == "meeting" and subj_label:
        question = (f"Prepare me for the meeting \"{subj_label}\": read everything about "
                    "its people and topics (earlier meetings' notes and transcripts, mail, Teams, "
                    "the records, my open work and my own notes) and tell me what I should know "
                    "and what I have to do.")
    elif raw.get("kind") == "weekly":
        question = f"Summarise the {(clean_title or 'weekly review').lower()}."
    gaps = [
        m.BriefGap(**{
            **g,
            **({"what": clean_text(g["what"])} if g.get("what") else {}),
            **({"reason": clean_text(g["reason"])} if g.get("reason") else {}),
        })
        for g in raw.get("gaps") or ()
    ]
    return m.Brief(kind=raw["kind"], title=clean_title,
                   subject=_ref(subject.get("object_id"), subj_label),
                   subject_sources=_sources(raw.get("subject_sources")),
                   when=raw.get("when"), as_of=raw["as_of"],
                   parties=[r for r in (_ref(p["object_id"], p.get("label"))
                                        for p in raw.get("parties") or ()) if r],
                   possible_parties=[r for r in (_ref(p["object_id"], p.get("label"))
                                                 for p in raw.get("possible_parties") or ())
                                     if r],
                   sections=sections, gaps=gaps,
                   basis=clean_text(raw.get("basis") or ""), summary_question=question,
                   stored_at=raw.get("stored_at"))


def _held(view: Any) -> frozenset[str] | None:
    grant = getattr(getattr(view, "access", None), "grant", None)
    if grant is None:
        return None
    with view._request():
        return grant()[1]


WEEKLY_FILE = "weekly-reviews.sqlite"
# how long each reader's week took to build here (seconds, the last ``WEEKLY_TIMES_KEPT``):
# the estimate the page shows while a first review is built (an archive read is not a build).
WEEKLY_TIMES_FILE = "weekly-build-seconds.json"
WEEKLY_TIMES_KEPT = 20


def _alerts(view: Any, *, resolved: bool = False,
            only: Optional[Iterable[str]] = None) -> Mapping[str, Any]:
    """The reader's alerts of the briefing's kinds; ``only``: of those, just these (a page
    that shows a few kinds never reads and decides the rest)."""
    from operating import briefing as bf
    if only is not None:
        wanted = set(only)
        kinds = [k for k in bf.EVENT_KINDS if k in wanted] or ["\x00none"]
        return view.gap_inbox(kinds=kinds, limit=bf.MAX_READ)
    if not resolved:
        return view.gap_inbox(kinds=list(bf.EVENT_KINDS), limit=bf.MAX_READ)
    try:
        out = view.gap_inbox(kinds=list(bf.EVENT_KINDS), limit=bf.MAX_READ,
                             states=["open", "acknowledged", "resolved"])
        return {**out, "with_resolved": True}
    except TypeError:   # a workspace without the states option: open alerts only
        return view.gap_inbox(kinds=list(bf.EVENT_KINDS), limit=bf.MAX_READ)


def _decide(view: Any) -> Any:
    """The reader's item decisions (``access.decide``), each call inside its request; a
    workspace without them decides nothing open (fail closed)."""
    decide = getattr(getattr(view, "access", None), "decide", None)
    if decide is None:
        return lambda handles: {h: {"reason": "undecided"} for h in handles}
    return decide


def meeting_brief_of(view: Any, object_id: str, who: Any = None, state_dir: Any = None,
                     *, store: Any = None, reader: Optional[str] = None,
                     refresh: bool = False) -> Optional[dict[str, Any]]:
    """The meeting brief as this reader reads it, whole (no time budget: Guardrail 1). With
    a ``store`` and the reader's key: the kept brief when the grant is the same and every
    source it shows still opens (``MeetingBriefStore``), else built now and kept.
    ``refresh``: built now whatever is kept. The brief carries ``kept`` (it came from the
    store) and ``kept_age`` (seconds)."""
    held = _held(view)
    if held is None:
        return None
    decide = _decide(view)
    grant = None
    if store is not None and reader:
        with view._request():
            grant = _grant_key(view, held)
        kept = store.get(reader, object_id, grant, decide) if grant and not refresh else None
        if kept is not None:
            from .meeting_briefs import age_seconds
            raw = with_carried_items(kept, held, who, state_dir)
            raw["kept"] = True
            raw["kept_age"] = age_seconds(kept.get("stored_at"))
            return raw
    alerts = _alerts(view)
    with view._request():
        brief = br.meeting_brief(view._call, held, alerts, object_id, decide=decide,
                                 people=_people(), budget_seconds=None)
    if store is not None and reader and grant:
        if (brief.get("budget") or {}).get("stopped") or not store.put(reader, object_id,
                                                                       grant, brief):
            import logging
            logging.getLogger("operator_api").info("meeting brief not kept")
    return with_carried_items(brief, held, who, state_dir)


def _viewer(who: Any, held: frozenset[str]) -> Any:
    """The signed-in reader as a follow-up viewer: their principals, address, markings."""
    from operating import meeting_followup as mf
    session = getattr(who, "session", None)
    principals = set(getattr(session, "principals", ()) or ())
    identities = {str(i).lower() for i in getattr(session, "identities", ()) or ()}
    principals |= {"employee:" + i for i in identities if "@" not in i}  # the subject's ids
    base = mf.Viewer.of(principals, held, getattr(who, "email", None))
    return mf.Viewer(base.oids, base.addresses | {i for i in identities if "@" in i},
                     base.held, base.owner)


def _follow_ups(state_dir: Any) -> Any:
    from operating import meeting_followup as mf
    state_dir = state_dir or os.environ.get("PLX_WORKBENCH_STATE_DIR")
    return mf.FollowUps(state_dir) if state_dir else None


def _follow_up_stamp(state_dir: Any) -> Optional[str]:
    """The follow-up store as it stands (each record file and its change time), so a new,
    changed or closed action item is a new upcoming-meetings page key; None without a
    store, ``"unreadable"`` when it cannot be listed (never a stale page for it)."""
    import hashlib
    store = _follow_ups(state_dir)
    if store is None:
        return None
    try:
        entries = sorted((p.name, p.stat().st_mtime_ns) for p in Path(store.dir).glob("*"))
    except OSError:
        return "unreadable"
    return hashlib.sha256(repr(entries).encode()).hexdigest()[:16]


def with_carried_items(brief: Any, held: frozenset[str], who: Any, state_dir: Any) -> Any:
    """The meeting brief with the open action items of the series' earlier meetings (meeting
    follow-ups the reader may see) in its "Last meeting" section."""
    if not isinstance(brief, dict) or brief.get("kind") != "meeting":
        return brief
    store = _follow_ups(state_dir)
    if store is None or who is None:
        return brief
    from operating import meeting_followup as mf
    meeting = {"title": (brief.get("subject") or {}).get("label") or brief.get("title"),
               "start": brief.get("when") or ""}
    rows = mf.carried_items(store, meeting, _viewer(who, held))
    for section in brief.get("sections") or ():
        if section.get("key") == "notes" and rows:
            section["rows"] = list(section.get("rows") or ()) + [
                {**r, "sources": list(r.get("sources") or ())} for r in rows]
            section["state"] = "shown"
            section["note"] = None
    return brief


def follow_up_of(view: Any, meeting_id: str, who: Any, state_dir: Any
                 ) -> Optional[dict[str, Any]]:
    from operating import meeting_followup as mf
    held, store = _held(view), _follow_ups(state_dir)
    if held is None or store is None:
        return None
    record = store.get(meeting_id)
    if record is None:
        return None
    with view._request():
        return mf.page(record, _viewer(who, held), view._call, held, decide=_decide(view))


def meeting_card_of(view: Any, card_id: str, who: Any, state_dir: Any
                    ) -> Optional[dict[str, Any]]:
    """The drafted message of a meeting action item (card id ``meeting:<item id>``), rebuilt
    now from a follow-up this reader may see (never from the request)."""
    from operating import meeting_followup as mf
    held, store = _held(view), _follow_ups(state_dir)
    if held is None or store is None or not card_id.startswith("meeting:"):
        return None
    viewer = _viewer(who, held)
    for record in store.all():
        if not mf.visible(record, viewer):
            continue
        for action in record.get("actions") or ():
            if "meeting:" + action["id"] != card_id:
                continue
            with view._request():
                work = mf.prepare(record["meeting"], action, view._call, held,
                                  decide=_decide(view))
            return work.get("card")
    return None


def action_items_of(view: Any, person: Optional[str], who: Any, state_dir: Any
                    ) -> Optional[list[dict[str, Any]]]:
    from operating import meeting_followup as mf
    held, store = _held(view), _follow_ups(state_dir)
    if held is None or store is None:
        return None
    return mf.my_items(store, _viewer(who, held), person)


def _grant_key(view: Any, held: frozenset[str]) -> Optional[str]:
    """The reader's grant now (policy fingerprint, owner-open, people exclusion stamp and
    held markings): a kept week is served only under the same one. None: never kept."""
    import hashlib
    try:
        from operating.pg.permissions import _fingerprint, exclusion_stamp
        gated, policy = view.access.reader_policy()
        parts = [str(_fingerprint(policy, gated)), str(bool(getattr(policy, "unrestricted",
                                                                       False))),
                 str(exclusion_stamp()), ",".join(sorted(held))]
    except Exception as error:  # noqa: BLE001 - an undecidable grant keeps nothing
        import logging
        logging.getLogger("operator_api").info("weekly review not kept: grant undecided "
                                               "(%s)", type(error).__name__)
        return None
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def weekly_review_of(view: Any, week: Optional[date], *, archive: Any = None,
                     reader: Optional[str] = None, today: Optional[date] = None,
                     built: Any = None) -> Optional[dict[str, Any]]:
    """The weekly review as this reader reads it, whole (no time budget; ``built(seconds)``
    is told how long a build took, never an archive read); a finished week is
    kept in ``archive`` for ``reader`` and served from it while the grant is the same and
    every source it shows still opens for the reader."""
    held = _held(view)
    if held is None:
        return None
    today = today or date.today()
    start = br.week_of(week) if week else br.week_of(today) - timedelta(days=7)
    decide = _decide(view)
    grant = None
    if archive is not None and reader:
        with view._request():
            grant = _grant_key(view, held)
            kept = archive.get(reader, start, grant, decide) if grant else None
        if kept is not None:
            return kept
    import time
    began = time.monotonic()
    alerts = _alerts(view, resolved=True)
    kept = archive is not None and bool(reader) and grant is not None \
        and start + timedelta(days=7) <= today
    with view._request():
        raw = br.weekly_review(view._call, held, alerts, week=start, today=today,
                               decide=decide, people=_people(), budget_seconds=None)
    if built is not None:
        built(time.monotonic() - began)
    import logging
    log = logging.getLogger("operator_api")
    if archive is not None and reader and grant:
        if archive.put(reader, start, grant, raw, today):
            log.info("weekly review of %s kept", start.isoformat())
        elif kept:
            log.info("weekly review not kept: %s", "cut (%s)" % (raw.get("budget") or {}).get(
                "stopped") if (raw.get("budget") or {}).get("stopped") else "archive unwritable")
    elif archive is not None:
        log.info("weekly review not kept: %s", "no reader" if not reader else "no grant")
    return raw


def _people() -> Any:
    """The people exclusion the calendar and meeting briefs read with: the API's read-path
    guard (``feedback.address_guard``: the mail capture's directory attached, an outsider's
    address decided by the tenant's mail domains), as mail and search decide addresses.
    The passes' bare guard (``br.env_people``) decides no address at all, so every meeting
    naming anyone was dropped: the owner's Meetings page said "No meetings" over 81 events
    in the next two weeks (owner walk 11-17)."""
    from .feedback import address_guard
    return address_guard()


def upcoming_of(view: Any, who: Any = None, state_dir: Any = None
                ) -> Optional[list[dict[str, Any]]]:
    """The reader's calendar from the start of today (business time) for two weeks, each meeting
    with the open action items of its series' earlier meetings the reader may see."""
    from datetime import datetime
    held = _held(view)
    if held is None:
        return None
    from zoneinfo import ZoneInfo
    from operating.time_window import BUSINESS_TZ
    since = datetime.now(ZoneInfo(BUSINESS_TZ)).replace(hour=0, minute=0, second=0,
                                                        microsecond=0)
    # the reader's own meetings (their addresses and object ids): one who may read every
    # calendar is not shown everyone's
    viewer = _viewer(who, held) if who is not None else None
    mine = None if viewer is None else (
        {a for a in viewer.addresses if a} | {o.rpartition(":")[2] for o in viewer.oids if o})
    with view._request():
        raw = br.upcoming_meetings(view._call, held, people=_people(), since=since,
                                   reader=mine or None)
    store = _follow_ups(state_dir) if who is not None else None
    if store is not None:
        from operating import meeting_followup as mf
        for e in raw:
            if e.get("private") or e.get("people_withheld"):
                continue
            meeting = {"title": e.get("label"), "start": e.get("start") or "",
                       "organizer": e.get("organizer") or {}}
            try:
                e["open_items"] = mf.carried_items(store, meeting, viewer)[:10]
            except Exception:  # noqa: BLE001 - an unreadable store shows no item
                e["open_items"] = []
    return raw


def upcoming_model(view: Any, who: Any = None, state_dir: Any = None) -> m.UpcomingMeetings:
    """``upcoming_of`` as the route's model (no meeting for a reader without a grant)."""
    raw = upcoming_of(view, who, state_dir) or []
    return m.UpcomingMeetings(meetings=[upcoming_entry(e) for e in raw])


def upcoming_entry(e: Mapping[str, Any]) -> m.UpcomingMeeting:
    def invitee(p: Any) -> Optional[m.MeetingInvitee]:
        if not isinstance(p, Mapping):
            return None
        cleaned = dict(p)
        if cleaned.get("name"):
            cleaned["name"] = clean_person(cleaned["name"]) or clean_text(cleaned["name"]) or None
        return m.MeetingInvitee(**{**cleaned, "sources": _sources(p.get("sources"))})
    attendees = [a for a in (invitee(p) for p in e.get("attendees") or ()) if a]
    parties = [r for r in (_ref(p.get("object_id"), p.get("label"))
                           for p in e.get("parties") or ()) if r]
    label = clean_subject(e["label"], fallback="(no subject)", max_len=200)
    agenda = clean_text(e.get("agenda"), max_len=280, strip_email_noise=True) or None
    if agenda and agenda.lower() == label.lower():
        agenda = None
    return m.UpcomingMeeting(
        object=_ref(e["object_id"], label), start=e.get("start"), end=e.get("end"),
        all_day=bool(e.get("all_day")), location=clean_location(e.get("location")),
        online=bool(e.get("online")), join_url=e.get("join_url"), agenda=agenda,
        organizer=invitee(e.get("organizer")), attendees=attendees,
        is_organizer=bool(e.get("is_organizer")), private=bool(e.get("private")),
        people_withheld=bool(e.get("people_withheld")), parties=parties,
        open_items=[m.MeetingOpenItem(label=clean_text(i["label"]),
                                      detail=clean_text(i.get("detail"), strip_email_noise=True) or None,
                                      date=i.get("date"), sources=_sources(i.get("sources")))
                    for i in e.get("open_items") or ()],
        brief_url=brief_url(e["object_id"]), sources=_sources(e.get("sources")))


PACK_CARD = "pack:"
NUDGE_CARD = "nudge:"


def nudge_card(item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """A queue item's nudge as a card ``ActionDesk`` accepts (sent only through its preview
    and the reader's confirm); addressed to the first approver, cited by the alert."""
    nudge = item.get("nudge") or {}
    cites = [c for c in item.get("citations") or () if isinstance(c, str)]
    if nudge.get("channel") != "email" or not item.get("id"):
        return None
    to = [t["address"] for t in nudge.get("to") or () if t.get("address")]
    card_id = NUDGE_CARD + str(item["id"])
    if len(card_id) > 200:
        return None
    headline = clean_text(item.get("summary") or "", max_len=300, strip_email_noise=True)
    return {"id": card_id, "headline": headline,
            "subject": {"label": clean_subject(headline, fallback="Approval waiting", max_len=200)},
            "action": {"text": ""},
            "draft": {"channel": "email",
                      "to": {"address": to[0], "source": cites[0]} if to and cites else None,
                      "subject": clean_subject(nudge.get("subject"), fallback="") or nudge.get("subject"),
                      "body": nudge.get("body"), "facts": []},
            "teams_draft": None, "playbook": True}


def record_pack_of(view: Any, object_id: str, environ: Mapping[str, str]
                   ) -> Optional[dict[str, Any]]:
    """The record's document pack as this reader reads it (``operating.document_pack``)."""
    from operating import document_pack as dp
    held = _held(view)
    if held is None:
        return None
    with view._request():
        try:
            return dp.build(view._call, held, object_id, decide=_decide(view),
                            environ=environ)
        except dp.PackRefused:
            return None


def approvals_of(view: Any, today: Optional[date] = None) -> Optional[dict[str, Any]]:
    """The approvals queue from this reader's own trimmed alert list."""
    from operating import proactive_threads as pt
    found = _alerts(view)
    if not isinstance(found, Mapping) or found.get("status") != "read":
        return None
    return pt.approvals_queue(found.get("gaps") or (), today or date.today())


REPLY_CARD = "reply:"


BLANK = re.compile(r"\[[^\[\]\n]{1,60}\]")


def reply_card(line: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """A triage line's drafted reply as a card ``ActionDesk`` accepts (sent only through its
    preview and the reader's confirm); addressed to the asker, cited by the alert."""
    draft = line.get("draft") or {}
    cites = [c for c in line.get("citations") or () if isinstance(c, str)]
    if draft.get("channel") != "email" or not line.get("id"):
        return None
    to = [t["address"] for t in draft.get("to") or () if t.get("address")]
    card_id = REPLY_CARD + str(line["id"])
    if len(card_id) > 200:
        return None
    body = draft.get("body")
    # a model draft's [bracketed blanks] are the person's to fill: the send refuses while any
    # is still in the text (``card_actions.review``)
    blanks = (sorted(set(BLANK.findall(body))) if line.get("drafted_by") == "model"
              and isinstance(body, str) else [])
    return {"id": card_id,
            "headline": clean_text(line.get("summary") or "", max_len=300, strip_email_noise=True),
            "subject": {"label": clean_subject(line.get("subject"), fallback="Needs your reply", max_len=200)},
            "action": {"text": ""},
            "draft": {"channel": "email",
                      "to": {"address": to[0], "source": cites[0]} if to and cites else None,
                      "subject": clean_subject(draft.get("subject"), fallback="") or draft.get("subject"),
                      "body": body, "facts": [],
                      "blanks": blanks},
            "teams_draft": None, "playbook": True}


def replies_of(view: Any, today: Optional[date] = None) -> Optional[dict[str, Any]]:
    """The reader's "needs my reply" list from their own trimmed alert list (owner kinds
    reach only their owner: ``proactive_engine.read_inbox``)."""
    from operating import inbox_triage as it
    found = view.gap_inbox(kinds=list(it.TRIAGE_KINDS), limit=bf_max())
    if not isinstance(found, Mapping) or found.get("status") != "read":
        return None
    return it.triage(found.get("gaps") or (), today or date.today())


def label_changes_of(view: Any, object_id: str) -> Optional[dict[str, Any]]:
    from operating import label_changes as lc
    found = view.gap_inbox(kinds=list(lc.KINDS), limit=bf_max())
    if not isinstance(found, Mapping) or found.get("status") != "read":
        return None
    return lc.item_section(found.get("gaps") or (), object_id)


def label_revisions_of(view: Any, object_id: str) -> Optional[dict[str, Any]]:
    from operating import label_revisions as lr
    found = view.gap_inbox(kinds=list(lr.KINDS), limit=bf_max())
    if not isinstance(found, Mapping) or found.get("status") != "read":
        return None
    return lr.history(found.get("gaps") or (), object_id)


def bf_max() -> int:
    from operating import briefing as bf
    return bf.MAX_READ


def labour_held(reader: Reader) -> bool:
    """Whether the reader holds hr or ops-management (the host's grants file, read now), or
    is the owner-open reader (full read over every source and every mailbox, decided by the
    access layer at sign-in; nothing in a request can claim it): the owner sees labour
    hours without a separate grant."""
    from operating import labour_hours as lh
    from operator_api.feedback import owner_open
    if owner_open(reader.session):
        return True
    return lh.holds_labour(getattr(reader.session, "principals", ()) or (), lh.load_grants())


def labour_page_of(state_dir: Any, reader: Reader, weeks: Optional[int] = None
                   ) -> dict[str, Any]:
    from operating import labour_hours as lh
    held = labour_held(reader)
    return lh.labour_page(lh.open_store(state_dir) if held else None, held, weeks)


def labour_answer_of(state_dir: Any, reader: Reader, question: str) -> dict[str, Any]:
    from operating import labour_hours as lh
    held = labour_held(reader)
    return lh.answer(question, lh.open_store(state_dir) if held else None, held)


# The reader's alert list (the gap inbox) not read on this server: not "not found or no
# access", which the app says for a 404 not_found (the UX walk of 11-13 showed Team's
# approvals as "you don't have access" while the alert run had not run)
def alerts_not_read() -> UserFacing:
    return UserFacing("alerts_not_read", "Your alert list could not be read.", 404)

class BriefsMixin:
    """The briefs routes' backend (``WorkspaceBackend`` mixes it in)."""

    def record_pack_dict(self, reader: Reader, object_id: str) -> Optional[dict[str, Any]]:
        env = self._actions_env() if hasattr(self, "_actions_env") else {}  # type: ignore
        return self._brief_run(reader, lambda view: record_pack_of(view, object_id, env))

    def record_pack(self, reader: Reader, object_type: str, object_id: str) -> m.RecordPack:
        from operating import document_pack as dp
        if object_type not in dp.PACK_TYPES or br.type_of(object_id) != object_type:
            raise UserFacing("not_found", "A document pack is made for an order, a purchase "
                             "order, an invoice, a shipment, a receipt or a lot.", 404)
        raw = self.record_pack_dict(reader, object_id)
        if raw is None:
            raise UserFacing("not_found", "That record is not one you can open.", 404)
        card_id = PACK_CARD + object_id
        email = raw["draft"]
        return m.RecordPack(
            object_id=raw["object_id"], object_type=raw["object_type"], label=raw["label"],
            documents=[m.RecordPackDocument(**{k: v for k, v in d.items()
                                               if k in m.RecordPackDocument.model_fields})
                       for d in raw["documents"]],
            expected=raw["expected"], missing=raw["missing"],
            missing_labels=raw["missing_labels"], records_read=raw["records_read"],
            links=[m.PlaybookLink(**link) for link in raw["links"]],
            card_id=card_id if len(card_id) <= 200 else None,
            draft=m.RecordPackDraft(**email), draft_missing=raw["draft_missing"],
            offers=[m.CardActionOffer(**o) for o in ca.offers(raw["card"])],
            gaps=raw["gaps"], basis=raw["basis"], sent=False, writes=0)

    def meeting_follow_up(self, reader: Reader, meeting_id: str) -> m.MeetingFollowUp:
        raw = self._brief_run(reader, lambda view: follow_up_of(
            view, meeting_id, reader, getattr(self, "state_dir", None)))
        if raw is None:
            raise UserFacing("not_found", "That meeting is not one you attended.", 404)
        for action in raw.get("actions") or ():
            card = (action.get("prepared") or {}).get("card")
            if card:
                action["prepared"]["offers"] = ca.offers(card)
        return m.MeetingFollowUp(follow_up=raw)

    def meeting_card(self, reader: Reader, card_id: str) -> Optional[dict[str, Any]]:
        return self._brief_run(reader, lambda view: meeting_card_of(
            view, card_id, reader, getattr(self, "state_dir", None)))

    def my_action_items(self, reader: Reader, person: Optional[str]) -> m.MyActionItems:
        from operating import meeting_followup as mf
        state_dir = getattr(self, "state_dir", None)
        worker = getattr(self, "access_worker", None)
        if worker is not None and not worker.stopped.is_set():
            # action_items_of only needs the reader's held markings (no ontology tool calls):
            # read them on the fast access worker so follow-ups never stall behind slow walks
            # on self.reads.
            held = worker.run(reader, _held, timeout=15, priority=-1)
            store = _follow_ups(state_dir) if held is not None else None
            raw = mf.my_items(store, _viewer(reader, held), person) if store is not None else None
        else:
            raw = self._brief_run(reader, lambda view: action_items_of(
                view, person, reader, state_dir))
        if raw is None:
            raise UserFacing("not_found", "No meeting follow-ups are kept here.", 404)
        return m.MyActionItems(person=person, items=raw)

    def approvals_queue(self, reader: Reader) -> m.ApprovalsQueue:
        raw = self._brief_run(reader, lambda view: approvals_of(view), priority=ENRICH_PRIORITY)
        if raw is None:
            raise alerts_not_read()
        for group in raw["groups"]:
            for item in group["items"]:
                card = nudge_card(item)
                if card is not None:
                    item["card_id"] = card["id"]
                    item["offers"] = ca.offers(card)
        return m.ApprovalsQueue(**{k: v for k, v in raw.items() if k != "schema"})

    def nudge_card_of(self, reader: Reader, card_id: str) -> Optional[dict[str, Any]]:
        """The nudge card ``card_id`` rebuilt from this reader's own queue (None: gone)."""
        raw = self._brief_run(reader, lambda view: approvals_of(view))
        for group in (raw or {}).get("groups") or ():
            for item in group["items"]:
                card = nudge_card(item)
                if card is not None and card["id"] == card_id:
                    return card
        return None

    def reply_triage(self, reader: Reader) -> m.ReplyTriage:
        raw = self._brief_run(
            reader,
            lambda view: self._pg_replies(reader, view) or replies_of(view),
            priority=ENRICH_PRIORITY,
        )
        if raw is None:
            raise alerts_not_read()
        lines = raw["lines"] = self._owed_lines(reader, raw["lines"])
        raw["total"] = len(lines)
        self._with_drafts(reader, lines)
        for line in lines:
            card = reply_card(line)
            if card is not None:
                line["card_id"] = card["id"]
                line["offers"] = ca.offers(card)
        return m.ReplyTriage(**{k: v for k, v in raw.items() if k != "schema"})

    def _pg_replies(self, reader: Reader, view: Any) -> Optional[dict[str, Any]]:
        """Read open ``reply_owed`` (``needs_my_reply``) alerts from ``biz.alerts`` when
        Postgres is configured, strictly fail-closed on ``view.access.grant()``, comms
        units, and own mailboxes."""
        if not getattr(self, "pg_dsn", None) or not callable(getattr(self, "pg", None)):
            return None
        my_oids = self._my_oids(reader)
        if not my_oids:
            return None
        grant = getattr(getattr(view, "access", None), "grant", None)
        if grant is None:
            return None
        try:
            may_read, held = grant()
        except Exception:  # noqa: BLE001 - fail closed
            return None
        may_set = set(may_read or ())
        held_set = set(held or ())
        if not may_set:
            return None
        from operating.pg import alerts as pg_alerts
        today = self.today() if getattr(self, "today", None) else date.today()
        try:
            with self.pg().connection() as conn:
                unit_rows = conn.execute(
                    "SELECT unit, audience_key, marks FROM biz.comms_units"
                ).fetchall()
                units = [
                    int(u) for u, ak, marks in unit_rows
                    if ak in may_set and set(marks or ()) <= held_set
                ]
                if not units:
                    return {
                        "as_of": today.isoformat(), "total": 0,
                        "lines": [], "sent": 0, "writes": 0,
                    }
                rows = pg_alerts.read(
                    conn, classes=(), units=units,
                    mailboxes=sorted(my_oids), me=sorted(my_oids),
                    kinds=["reply_owed"], state="open", ai=False, limit=bf_max(),
                )
        except Exception:  # noqa: BLE001 - fail closed, fall back to gap_inbox if needed
            return None
        lines: list[dict[str, Any]] = []
        for row in rows:
            since = row.get("since")
            days = max(0, (today - since.date()).days) if since is not None else 0
            why_clean = clean_text(row["why"], strip_email_noise=True)
            lines.append({
                "id": f"pg:{row['rule']}:{row['handle']}",
                "kind": row["kind"],
                "summary": why_clean,
                "channel": "mail",
                "sent": since.isoformat() if since is not None else None,
                "subject": None,
                "days": days,
                "records_named": {},
                "customer_tier": None,
                "chases": 0,
                "rank": [0, 0, 0, min(days, 99)],
                "new_inquiry": False,
                "object_id": row["handle"],
                "citations": [row["handle"]],
                "draft": {
                    "channel": "email", "status": "draft_not_sent", "sent": False,
                    "subject": "Re: Needs your reply", "body": "",
                },
            })
        return {
            "as_of": today.isoformat(), "total": len(lines),
            "lines": lines, "sent": 0, "writes": 0,
        }

    def _owed_lines(self, reader: Reader, lines: list[dict[str, Any]]
                    ) -> list[dict[str, Any]]:
        """The lines whose message the triage model did not read as owed nothing (fyi,
        automated, notice: ``operating.autopilot_triage``, kept for the reader's own
        mailboxes in the autopilot queue); a message with no reading stays."""
        from operating import autopilot_triage as at
        state = getattr(self, "state_dir", None)
        if not state or not lines:
            return lines
        kinds = at.readings(Path(state) / "autopilot" / "autopilot.sqlite",
                            sorted(self._my_oids(reader)),
                            [str(x.get("object_id") or "") for x in lines])
        return [x for x in lines if kinds.get(str(x.get("object_id") or ""), "reply")
                == "reply"]

    def _with_drafts(self, reader: Reader, lines: list[dict[str, Any]]) -> None:
        """The model's drafts the background job kept for these lines (``reply_drafts``), in
        place: read, never waited on; the holding sentence stays until one is ready."""
        from operating import reply_drafts as rd
        rd.attach(lines, rd.kept_for(getattr(self, "state_dir", None), self._my_oids(reader),
                                     lines))

    def reply_card_of(self, reader: Reader, card_id: str) -> Optional[dict[str, Any]]:
        """The reply card ``card_id`` rebuilt from this reader's own list (None: gone)."""
        raw = self._brief_run(reader, lambda view: replies_of(view))
        lines = (raw or {}).get("lines") or []
        # the same kept model draft the list shows: the send dialog sends what the person
        # read on the line, never the holding body
        self._with_drafts(reader, lines)
        for line in lines:
            card = reply_card(line)
            if card is not None and card["id"] == card_id:
                return card
        return None

    def label_changes(self, reader: Reader, object_id: str) -> m.LabelChanges:
        raw = self._brief_run(reader, lambda view: label_changes_of(view, object_id))
        if raw is None:
            raise alerts_not_read()
        return m.LabelChanges(**raw)

    def label_revisions(self, reader: Reader, object_id: str) -> m.LabelRevisions:
        raw = self._brief_run(reader, lambda view: label_revisions_of(view, object_id))
        if raw is None:
            raise alerts_not_read()
        return m.LabelRevisions(**raw)

    @staticmethod
    def _my_oids(reader: Reader) -> frozenset[str]:
        from operating.proactive_commitments import owner_ids
        principals = ",".join(sorted(getattr(reader.session, "principals", ()) or ()))
        return frozenset(owner_ids(principals))

    def answer_bank(self, reader: Reader, question: Optional[str] = None) -> m.DocList:
        """Past questionnaire answers any customer may get, read as this reader."""
        from .record_documents import answer_bank
        today = self.today() if getattr(self, "today", None) else date.today()
        got = self._brief_run(reader, lambda view: answer_bank(view, question, today))
        return got if isinstance(got, m.DocList) else m.DocList(
            view="answer_bank", title="Answers any customer may get", complete=False,
            error="The list could not be read.")

    def _is_owner(self, reader: Reader) -> bool:
        """The owner-open reader: ``feedback.owner_open``, the one owner test (S-16). The
        session carries the full-read code the access layer set when it resolved the signed-in
        reader, only for a grant over every source and every mailbox that is usable now
        (``EmployeeEntitlement.full_read_code``; the same rule as ``unrestricted``); nothing
        in a request can claim it."""
        from operator_api.feedback import owner_open
        return owner_open(getattr(reader, "session", None))

    def _workload_grants(self) -> Any:
        from operating import inbox_triage as it
        data_dir = getattr(self, "data_dir", None)
        return it.WorkloadGrants(Path(data_dir) / it.GRANTS_FILE) if data_dir else None

    def workload(self, reader: Reader) -> m.Workload:
        """Counts per team for every signed-in reader; per person only the reader's own and
        the people of teams the owner granted the reader. The people exclusion fails closed:
        unavailable, nobody is counted per person or by team for their threads."""
        from operating import inbox_triage as it
        from operating import proactive_engine as pe
        from operating.proactive_commitments import OWNER_KINDS, owner_ids
        grants = self._workload_grants()
        store = getattr(self, "store_path", None)
        rows = it.open_rows(pe.inbox_path(store)) if store else []
        from .team_view import (HIDDEN_ENV, OWNER_IDS_FILE, _learn_owner, _learned_owners,
                                _listed, hidden_people, snapshot)
        guard = it.excluded_by(br.env_people())
        data_dir = getattr(self, "data_dir", None)
        owners_file = Path(data_dir) / OWNER_IDS_FILE if data_dir else None
        if self._is_owner(reader):
            _learn_owner(owners_file, owner_ids(",".join(sorted(
                getattr(reader.session, "principals", ()) or ()))))
        snap = snapshot(self._team_file()) if hasattr(self, "_team_file") else None
        hidden = hidden_people(_listed(HIDDEN_ENV), _learned_owners(owners_file),
                               (snap or {}).get("people"))

        def excluded(oid: str) -> bool:   # the owner is counted in no people view
            return oid in hidden or guard(oid)
        principals = ",".join(sorted(getattr(reader.session, "principals", ()) or ()))
        raw = it.workload(rows, reader=owner_ids(principals),
                          grants=grants.load() if grants else {"teams": {}, "managers": {}},
                          owner_kinds=OWNER_KINDS, excluded=excluded)
        # a name for each person already shown (the reader's own, a granted team's people)
        names = (snap or {}).get("people") or {}
        for row in raw.get("people") or ():
            row["name"] = str((names.get(row["person"]) or {}).get("name") or "") or None
        return m.Workload(**{k: v for k, v in raw.items() if k != "schema"})

    def _grants_owner(self, reader: Reader) -> Any:
        """The grants file for the owner (``_is_owner``: ``feedback.owner_open``) only: 403
        for everyone else, a listed full reader too (its read policy is unrestricted, but
        assigning who sees other people's workload is the owner's), 404 with no data
        directory."""
        if not self._is_owner(reader):
            raise UserFacing("forbidden", "Only the owner assigns workload grants.", 403)
        grants = self._workload_grants()
        if grants is None:
            raise UserFacing("not_found", "No data directory is configured.", 404)
        return grants

    def _grants_gone(self, reader: Reader) -> Any:
        """Who the grant sheet never shows: the owner, the hidden list and the people
        exclusion. The exclusion unavailable: 503, before anything is read or written."""
        people = br.env_people()
        if people is None or getattr(people, "unavailable", None):
            raise UserFacing("people_exclusion_unavailable", "The list of people the Workbench "
                             "leaves out isn't loaded, so the grants can't be read or changed. "
                             "Nothing was changed.", 503)
        _everyone, _me, gone, _known = self._team_scope(reader)  # type: ignore[attr-defined]
        return gone

    def workload_grant_sheet(self, reader: Reader) -> m.WorkloadGrantSheet:
        """The owner's grant editor: each person of the team snapshot the exclusion clears,
        with their team and the teams they see per person. Entries for people the sheet
        never shows are counted, not listed; an assignment keeps them as they are."""
        from .team_view import snapshot
        grants = self._grants_owner(reader)
        current = grants.load()
        gone = self._grants_gone(reader)
        teams, managers = current["teams"], current["managers"]
        people = {oid: str((row or {}).get("name") or "") for oid, row in (
            (snapshot(self._team_file()) or {}).get("people") or {}).items()}  # type: ignore[attr-defined]
        listed = {o for o in set(people) | set(teams) | set(managers) if not gone(o)}
        rows = [m.WorkloadGrantPerson(oid=o, name=people.get(o) or None, team=teams.get(o),
                                      sees=managers.get(o, []), in_snapshot=o in people)
                for o in sorted(listed, key=lambda o: ((people.get(o) or "~").lower(), o))]
        held = sum(1 for o in set(teams) | set(managers) if gone(o))
        codes = sorted(set(teams.values()) | {t for v in managers.values() for t in v})
        return m.WorkloadGrantSheet(people=rows, teams=codes, held_back=held)

    def assign_workload_grants(self, reader: Reader, body: m.WorkloadGrants) -> m.WorkloadGrants:
        """Only the owner-open reader assigns teams and grants. The body is the whole
        assignment, except that entries for people the sheet never shows (the owner, the
        hidden list, the people exclusion) are kept as they are: the editor cannot see them,
        so it can neither change nor drop them."""
        from operating import inbox_triage as it
        grants = self._grants_owner(reader)
        try:
            wanted = it.validate(body.model_dump())
        except it.GrantsInvalid as bad:
            raise UserFacing("invalid_request", str(bad), 422)
        gone = self._grants_gone(reader)
        current = grants.load()
        for part in ("teams", "managers"):
            wanted[part] = {o: v for o, v in wanted[part].items() if not gone(o)}
            wanted[part].update({o: v for o, v in current[part].items() if gone(o)})
        try:
            kept = grants.assign(wanted)
        except it.GrantsInvalid as bad:
            raise UserFacing("invalid_request", str(bad), 422)
        return m.WorkloadGrants(teams={o: t for o, t in kept["teams"].items() if not gone(o)},
                                managers={o: v for o, v in kept["managers"].items()
                                          if not gone(o)})

    def _brief_run(self, reader: Reader, job: Any, *, priority: int = 0,
                   timeout: Optional[float] = BRIEF_TIMEOUT_SECONDS, long: bool = False) -> Any:
        # an old-engine enrichment read (``ENRICH_PRIORITY``) is a long read; ``timeout``
        # None: no time limit (the weekly review, whole whatever it takes)
        return self.reads.run(reader, job, timeout=timeout,  # type: ignore
                              priority=priority, long=long or priority >= ENRICH_PRIORITY)

    def meeting_brief(self, reader: Reader, object_type: str, object_id: str) -> m.Brief:
        """The brief, built fast through the reader's Postgres door (``operating.
        meeting_brief_door``) when available; else drawn from the one prepared ahead or built
        whole via the primitives fallback. The person's own notes about the meeting and its
        parties are added to the draw."""
        from operating import meeting_brief_door as mbd
        from . import meeting_briefs as mb
        if object_type not in br.MEETING_TYPES + br.PARTY_TYPES \
                or br.type_of(object_id) != object_type:
            raise UserFacing("not_found", "A brief is prepared for a calendar event, a "
                             "meeting, a customer or a supplier.", 404)
        if getattr(self, "doors", None) is not None:
            try:
                raw = mbd.build_for_reader(self, reader, object_id)
            except br.BriefRefused:
                raise UserFacing("not_found", "That record is not one you can open.", 404)
            except mbd.DoorUnavailable:
                raw = None
            if raw is not None:
                held = self._door_held_markings(reader)
                raw = with_carried_items(raw, held, reader, getattr(self, "state_dir", None))
                return self._with_notes(reader, brief_model(raw))
        store = self._meeting_store()
        try:
            raw = self._brief_run(reader, lambda view: meeting_brief_of(
                view, object_id, reader, getattr(self, "state_dir", None), store=store,
                reader=reader.key), timeout=None, long=True)
        except br.BriefRefused:
            raw = None
        if raw is None:
            raise UserFacing("not_found", "That record is not one you can open.", 404)
        if raw.get("kept") and raw.get("kept_age", 0) > mb.REFRESH_AFTER:
            self._prepare_ahead(reader, [object_id], refresh=True)
        return self._with_notes(reader, brief_model(raw))

    def _door_held_markings(self, reader: Reader) -> frozenset[str]:
        doors = getattr(self, "doors", None)
        if doors is None:
            return frozenset()
        held = doors.get(reader)
        if held is None:
            return frozenset()
        return frozenset(getattr(getattr(held[0], "grant", None), "held", ()) or ())

    def _meeting_store(self) -> Any:
        from . import meeting_briefs as mb
        data_dir = getattr(self, "data_dir", None)
        return mb.MeetingBriefStore(Path(data_dir) / mb.DIR) if data_dir is not None else None

    def _with_notes(self, reader: Reader, brief: m.Brief) -> m.Brief:
        """``brief`` with the reader's own notes about it (``your_notes``, first section)."""
        from . import meeting_briefs as mb
        store = getattr(self, "memory", None)
        ids = {r.object_id for r in [brief.subject, *brief.parties] if r is not None}
        if store is None or not ids:
            return brief
        try:
            items = store.items(reader, limit=None, about=sorted(ids))
        except Exception:  # noqa: BLE001 - said in the section, never an empty one
            import logging
            logging.getLogger("operator_api").warning("brief notes unreadable", exc_info=True)
            section = m.BriefSection(key="your_notes", title="Your notes", state="unread",
                                     note="Your notes could not be read just now.")
            return brief.model_copy(update={"sections": [section, *brief.sections]})
        raw = mb.notes_section(items, ids)
        section = m.BriefSection(
            key=raw["key"], title=raw["title"], state=raw["state"], note=raw["note"],
            rows=[m.BriefRow(**r) for r in raw["rows"]])
        return brief.model_copy(update={"sections": [section, *brief.sections]})

    def _prepare_ahead(self, reader: Reader, object_ids: list[str], *,
                       refresh: bool = False) -> None:
        """        A
        failure is logged and the brief is built when the person opens it."""
        import logging
        import threading
        log = logging.getLogger("operator_api")
        store = self._meeting_store()
        if store is None:
            return

        def run() -> None:
            for oid in object_ids:
                try:
                    self._brief_run(reader, lambda view, oid=oid: meeting_brief_of(
                        view, oid, None, None, store=store, reader=reader.key,
                        refresh=refresh), timeout=None, priority=ENRICH_PRIORITY, long=True)
                except Exception as error:  # noqa: BLE001 - built on open instead
                    log.info("meeting brief not prepared ahead: %s", type(error).__name__)
        threading.Thread(target=run, name="brief-ahead", daemon=True).start()

    def prepare_day_ahead(self, reader: Reader, model: m.UpcomingMeetings) -> None:
        """After the reader's upcoming meetings were read: prepare the briefs of today's and
        tomorrow's, at most one pass per reader every ``AHEAD_EVERY`` seconds."""
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from operating.time_window import BUSINESS_TZ
        from . import meeting_briefs as mb
        passes = self.__dict__.setdefault("_ahead_passes", {})
        if time.monotonic() - passes.get(reader.key, -1e9) < mb.AHEAD_EVERY:
            return
        today = datetime.now(ZoneInfo(BUSINESS_TZ)).date()
        ids = []
        for e in model.meetings:
            try:
                day = datetime.fromisoformat(str(e.start)).astimezone(ZoneInfo(BUSINESS_TZ)).date()
            except (TypeError, ValueError):
                continue    # no readable start: prepared when opened
            if 0 <= (day - today).days <= 1 and e.object is not None and not e.private:
                ids.append(e.object.object_id)
        passes[reader.key] = time.monotonic()
        if ids:
            self._prepare_ahead(reader, ids)

    def _weekly_archive(self) -> Any:
        data_dir = getattr(self, "data_dir", None)
        return br.WeeklyArchive(data_dir / WEEKLY_FILE) if data_dir is not None else None

    def _weekly_ahead(self, reader: Reader) -> Any:
        """Last week's review for ``reader`` as ``/api/brief/weekly`` reads it, kept in the
        archive (read whole once, served from there after); nothing when no archive."""
        archive = self._weekly_archive()

        def job(view: Any) -> None:
            if archive is None:
                return
            try:
                weekly_review_of(view, None, archive=archive, reader=reader.key,
                                 built=self._weekly_took)
            except Exception as error:  # noqa: BLE001 - read when opened instead
                import logging
                import traceback
                where = traceback.extract_tb(error.__traceback__)[-1]
                logging.getLogger("operator_api.warm").info(
                    "weekly review not read ahead: %s at %s:%s", type(error).__name__,
                    Path(where.filename).name, where.lineno)
        return job

    def _weekly_times(self) -> Any:
        data_dir = getattr(self, "data_dir", None)
        return data_dir / WEEKLY_TIMES_FILE if data_dir is not None else None

    def _weekly_seconds(self) -> Optional[float]:
        """The usual time one reader's week took to build here (the median of the last
        ``WEEKLY_TIMES_KEPT`` builds, kept in ``WEEKLY_TIMES_FILE``); None before any."""
        import json
        import statistics
        path = self._weekly_times()
        try:
            seen = [float(x) for x in json.loads(path.read_text())] if path else []
        except (OSError, ValueError, TypeError):
            return None   # none measured yet (or unreadable): no estimate is given
        return statistics.median(seen) if seen else None

    def _weekly_took(self, seconds: float) -> None:
        import json
        import logging
        path = self._weekly_times()
        if path is None:
            return
        try:
            try:
                seen = [float(x) for x in json.loads(path.read_text())]
            except (OSError, ValueError, TypeError):
                seen = []
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps((seen + [round(seconds, 1)])[-WEEKLY_TIMES_KEPT:]))
            os.replace(tmp, path)
        except OSError as error:
            logging.getLogger("operator_api").warning(
                "weekly review build time not kept: %s", type(error).__name__)

    def weekly_reading(self, reader: Reader) -> m.Brief:
        """The weekly review as shown while its first build goes on (nothing of the
        reader's data in it): ``reading`` and, from the usual build time here, about when
        it will be ready."""
        from datetime import datetime, timedelta as td, timezone
        import time
        now = datetime.now(timezone.utc)
        usual = self._weekly_seconds()
        began = self.__dict__.get("_weekly_began", {}).get(reader.key)
        ready = None
        if usual is not None:
            left = usual - (time.monotonic() - began) if began is not None else usual
            ready = (now + td(seconds=max(left, 0.0))).isoformat(timespec="seconds")
        return m.Brief(kind="weekly", title="Weekly review", as_of=now.isoformat(timespec="seconds"),
                       sections=[], basis="This review is being built from every record you may "
                       "read; nothing is left out, so the first one takes a few minutes.",
                       reading=True, ready_about=ready)

    def weekly_review(self, reader: Reader, week: Optional[date], *,
                      ahead: bool = False) -> m.Brief:
        """``ahead``: the warm-up's screen read (the same read, whole, kept in the archive).
        No time limit: a reader's week is read whole (424-714 s), served from the archive
        once built; until then the route answers ``weekly_reading``."""
        import time
        archive = self._weekly_archive()
        started = self.__dict__.setdefault("_weekly_began", {})
        started[reader.key] = time.monotonic()
        try:
            raw = self.weekly_run(reader, lambda view: weekly_review_of(
                view, week, archive=archive, reader=reader.key, built=self._weekly_took))
        except br.BriefRefused:
            raise UserFacing("invalid_request", "That week has not started yet.", 422)
        finally:
            started.pop(reader.key, None)
        if raw is None:
            raise UserFacing("not_found", "The weekly review needs a signed-in reader.", 404)
        return late_from_home(brief_model(raw), lambda: self.late_list(reader, limit=LATE_ROWS))

    def labour(self, reader: Reader, weeks: Optional[int] = None) -> m.LabourPage:
        """Department and week labour hours (``operating.labour_hours``): only for a reader
        holding the hr or ops-management grant; read from the aggregates store alone.
        ``weeks``: the last that many weeks (0 or none: every week)."""
        return m.LabourPage(**labour_page_of(getattr(self, "state_dir", None), reader, weeks))

    def upcoming_meetings(self, reader: Reader) -> m.UpcomingMeetings:
        try:
            got = self._brief_run(reader, lambda view: self._upcoming_cached(view, reader)) \
                or m.UpcomingMeetings(meetings=[])
            self.prepare_day_ahead(reader, got)   # the day's briefs, kept before they are opened
            return got
        except br.CalendarNotRead:
            # a failed search is never an empty calendar: the page offers to try again
            raise UserFacing("busy", "Your calendar could not be read just now. Try again "
                                     "in a minute.", 503) from None

    def _upcoming_cached(self, view: Any, who: Any = None) -> m.UpcomingMeetings:
        """The reader's upcoming meetings as read last for them this hour (``page_key``:
        their policy, the store state and the exclusion; the hour, as the list starts
        now; the reader themself and the follow-up store as it stands, as each meeting
        carries the open action items this reader may see), when every source it shows
        still opens for them; else read now and kept."""
        from datetime import datetime, timezone
        from .backend import page_key
        from .page_parts import page_handles
        state_dir = getattr(self, "state_dir", None)
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        with view._request():
            key = page_key(view, ("upcoming_meetings", hour, getattr(who, "key", None),
                                  _follow_up_stamp(state_dir) if who is not None else None))
        return self.page_cache.kept(view, key, lambda: upcoming_model(view, who, state_dir),
                                    page_handles,
                                    copy=lambda model: model.model_copy(deep=True))
