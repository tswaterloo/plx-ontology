"""Assist's "do a whole job now" routes (ASSIST10; ``operating/assist_job.py`` is the worker).

* ``GET  /api/assist/jobs``: the reader's own jobs, newest first, each folded from ``assist.job_log``
  (progress, the plan, every prepared piece with the reads behind it). Nobody reads another person's job.
* ``POST /api/assist/jobs``: hand Assist a job: one ``requested`` row; the worker takes it from there.
* ``POST /api/assist/jobs/{id}/stop``: a ``stop`` row; the worker ends its sessions and logs ``stopped``.
* ``POST /api/assist/jobs/{id}/pieces/{n}/decision``: approved, dismissed or edited (the person's own
  words, or a message's body for a change). Recorded only: approving a piece sends nothing.
* ``POST /api/assist/jobs/{id}/pieces/{n}/changes/{i}/prepare``: one proposed change prepared through the
  prepared-writes desk as the reader (the record read from its system, before and after), then confirmed
  in two steps on ``/api/prepared/{id}``: the one confirm path, the one live-send rule (Vince only),
  card masking and money class. A reopen answers the same card.

The API role appends only what the person does (``requested``, ``stop``, ``decided``, ``prepared``;
a trigger enforces it); it never sends or writes to a business system from here."""
from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Mapping

from fastapi import Depends

from . import models as m
from .auth import Reader
from .errors import UserFacing

LOG = logging.getLogger(__name__)
_ERR = {401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
        422: {"model": m.ErrorResponse}}

_URI_LABELS: dict[str, str] = {
    "teams-chat": "Teams chat",
    "teams-channel": "Teams channel",
    "graph-mail": "email",
    "mail": "email",
    "sharepoint-list": "SharePoint list",
    "sharepoint": "SharePoint file",
    "calendar-event": "calendar event",
    "planner-task": "Planner task",
    "meeting-transcript": "meeting transcript",
    "mc": "Mission Control record",
    "bc": "Business Central record",
    "api": "Portal record",
    "original": "record",
    "record": "record",
    "po": "purchase order",
}
_SCHEME_ALT = "|".join(sorted(_URI_LABELS, key=len, reverse=True))
_PAREN_INTERNAL_URI = re.compile(rf"\s*\(\s*(?:{_SCHEME_ALT})://[^\s)]+\s*\)", re.IGNORECASE)
_BARE_INTERNAL_URI = re.compile(rf"\b({_SCHEME_ALT})://[^\s),;\]]+", re.IGNORECASE)
_EXCEPTION_PAREN = re.compile(r"\s*\([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\s*:[^)]*\)")
_RAW_EXCEPTION = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\s*:")
_SNAKE_CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_WHY_LABELS: dict[str, str] = {
    "excluded_in_thread": "access rules",
    "plan_screened_redone_from_reads": "access rules",
    "not_cleared": "access rules",
    "pay_or_hr": "restricted pay or HR content",
    "freightcom": "restricted content",
    "outbound_check_unavailable": "outbound verification was unavailable",
    "no_check": "outbound verification was unavailable",
    "withheld": "access rules",
}
_CHECK_DROPPED_MESSAGE = (
    "Could not prepare a draft: there wasn't enough verified detail in the records or thread "
    "to back it up. Handle this one directly, or try again with more context."
)
_PIECE_CRASHED_MESSAGE = (
    "Something went wrong while preparing this piece. Handle this one directly, or try again."
)
_JOB_CRASHED_MESSAGE = (
    "Something went wrong while running this job. Nothing was sent or changed; try again."
)
_RANKING_FALLBACK_MESSAGE = (
    "Past cases were ordered by most recent rather than similarity; all past cases remain searchable."
)


def strip_internal_uris(text: Any) -> str:
    """Remove or humanize raw internal URI handles (teams-chat://, mail://, record://, ...) and
    em dashes in user-facing Assist job text while preserving readability and spacing."""
    s = str(text or "")
    if "\u2014" in s:
        s = re.sub(r"\s*\u2014\s*", " - ", s)
    if "://" in s:
        s = _PAREN_INTERNAL_URI.sub("", s)
        s = _BARE_INTERNAL_URI.sub(lambda match: _URI_LABELS.get(match.group(1).lower(), "record"), s)
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = re.sub(r" +([.,;:!?])", r"\1", s)
    return s.strip()


def clean_fallback(text: Any) -> str:
    """Turn stored or live Assist job fallback notes into calm plain English without Python
    exception names or internal error codes."""
    s = str(text or "").strip()
    if not s:
        return ""
    if "Past cases ranked by newest" in s or "no_gpu_host" in s:
        return _RANKING_FALLBACK_MESSAGE
    s = _EXCEPTION_PAREN.sub("", s)
    if _RAW_EXCEPTION.search(s):
        return _RANKING_FALLBACK_MESSAGE
    return strip_internal_uris(s)


