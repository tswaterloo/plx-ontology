"""The app's briefs (``operating.briefs``): a meeting prep brief per calendar event, meeting,
customer or supplier, the upcoming meetings to prepare, and the weekly review, each read
as the signed-in reader (their own tool calls, their own trimmed alert list, their
markings) on the read worker. No model call; an optional summary is a question for the
answer path (``/api/ask``) with its claim checks, never written here."""

from __future__ import annotations

from datetime import date, timedelta
import os
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

from operating import briefs as br
from operating import card_actions as ca

from . import models as m
from .auth import Reader
from .errors import UserFacing

BRIEF_TIMEOUT_SECONDS = 90


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
    return m.ObjectRef(object_id=object_id, type=kind, label=(label or kind)[:200],
                       open_url=object_url(kind, object_id))


def brief_url(object_id: str) -> str:
    return f"/brief/{quote(br.type_of(object_id), safe='')}/{quote(object_id, safe='')}"


def brief_model(raw: Mapping[str, Any]) -> m.Brief:
    sections = []
    for s in raw.get("sections") or ():
        rows = [m.BriefRow(label=r["label"], detail=r.get("detail"), date=r.get("date"),
                           who=r.get("who"),
                           amount=m.Money(**r["amount"]) if r.get("amount") else None,
                           object=_ref(r.get("object_id"), None),
                           sources=_sources(r.get("sources"))) for r in s.get("rows") or ()]
        figures = [m.BriefFigure(**{**f, "sources": _sources(f.get("sources"))})
                   for f in s.get("figures") or ()]
        sections.append(m.BriefSection(key=s["key"], title=s["title"], state=s["state"],
                                       note=s.get("note"), rows=rows, more=s.get("more", 0),
                                       figures=figures))
    subject = raw.get("subject") or {}
    question = None
    if raw.get("kind") == "meeting" and subject.get("label"):
        question = (f"Prepare me for the meeting \"{subject['label']}\": what should I know "
                    "about the open orders, late items, promises and quality issues?")
    elif raw.get("kind") == "weekly":
        question = f"Summarise the {raw.get('title', 'weekly review').lower()}."
    return m.Brief(kind=raw["kind"], title=str(raw.get("title") or "")[:200],
                   subject=_ref(subject.get("object_id"), subject.get("label")),
                   subject_sources=_sources(raw.get("subject_sources")),
                   when=raw.get("when"), as_of=raw["as_of"],
                   parties=[r for r in (_ref(p["object_id"], p.get("label"))
                                        for p in raw.get("parties") or ()) if r],
                   possible_parties=[r for r in (_ref(p["object_id"], p.get("label"))
                                                 for p in raw.get("possible_parties") or ())
                                     if r],
                   sections=sections, gaps=[m.BriefGap(**g) for g in raw.get("gaps") or ()],
                   basis=raw.get("basis") or "", summary_question=question,
                   stored_at=raw.get("stored_at"))


def _held(view: Any) -> frozenset[str] | None:
    grant = getattr(getattr(view, "access", None), "grant", None)
    if grant is None:
        return None
    with view._request():
        return grant()[1]


WEEKLY_FILE = "weekly-reviews.sqlite"


def _alerts(view: Any, *, resolved: bool = False) -> Mapping[str, Any]:
    from operating import briefing as bf
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


