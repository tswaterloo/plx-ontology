"""The app's briefs (``operating.briefs``): a meeting prep brief per calendar event, meeting,
customer or supplier, the upcoming meetings to prepare, and the weekly review, each read
as the signed-in reader (their own tool calls, their own trimmed alert list, their
markings) on the read worker. No model call; an optional summary is a question for the
answer path (``/api/ask``) with its claim checks, never written here."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional
from urllib.parse import quote

from operating import briefs as br

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
                   basis=raw.get("basis") or "", summary_question=question)


def _held(view: Any) -> frozenset[str] | None:
    grant = getattr(getattr(view, "access", None), "grant", None)
    if grant is None:
        return None
    with view._request():
        return grant()[1]


def _alerts(view: Any) -> Mapping[str, Any]:
    from operating import briefing as bf
    return view.gap_inbox(kinds=list(bf.EVENT_KINDS), limit=bf.MAX_READ)


def meeting_brief_of(view: Any, object_id: str) -> Optional[dict[str, Any]]:
    held = _held(view)
    if held is None:
        return None
    alerts = _alerts(view)
    with view._request():
        return br.meeting_brief(view._call, held, alerts, object_id)


def weekly_review_of(view: Any, week: Optional[date]) -> Optional[dict[str, Any]]:
    held = _held(view)
    if held is None:
        return None
    alerts = _alerts(view)
    with view._request():
        return br.weekly_review(view._call, held, alerts, week=week)


def upcoming_of(view: Any) -> Optional[list[dict[str, Any]]]:
    held = _held(view)
    if held is None:
        return None
    with view._request():
        return br.upcoming_meetings(view._call, held)


class BriefsMixin:
    """The briefs routes' backend (``WorkspaceBackend`` mixes it in)."""

    def _brief_run(self, reader: Reader, job: Any) -> Any:
        return self.reads.run(reader, job, timeout=BRIEF_TIMEOUT_SECONDS)  # type: ignore

    def meeting_brief(self, reader: Reader, object_type: str, object_id: str) -> m.Brief:
        if object_type not in br.MEETING_TYPES + br.PARTY_TYPES \
                or br.type_of(object_id) != object_type:
            raise UserFacing("not_found", "A brief is prepared for a calendar event, a "
                             "meeting, a customer or a supplier.", 404)
        try:
            raw = self._brief_run(reader, lambda view: meeting_brief_of(view, object_id))
        except br.BriefRefused:
            raw = None
        if raw is None:
            raise UserFacing("not_found", "That record is not one you can open.", 404)
        return brief_model(raw)

    def weekly_review(self, reader: Reader, week: Optional[date]) -> m.Brief:
        try:
            raw = self._brief_run(reader, lambda view: weekly_review_of(view, week))
        except br.BriefRefused:
            raise UserFacing("invalid_request", "That week has not started yet.", 422)
        if raw is None:
            raise UserFacing("not_found", "The weekly review needs a signed-in reader.", 404)
        return brief_model(raw)

    def upcoming_meetings(self, reader: Reader) -> m.UpcomingMeetings:
        raw = self._brief_run(reader, upcoming_of) or []
        return m.UpcomingMeetings(meetings=[m.UpcomingMeeting(
            object=_ref(e["object_id"], e["label"]), start=e.get("start"),
            brief_url=brief_url(e["object_id"]), sources=_sources(e.get("sources")))
            for e in raw])