def humanize_why(why: Any, *, default: str = "access rules") -> str:
    """Map internal snake_case reason codes in unread_reads / held_back to calm plain English."""
    raw = str(why or "").strip()
    if not raw:
        return default
    low = raw.lower()
    if low in _WHY_LABELS:
        return _WHY_LABELS[low]
    if _RAW_EXCEPTION.search(raw):
        return default
    if _SNAKE_CODE.match(raw):
        return default if "screen" in low or "exclud" in low else raw.replace("_", " ")
    return strip_internal_uris(raw)


def _clean_unread_reads(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, Mapping):
            copy_item = dict(item)
            copy_item["why"] = humanize_why(item.get("why"), default="access rules")
            out.append(copy_item)
    return out


def _clean_held_back(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, Mapping):
            copy_item = dict(item)
            if "why" in copy_item:
                copy_item["why"] = humanize_why(copy_item.get("why"), default="outbound rules")
            out.append(copy_item)
    return out


def _clean_message(reason: Any, message: Any, *, piece: bool = False) -> str:
    why = str(reason or "").strip()
    msg = str(message or "").strip()
    if why == "check_dropped" or "nothing in it the records" in msg:
        return _CHECK_DROPPED_MESSAGE
    if why == "piece_crashed" or (piece and _RAW_EXCEPTION.search(msg)):
        return _PIECE_CRASHED_MESSAGE
    if why == "job_crashed" or (not piece and _RAW_EXCEPTION.search(msg)):
        return _JOB_CRASHED_MESSAGE
    return strip_internal_uris(msg)


def _clean_ask(ask: Any) -> Any:
    if not isinstance(ask, Mapping):
        return ask
    out = dict(ask)
    if "question" in out:
        out["question"] = strip_internal_uris(out["question"])
    if isinstance(out.get("options"), list):
        cleaned_opts = []
        for opt in out["options"]:
            if isinstance(opt, Mapping):
                o = dict(opt)
                if "label" in o:
                    o["label"] = strip_internal_uris(o["label"])
                if "reply" in o and isinstance(o["reply"], str):
                    o["reply"] = strip_internal_uris(o["reply"])
                cleaned_opts.append(o)
            else:
                cleaned_opts.append(opt)
        out["options"] = cleaned_opts
    return out


def _clean_todos(todos: Any) -> list[Any]:
    out = []
    for todo in todos or ():
        if isinstance(todo, Mapping):
            t = dict(todo)
            for field in ("what", "record"):
                if field in t and isinstance(t[field], str):
                    t[field] = strip_internal_uris(t[field])
            out.append(t)
        else:
            out.append(todo)
    return out


def _pool(backend: Any) -> Any:
    return getattr(getattr(backend, "memory", None), "pool", None)


def _person(backend: Any, reader: Reader) -> str:
    ids = sorted(set(backend._apa_me(reader)) - {"unknown"})
    if not ids:
        raise UserFacing("not_found", "Assist does not know who you are here.", 404)
    return ids[0]


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, datetime) else str(v or "")