def meeting_brief_of(view: Any, object_id: str, who: Any = None, state_dir: Any = None
                     ) -> Optional[dict[str, Any]]:
    held = _held(view)
    if held is None:
        return None
    alerts = _alerts(view)
    with view._request():
        brief = br.meeting_brief(view._call, held, alerts, object_id, decide=_decide(view),
                                 people=br.env_people())
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
                {**r, "sources": [{"citation": c} for c in r["sources"]]} for r in rows]
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
    except Exception:  # noqa: BLE001 - an undecidable grant keeps nothing
        return None
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def weekly_review_of(view: Any, week: Optional[date], *, archive: Any = None,
                     reader: Optional[str] = None,
                     today: Optional[date] = None) -> Optional[dict[str, Any]]:
    """The weekly review as this reader reads it; a finished week is kept in ``archive``
    for ``reader`` and served from it while the grant is the same and every source it
    shows still opens for the reader."""
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
    alerts = _alerts(view, resolved=True)
    with view._request():
        raw = br.weekly_review(view._call, held, alerts, week=start, today=today,
                               decide=decide, people=br.env_people())
    if archive is not None and reader and grant:
        archive.put(reader, start, grant, raw, today)
    return raw


def upcoming_of(view: Any) -> Optional[list[dict[str, Any]]]:
    held = _held(view)
    if held is None:
        return None
    with view._request():
        return br.upcoming_meetings(view._call, held)


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
    return {"id": card_id, "headline": str(item.get("summary") or "")[:300],
            "subject": {"label": str(item.get("summary") or "Approval waiting")[:200]},
            "action": {"text": ""},
            "draft": {"channel": "email",
                      "to": {"address": to[0], "source": cites[0]} if to and cites else None,
                      "subject": nudge.get("subject"), "body": nudge.get("body"), "facts": []},
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
    return {"id": card_id, "headline": str(line.get("summary") or "")[:300],
            "subject": {"label": str(line.get("subject") or "Needs your reply")[:200]},
            "action": {"text": ""},
            "draft": {"channel": "email",
                      "to": {"address": to[0], "source": cites[0]} if to and cites else None,
                      "subject": draft.get("subject"), "body": draft.get("body"), "facts": []},
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


def bf_max() -> int:
    from operating import briefing as bf
    return bf.MAX_READ


def labour_held(reader: Reader) -> bool:
    """Whether the reader holds hr or ops-management (the host's grants file, read now)."""
    from operating import labour_hours as lh
    return lh.holds_labour(getattr(reader.session, "principals", ()) or (), lh.load_grants())


def labour_page_of(state_dir: Any, reader: Reader) -> dict[str, Any]:
    from operating import labour_hours as lh
    held = labour_held(reader)
    return lh.labour_page(lh.open_store(state_dir) if held else None, held)


def labour_answer_of(state_dir: Any, reader: Reader, question: str) -> dict[str, Any]:
    from operating import labour_hours as lh
    held = labour_held(reader)
    return lh.answer(question, lh.open_store(state_dir) if held else None, held)


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
        raw = self._brief_run(reader, lambda view: action_items_of(
            view, person, reader, getattr(self, "state_dir", None)))
        if raw is None:
            raise UserFacing("not_found", "No meeting follow-ups are kept here.", 404)
        return m.MyActionItems(person=person, items=raw)

    def approvals_queue(self, reader: Reader) -> m.ApprovalsQueue:
        raw = self._brief_run(reader, lambda view: approvals_of(view))
        if raw is None:
            raise UserFacing("not_found", "Your alert list could not be read.", 404)
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
        raw = self._brief_run(reader, lambda view: replies_of(view))
        if raw is None:
            raise UserFacing("not_found", "Your alert list could not be read.", 404)
        for line in raw["lines"]:
            card = reply_card(line)
            if card is not None:
                line["card_id"] = card["id"]
                line["offers"] = ca.offers(card)
        return m.ReplyTriage(**{k: v for k, v in raw.items() if k != "schema"})

    def reply_card_of(self, reader: Reader, card_id: str) -> Optional[dict[str, Any]]:
        """The reply card ``card_id`` rebuilt from this reader's own list (None: gone)."""
        raw = self._brief_run(reader, lambda view: replies_of(view))
        for line in (raw or {}).get("lines") or ():
            card = reply_card(line)
            if card is not None and card["id"] == card_id:
                return card
        return None

    def label_changes(self, reader: Reader, object_id: str) -> m.LabelChanges:
        raw = self._brief_run(reader, lambda view: label_changes_of(view, object_id))
        if raw is None:
            raise UserFacing("not_found", "Your alert list could not be read.", 404)
        return m.LabelChanges(**raw)

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
        excluded = it.excluded_by(br.env_people())
        principals = ",".join(sorted(getattr(reader.session, "principals", ()) or ()))
        raw = it.workload(rows, reader=owner_ids(principals),
                          grants=grants.load() if grants else {"teams": {}, "managers": {}},
                          owner_kinds=OWNER_KINDS, excluded=excluded)
        return m.Workload(**{k: v for k, v in raw.items() if k != "schema"})

    def assign_workload_grants(self, reader: Reader, body: m.WorkloadGrants) -> m.WorkloadGrants:
        """Only the owner-open reader (``ReadPolicy.unrestricted``) assigns teams and grants."""
        from operating import inbox_triage as it

        def owner(view: Any) -> bool:
            try:
                with view._request():
                    _gated, policy = view.access.reader_policy()
                return bool(getattr(policy, "unrestricted", False))
            except Exception:  # noqa: BLE001 - an undecidable reader is not the owner
                return False
        if not self._brief_run(reader, owner):
            raise UserFacing("forbidden", "Only the owner assigns workload grants.", 403)
        grants = self._workload_grants()
        if grants is None:
            raise UserFacing("not_found", "No data directory is configured.", 404)
        try:
            return m.WorkloadGrants(**grants.assign(body.model_dump()))
        except it.GrantsInvalid as bad:
            raise UserFacing("invalid_request", str(bad), 422)

    def _brief_run(self, reader: Reader, job: Any) -> Any:
        return self.reads.run(reader, job, timeout=BRIEF_TIMEOUT_SECONDS)  # type: ignore

    def meeting_brief(self, reader: Reader, object_type: str, object_id: str) -> m.Brief:
        if object_type not in br.MEETING_TYPES + br.PARTY_TYPES \
                or br.type_of(object_id) != object_type:
            raise UserFacing("not_found", "A brief is prepared for a calendar event, a "
                             "meeting, a customer or a supplier.", 404)
        try:
            raw = self._brief_run(reader, lambda view: meeting_brief_of(
                view, object_id, reader, getattr(self, "state_dir", None)))
        except br.BriefRefused:
            raw = None
        if raw is None:
            raise UserFacing("not_found", "That record is not one you can open.", 404)
        return brief_model(raw)

    def _weekly_archive(self) -> Any:
        data_dir = getattr(self, "data_dir", None)
        return br.WeeklyArchive(data_dir / WEEKLY_FILE) if data_dir is not None else None

    def weekly_review(self, reader: Reader, week: Optional[date]) -> m.Brief:
        archive = self._weekly_archive()
        try:
            raw = self._brief_run(reader, lambda view: weekly_review_of(
                view, week, archive=archive, reader=reader.key))
        except br.BriefRefused:
            raise UserFacing("invalid_request", "That week has not started yet.", 422)
        if raw is None:
            raise UserFacing("not_found", "The weekly review needs a signed-in reader.", 404)
        return brief_model(raw)

    def labour(self, reader: Reader) -> m.LabourPage:
        """Department and week labour hours (``operating.labour_hours``): only for a reader
        holding the hr or ops-management grant; read from the aggregates store alone."""
        return m.LabourPage(**labour_page_of(getattr(self, "state_dir", None), reader))

    def upcoming_meetings(self, reader: Reader) -> m.UpcomingMeetings:
        raw = self._brief_run(reader, upcoming_of) or []
        return m.UpcomingMeetings(meetings=[m.UpcomingMeeting(
            object=_ref(e["object_id"], e["label"]), start=e.get("start"),
            brief_url=brief_url(e["object_id"]), sources=_sources(e.get("sources")))
            for e in raw])