def _is_missing_table(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "42P01" or "does not exist" in str(exc).lower()


def shown(job: Mapping[str, Any]) -> m.AssistJob:
    """A folded job as the API's model. The person's edits are applied to the proposals they review, so the
    page shows what a prepare would use."""
    from operating import autopilot_jobs as ajobs
    pieces = []
    for p in job["pieces"]:
        props = copy.deepcopy(p.get("proposals") or [])
        for idx, fields in (p.get("edits") or {}).items():
            if idx.isdigit() and int(idx) < len(props):
                change = props[int(idx)].setdefault("arguments", {}).setdefault("change", {})
                if isinstance(change, dict):
                    change.update(fields)
        over = max(0, len(props) - ajobs.MOST)
        pieces.append(m.AssistJobPiece(
            n=p["n"], title=strip_internal_uris(p.get("title", "")), instruction=p.get("instruction", ""),
            status=p.get("status", "failed"), why=p.get("why", ""),
            message=_clean_message(p.get("why", ""), p.get("message", ""), piece=True),
            summary=strip_internal_uris(p.get("summary", "")), reply=p.get("reply", ""),
            ask=_clean_ask(p.get("ask")), proposals=props, todos=_clean_todos(p.get("todos")),
            note=strip_internal_uris(p.get("note", "")), warning=strip_internal_uris(p.get("warning", "")),
            nothing=strip_internal_uris(p.get("nothing", "")), held_back=_clean_held_back(p.get("held_back")),
            reads=[str(x) for x in p.get("reads") or []], tool_reads=p.get("tool_reads") or [],
            reads_traced=bool(p.get("reads_traced", True)),
            records_unread=[strip_internal_uris(x) for x in p.get("records_unread") or []],
            unread_reads=_clean_unread_reads(p.get("unread_reads")), planned_records=p.get("planned_records") or [],
            decision=p.get("decision", ""), edited_text=p.get("edited_text", ""),
            prepared_ids={str(k): v for k, v in (p.get("prepared_ids") or {}).items()},
            changes_over_limit=over, usd=p.get("usd")))
    fallbacks = [fb for fb in (clean_fallback(f) for f in job.get("fallbacks") or ()) if fb]
    checklist = [m.AssistJobChecklistItem(text=strip_internal_uris(c.get("text", "")), piece=c.get("piece"))
                 for c in job["checklist"]]
    return m.AssistJob(
        job_id=job["job_id"], text=job["text"], status=job["status"], reason=job["reason"],
        message=_clean_message(job.get("reason", ""), job.get("message", ""), piece=False),
        progress=job["progress"], requested_at=_iso(job["requested_at"]),
        summary=strip_internal_uris(job["summary"]), checklist=checklist,
        found=strip_internal_uris(job["found"]), not_found=strip_internal_uris(job["not_found"]),
        nothing=strip_internal_uris(job["nothing"]), planned=job["planned"], unread=job["unread"],
        fallbacks=fallbacks, unread_reads=_clean_unread_reads(job.get("unread_reads")),
        plan_reads=[str(x) for x in job["plan_reads"]], pieces=pieces, usd=job["usd"])


def _job(backend: Any, reader: Reader, job_id: str) -> dict[str, Any]:
    from operating import assist_job as aj
    pool = _pool(backend)
    if pool is None:
        raise UserFacing("action_refused", "Jobs are not available here.", 409)
    try:
        with pool.connection() as pg:
            rows = aj.rows_of(pg, job_id)
    except Exception as exc:  # noqa: BLE001 - missing 0082 migration surfaced cleanly
        if _is_missing_table(exc):
            LOG.info("assist jobs: assist.job_log missing (%s)", exc)
            raise UserFacing("action_refused", "Jobs are not ready on this server yet (migration 0082 pending).", 409) from None
        raise
    person = _person(backend, reader)
    if not rows or rows[0]["person"] != person:      # another person's job is not found
        raise UserFacing("not_found", "That job is not on your list.", 404)
    return aj.fold(rows)


def _append(backend: Any, job_id: str, person: str, what: str, detail: Mapping[str, Any]) -> None:
    from operating import assist_job as aj
    if what not in aj.PERSON_WHATS:
        raise ValueError(what)
    try:
        with _pool(backend).connection() as pg:
            pg.execute(f"INSERT INTO {aj.TABLE} (job_id, person, what, detail) VALUES (%s, %s, %s, %s)",
                       (job_id, person, what, json.dumps(dict(detail), default=str)))
            pg.commit()        # the pool's hand-back rolls an open transaction back
    except Exception as exc:  # noqa: BLE001 - missing 0082 migration surfaced cleanly
        if _is_missing_table(exc):
            LOG.info("assist jobs: assist.job_log missing (%s)", exc)
            raise UserFacing("action_refused", "Jobs are not ready on this server yet (migration 0082 pending).", 409) from None
        raise


def install(app: Any, backend: Any, reader: Callable[..., Reader]) -> None:

    @app.get("/api/assist/jobs", response_model=m.AssistJobs, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def jobs_list(who: Reader = Depends(reader)) -> m.AssistJobs:
        """Your jobs, newest first, with their pieces."""
        from operating import assist_job as aj
        pool = _pool(backend)
        if pool is None:
            return m.AssistJobs(status="not_run", reason="the Assist store is not connected here")
        person = _person(backend, who)
        try:
            with pool.connection() as pg:
                folded = aj.jobs_of(pg, [person], recent=None)
        except Exception as exc:  # noqa: BLE001 - missing 0082 migration returns not_run instead of 500
            if _is_missing_table(exc):
                LOG.info("assist jobs: assist.job_log missing (%s)", exc)
                return m.AssistJobs(status="not_run",
                                    reason="Jobs are not ready on this server yet (migration 0082 pending).")
            raise
        return m.AssistJobs(jobs=[shown(j) for j in folded])

    @app.post("/api/assist/jobs", response_model=m.AssistJob, tags=["actions"], responses=_ERR)
    def job_request(body: m.AssistJobRequest, who: Reader = Depends(reader)) -> m.AssistJob:
        """Hand Assist a whole job. It plans, reads and prepares every piece; nothing is sent or written."""
        from operating import assist_job as aj
        from operating.card_text import mask_served
        pool = _pool(backend)
        if pool is None:
            raise UserFacing("action_refused", "Jobs are not available here.", 409)
        person = _person(backend, who)
        text = " ".join(body.text.split())
        if not text:
            raise UserFacing("invalid_request", "Say what the job is.", 422)
        job_id = aj.new_job_id()
        _append(backend, job_id, person, "requested", {"text": mask_served(text)})
        return shown(_job(backend, who, job_id))

    @app.post("/api/assist/jobs/{job_id}/stop", response_model=m.AssistJob, tags=["actions"], responses=_ERR)
    def job_stop(job_id: str, who: Reader = Depends(reader)) -> m.AssistJob:
        """Stop a job: no more sessions start; the pieces already prepared stay."""
        job = _job(backend, who, job_id)
        if job["status"] in ("stopped", "finished", "failed"):
            return shown(job)
        if not job["stop_asked"]:
            _append(backend, job_id, job["person"], "stop", {})
        return shown(_job(backend, who, job_id))

    def piece_of(job: Mapping[str, Any], n: int) -> Mapping[str, Any]:
        found = next((p for p in job["pieces"] if p["n"] == n), None)
        if found is None:
            raise UserFacing("not_found", "That piece is not on this job.", 404)
        return found

    @app.post("/api/assist/jobs/{job_id}/pieces/{n}/decision", response_model=m.AssistJob, tags=["actions"],
              responses=_ERR)
    def piece_decide(job_id: str, n: int, body: m.AssistJobDecision, who: Reader = Depends(reader)) -> m.AssistJob:
        """Approve, dismiss or edit one piece. Recorded only: nothing is sent or written."""
        job = _job(backend, who, job_id)
        piece = piece_of(job, n)
        if body.decision == "edited" and not (body.text.strip() or body.edits):
            raise UserFacing("invalid_request", "Say what to change.", 422)
        for idx, fields in body.edits.items():
            props = piece.get("proposals") or []
            if not idx.isdigit() or int(idx) >= len(props):
                raise UserFacing("invalid_request", "That change is not on this piece.", 422)
            if set(fields) - {"body", "subject"}:
                raise UserFacing("invalid_request", "Only a message's subject and body can be edited here.", 422)
        _append(backend, job_id, job["person"], "decided",
                {"piece": n, "decision": body.decision, "text": body.text, "edits": body.edits})
        return shown(_job(backend, who, job_id))

    @app.post("/api/assist/jobs/{job_id}/pieces/{n}/changes/{index}/prepare", response_model=m.PreparedWrite,
              tags=["actions"], responses=_ERR)
    def piece_prepare(job_id: str, n: int, index: int, who: Reader = Depends(reader)) -> m.PreparedWrite:
        """Review one proposed change of a piece: prepared through the desk as you (nothing written); confirm it
        in two steps on ``/api/prepared/{id}``. A change reviewed before answers the card it became."""
        from operating import autopilot_jobs as ajobs
        from operating import prepared_writes as pw
        job = _job(backend, who, job_id)
        piece = piece_of(job, n)
        raw_proposals = list(piece.get("proposals") or ())
        proposals = ajobs.kept(raw_proposals)
        if index >= ajobs.MOST and index < len(raw_proposals):
            raise UserFacing(
                "action_refused",
                f"The desk prepares at most {ajobs.MOST} changes per piece; this piece holds {len(raw_proposals)}.",
                409,
            )
        if not 0 <= index < len(proposals):
            raise UserFacing("not_found", "That change is not on this piece.", 404)
        made = (piece.get("prepared_ids") or {}).get(index)
        desk, person = backend._pw_desk(who), backend._pw_person(who)
        if made:
            try:
                card = desk.card(person, made)
            except pw.Refused:
                card = None
            if backend._apa_job_stage(card) != "again":
                return backend._pw_card(card)
        proposal = copy.deepcopy(proposals[index])
        for field, value in ((piece.get("edits") or {}).get(str(index)) or {}).items():
            proposal["arguments"].setdefault("change", {})[field] = value
        pid = backend.prepare_proposal(who, proposal, origin="assist_job", citations=[
            r for r in (piece.get("reads") or []) if isinstance(r, str)])
        _append(backend, job_id, job["person"], "prepared", {"piece": n, "index": index, "prepared_id": pid})
        return backend._pw_card(backend._pw_do(lambda: desk.card(person, pid)))
