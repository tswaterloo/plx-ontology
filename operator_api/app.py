"""The Workbench app API: FastAPI routes over one backend and one authenticator.

``create_app(backend, auth)`` serves the contract's endpoints (``operator_api.models``);
the backend is the real one over the console's object workspace
(``operator_api.backend.WorkspaceBackend``) or the fixture one (``operator_api.mock``).

Every route except ``GET /api/auth/config`` and ``POST /api/auth/dev-login`` resolves the
reader first (``PUBLIC_ROUTES``; a test walks every route so a new one cannot skip it).
Errors are ``{"error": {"code", "message"}}`` with a plain-language message: no stack
trace, exception text or raw upstream JSON reaches a person; an unexpected failure is
logged with an incident id the message names. Each request's route template, status and
milliseconds are logged (``operator_api.latency``), never its query or body.
"""

from __future__ import annotations

from datetime import date
import json
import logging
import secrets
import time
from typing import Any, Callable, Iterator, Literal, Optional
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, Path as PathParam, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import feedback as fb
from . import models as m
from .auth import SESSION_COOKIE, AuthRequired, DevAuth, Reader
from .errors import BUSY, UserFacing

log = logging.getLogger("operator_api")
latency_log = logging.getLogger("operator_api.latency")

PUBLIC_ROUTES = frozenset({("GET", "/api/auth/config"), ("POST", "/api/auth/dev-login"),
                           ("GET", "/api/health")})

MESSAGES = {
    "sign_in_required": "Please sign in with your PLX Microsoft account.",
    "no_workbench_access": "Your account is signed in but has no Workbench access yet. "
                           "Ask your manager or IT to grant it.",
    "insufficient_scope": "Your sign-in does not include Workbench access. Sign out and "
                          "sign in again.",
    "dev_ticket_required": "This demo sign-in needs the access link you were given.",
    "dev_ticket_not_accepted": "This demo sign-in does not take an access link.",
    "access_link_refused": "This access link has expired or is not valid. Ask for a new one.",
    "dev_login_not_served": "Demo sign-in is off. Sign in with Microsoft.",
    "not_found": "We couldn't find that, or you don't have access to it.",
    "invalid_request": "That request wasn't understood. Check it and try again.",
    "origin_refused": "This request came from a page the Workbench does not trust.",
    "not_served": "That part of the Workbench isn't switched on here yet.",
    "forbidden": "Only the owner and the admins can do that.",
    "internal": "The Workbench server failed on this request. Try again; if it keeps happening, "
                "quote incident {incident}.",
}


def problem(code: str, status: int, **values: Any) -> JSONResponse:
    message = MESSAGES.get(code, MESSAGES["invalid_request"]).format(**values)
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _incident(error: BaseException, where: str) -> str:
    incident = secrets.token_hex(4)
    log.error("incident %s in %s: %s", incident, where, type(error).__name__, exc_info=error)
    return incident


def sse(event: str, payload: Any) -> str:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def origin_allowed(origin: str, headers: Any, allowed_origins: frozenset[str]) -> bool:
    """A browser POST's ``Origin`` is the page's own origin: the host the browser
    addressed, or one named with ``--allowed-origin``.

    The host the browser addressed is ``X-Forwarded-Host`` when a proxy in front set one,
    else ``Host``. Both deployed fronts are on the app's own host and set it from the
    browser's ``Host``: Caddy (``/api/*`` to the API, Host kept) and the Next.js rewrite
    of ``next start``/``next dev`` (Host becomes the API's loopback address, the browser's
    goes to ``X-Forwarded-Host``). A cross-site page cannot forge it: a browser sends no
    custom header cross-origin without a CORS preflight, and the API grants none. A
    header listing several hosts is not trusted (fail closed to ``Host``).
    """
    if origin in allowed_origins:
        return True
    netloc = urlsplit(origin).netloc
    if not netloc:
        return False   # "null" and other opaque origins
    forwarded = (headers.get("x-forwarded-host") or "").strip()
    if forwarded and "," not in forwarded:
        return netloc.lower() == forwarded.lower()
    return netloc.lower() == (headers.get("host") or "").strip().lower()


def create_app(backend: Any, auth: Any, *, allowed_origins: frozenset[str] = frozenset(),
               auth_config: Optional[dict[str, Any]] = None,
               secure_cookie: bool = True) -> FastAPI:
    """``auth_config``: the public sign-in configuration (``AuthConfig`` fields beyond the
    authenticator's own mode and dev-login switch)."""
    app = FastAPI(title="PLX Workbench app API", version=m.API_VERSION,
                  description="Read-only Workbench API for the web app. Every "
                              "route but auth/config, auth/dev-login and health needs a "
                              "signed-in reader. Errors are {error:{code,message}}.",
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.backend, app.state.auth = backend, auth
    # Operator freshness targets (operating.ops_health): the page latency window this
    # process measures, and the checks over the served state dir (none for a fixture).
    from operating.ops_health import CachedHealth, LatencyWindow
    pages = LatencyWindow()
    state_dir = getattr(backend, "state_dir", None)
    ops = CachedHealth(state_dir, pages) if state_dir else None
    app.state.page_latency = pages
    feedback_store = getattr(backend, "feedback_store", None) \
        or fb.FeedbackStore.for_backend(backend)
    app.state.feedback = feedback_store

    # ---- errors ---------------------------------------------------------------------

    @app.exception_handler(AuthRequired)
    async def _auth(request: Request, error: AuthRequired) -> JSONResponse:
        response = problem(error.code, error.status)
        if error.status == 401 and auth.mode == "entra":
            response.headers["WWW-Authenticate"] = 'Bearer realm="plx-workbench"'
        return response

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, error: RequestValidationError) -> JSONResponse:
        return problem("invalid_request", 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "invalid_request"}.get(error.status_code,
                                                               "invalid_request")
        return problem(code, error.status_code)

    @app.exception_handler(UserFacing)
    async def _plain(request: Request, error: UserFacing) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=error.status, headers={"Cache-Control": "no-store"})

    @app.exception_handler(TimeoutError)
    async def _busy(request: Request, error: TimeoutError) -> JSONResponse:
        return JSONResponse({"error": {"code": BUSY.code, "message": BUSY.message}},
                            status_code=BUSY.status, headers={"Cache-Control": "no-store"})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, error: Exception) -> JSONResponse:
        return problem("internal", 500, incident=_incident(error, request.url.path))

    # ---- middleware -------------------------------------------------------------------

    @app.middleware("http")
    async def _guard_and_time(request: Request, call_next: Callable) -> Response:
        started = time.perf_counter()
        origin = request.headers.get("origin")
        if origin is not None and request.method not in ("GET", "HEAD", "OPTIONS"):
            if not origin_allowed(origin, request.headers, allowed_origins):
                return problem("origin_refused", 403)
        try:
            response = await call_next(request)
        except Exception as error:  # noqa: BLE001 - never a stack trace to a person
            response = problem("internal", 500, incident=_incident(error, request.url.path))
        route = request.scope.get("route")
        ms = round((time.perf_counter() - started) * 1000, 1)
        path = getattr(route, "path", "unmatched")
        latency_log.info(json.dumps({
            "method": request.method, "route": path, "status": response.status_code,
            "ms": ms}))
        if request.method == "GET" and path.startswith("/api/") and path not in (
                "/api/health", "/api/ask") and response.status_code < 500:
            pages.add(ms)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    def reader(request: Request) -> Reader:
        who = auth.authenticate(request.headers, request.cookies)
        if getattr(who.session, "warm_ahead", False):   # read-ahead serves nobody
            raise AuthRequired("sign_in_required")
        return who

    # ---- auth -----------------------------------------------------------------------

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        """Public liveness ("status" stays "ok" while the process serves) and the operator
        freshness targets (``ops``: each check's value, target and breach; ages and
        counts only, never a record)."""
        if ops is None:
            return {"status": "ok"}
        try:
            return {"status": "ok", "ops": ops.get()}
        except Exception as error:  # noqa: BLE001 - liveness never fails on a check
            log.warning("ops health failed: %s", type(error).__name__)
            return {"status": "ok", "ops": None}

    @app.get("/api/auth/config", response_model=m.AuthConfig, tags=["auth"])
    def auth_config_route() -> m.AuthConfig:
        """Public: how to sign in (identifiers only)."""
        base = {**(auth_config or {}), **auth.config()}
        base.setdefault("sign_in_available", True)
        return m.AuthConfig(**base)

    @app.post("/api/auth/dev-login", response_model=m.DevLoginResponse, tags=["auth"],
              responses={400: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}})
    def dev_login(response: Response,
                  body: m.DevLoginRequest = Body(default_factory=m.DevLoginRequest)
                  ) -> m.DevLoginResponse:
        """Dev and mock modes only: open a demo session (HttpOnly cookie)."""
        if not isinstance(auth, DevAuth):
            raise AuthRequired("dev_login_not_served", 404)
        token, who = auth.login(body.ticket)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                            secure=secure_cookie, path="/api",
                            max_age=24 * 60 * 60)   # the server session is the authority (8 h idle, 24 h cap)
        return m.DevLoginResponse(ok=True, me=backend.me(who))

    @app.post("/api/auth/logout", response_model=m.LogoutResponse, tags=["auth"])
    def logout(request: Request, response: Response,
               who: Reader = Depends(reader)) -> m.LogoutResponse:
        """Ends a dev session (Entra sign-out is the front end's MSAL logout)."""
        if isinstance(auth, DevAuth):
            auth.logout(request.cookies.get(SESSION_COOKIE))
            response.delete_cookie(SESSION_COOKIE, path="/api")
        forget = getattr(backend, "forget_reader", None)
        if forget is not None:
            forget(who)   # signed out: no longer read ahead for
        return m.LogoutResponse(ok=True)

    @app.get("/api/me", response_model=m.Me, tags=["identity"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def me(who: Reader = Depends(reader)) -> m.Me:
        return backend.me(who).model_copy(update={"feedback_admin": fb.is_admin(who)})

    # ---- ask --------------------------------------------------------------------------

    ask_doc = {
        200: {"description": "Server-sent events. Each event's `event:` is its name and "
                             "`data:` one JSON object: understanding (UnderstandingEvent), "
                             "status (StatusEvent: a plain-words progress step), sources "
                             "(SourcesEvent), verdict (VerdictEvent: the answer's first "
                             "line once the answer gate accepts it), token "
                             "(TokenEvent), answer (AnswerPayload, last), error (ErrorBody, "
                             "last).",
              "content": {"text/event-stream": {"schema": {
                  "type": "string", "x-sse-events": {
                      name: {"$ref": f"#/components/schemas/{model.__name__}"}
                      for name, model in m.ASK_EVENTS.items()}}}}},
        401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}}

    @app.post("/api/ask", tags=["ask"], responses=ask_doc, response_class=StreamingResponse)
    def ask(body: m.AskRequest, who: Reader = Depends(reader)) -> StreamingResponse:
        """Ask a question; the answer streams as server-sent events."""
        def stream() -> Iterator[str]:
            try:
                for event, payload in backend.ask(who, body):
                    if event not in m.ASK_EVENTS:
                        continue
                    yield sse(event, payload)
            except UserFacing as error:
                yield sse("error", m.ErrorBody(code=error.code, message=error.message))
            except TimeoutError:
                yield sse("error", m.ErrorBody(code=BUSY.code, message=BUSY.message))
            except (KeyError, LookupError):
                yield sse("error", m.ErrorBody(code="not_found",
                                               message=MESSAGES["not_found"]))
            except ValueError:
                yield sse("error", m.ErrorBody(code="invalid_request",
                                               message=MESSAGES["invalid_request"]))
            except Exception as error:  # noqa: BLE001 - plain words, never a trace
                incident = _incident(error, "/api/ask")
                yield sse("error", m.ErrorBody(
                    code="internal", message=MESSAGES["internal"].format(incident=incident)))
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    # ---- suggest, home, object, feedback ------------------------------------------------

    @app.get("/api/suggest", response_model=m.SuggestResponse, tags=["search"],
             responses={401: {"model": m.ErrorResponse}})
    def suggest(q: str = Query(..., min_length=1, max_length=100),
                limit: int = Query(8, ge=1, le=20),
                who: Reader = Depends(reader)) -> m.SuggestResponse:
        """Typo-tolerant autocomplete over the records this reader may see."""
        return backend.suggest(who, q, limit)

    @app.get("/api/home", response_model=m.Home, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def home(who: Reader = Depends(reader)) -> m.Home:
        return backend.home(who)

    @app.get("/api/late", response_model=m.LateList, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def late_list(offset: int = Query(0, ge=0, le=20000), limit: int = Query(25, ge=1, le=100),
                  who: Reader = Depends(reader)) -> m.LateList:
        """Every late purchase order you may see, a page at a time, longest late first
        (Home shows the first ones: "show all")."""
        return backend.late_list(who, offset=offset, limit=limit)

    @app.get("/api/briefing", response_model=m.Briefing, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def briefing(view: Literal["all", "since_visit"] = "all",
                 who: Reader = Depends(reader)) -> m.Briefing:
        """Your briefing: the events routed to you that you may see, ranked by impact; the
        top ones each with why, a suggested next action and how we know, every other one
        counted in its type's group; each check's state; and a preview of the daily
        digest (never sent). ``view=since_visit``: only what is new since your last visit."""
        return backend.briefing(who, since_visit=view == "since_visit")

    @app.get("/api/briefing/items", response_model=m.BriefingItems, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def briefing_items(event_type: m.EventType, offset: int = Query(0, ge=0, le=5000),
                       limit: int = Query(25, ge=1, le=100),
                       view: Literal["all", "since_visit"] = "all",
                       who: Reader = Depends(reader)) -> m.BriefingItems:
        """One event type's items of your briefing, a page at a time (a group's "+N more")."""
        return backend.briefing_items(who, event_type, offset=offset, limit=limit,
                                      since_visit=view == "since_visit")

    @app.get("/api/suggestions", response_model=m.Suggestions, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def suggestions(who: Reader = Depends(reader)) -> m.Suggestions:
        """What needs your attention and what to do: late or unconfirmed purchase orders
        and the customer orders they hold up, "ETA confirmed?", customer orders at risk and
        margin alerts, ranked by impact; each with how we know, confidence, what is missing,
        a draft message and the actions it offers (``/api/actions/*``: nothing is sent
        without your confirm)."""
        return backend.suggestions(who)

    @app.get("/api/approvals", response_model=m.ApprovalsQueue, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def approvals_queue(who: Reader = Depends(reader)) -> m.ApprovalsQueue:
        """Every pending approval on your alert list (mail asks with no answer from the
        approver, Mission Control approvals), by who it waits on and how long, each with a
        drafted nudge. Nothing is sent."""
        return backend.approvals_queue(who)

    @app.get("/api/alert-kinds", response_model=m.AlertKinds, tags=["briefing"],
             responses={401: {"model": m.ErrorResponse}})
    def alert_kinds_registry(who: Reader = Depends(reader)) -> m.AlertKinds:
        """The alert registry: every alert type with its measured precision, its kind,
        whether it is on by default and whether it reaches My work (Watching lists it)."""
        from operating import alert_kinds
        return m.AlertKinds(**alert_kinds.public())

    @app.get("/api/replies", response_model=m.ReplyTriage, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def reply_triage(who: Reader = Depends(reader)) -> m.ReplyTriage:
        """What needs your reply today: your own mail and one-to-one chats where someone
        asked you something you have not answered, heaviest first, each with a drafted
        reply. Only your own mailbox. Nothing is sent."""
        return backend.reply_triage(who)

    @app.get("/api/my-work", response_model=m.MyWork, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def my_work(who: Reader = Depends(reader)) -> m.MyWork:
        """The work the autopilots prepared for you from new mail, tasks, approvals, QC
        results, meetings and record changes, each with what happened, the prepared work,
        its evidence and one-click approve through the confirm flows; your digest and
        nudges. Only yours (and your granted reports'). Nothing is sent or written."""
        return backend.my_work(who)

    @app.post("/api/my-work/{item_id}/write", response_model=m.WriteIntentView,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse},
                                           422: {"model": m.ErrorResponse}})
    def my_work_write(item_id: str, who: Reader = Depends(reader)) -> m.WriteIntentView:
        """Your item's prepared entry as a write intent the autopilot prepared: preview and
        approve it in ``/api/writes*`` (dry run until writes are granted)."""
        return backend.my_work_write(who, item_id)

    @app.get("/api/workload", response_model=m.Workload, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def workload(who: Reader = Depends(reader)) -> m.Workload:
        """Open threads awaiting reply, open tasks and alerts per team; per person only
        your own and the teams the owner granted you. Counts only."""
        return backend.workload(who)

    @app.get("/api/owner/workload", response_model=m.OwnerWorkload, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def owner_workload(who: Reader = Depends(reader)) -> m.OwnerWorkload:
        """Per person: open threads awaiting reply, open tasks, alerts and closed alerts.
        Owner only (403 for every other reader); counts only, never message content."""
        return backend.owner_workload(who)

    @app.get("/api/owner/workload/{person}/{bucket}", response_model=m.OwnerWorkloadItems,
             tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                        404: {"model": m.ErrorResponse}})
    def owner_workload_items(person: str, bucket: str, who: Reader = Depends(reader)
                             ) -> m.OwnerWorkloadItems:
        """The items behind one count of one person (bucket awaiting_reply, tasks or alerts):
        subject, counterpart, age, who it waits on, a snippet and the record to open. Owner
        only (403 for every other reader); excluded people never appear; marked items you
        cannot open stay withheld."""
        return backend.owner_workload_items(who, person, bucket)

    @app.get("/api/rd-projects", response_model=m.RdBoard, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def rd_board(who: Reader = Depends(reader)) -> m.RdBoard:
        """R&D and sample projects per customer from the threads and records you may open:
        status, last activity, owner and who each waits on, with a drafted status update.
        Nothing is sent."""
        return backend.rd_board(who)

    @app.post("/api/workload/grants", response_model=m.WorkloadGrants, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def assign_workload_grants(body: m.WorkloadGrants,
                               who: Reader = Depends(reader)) -> m.WorkloadGrants:
        """The owner assigns each person's team and the teams each manager sees per
        person (kept in the Workbench's own data directory)."""
        return backend.assign_workload_grants(who, body)

    @app.get("/api/label-changes/{object_id:path}", response_model=m.LabelChanges,
             tags=["actions"], responses={401: {"model": m.ErrorResponse}})
    def label_changes(object_id: str, who: Reader = Depends(reader)) -> m.LabelChanges:
        """The item page's label and artwork section: changes requested, approved, done,
        stale or with no approval, from your own alert list."""
        return backend.label_changes(who, object_id)

    @app.get("/api/record-pack/{object_type}/{object_id:path}", response_model=m.RecordPack,
             tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                          404: {"model": m.ErrorResponse}})
    def record_pack(object_type: str, object_id: str,
                    who: Reader = Depends(reader)) -> m.RecordPack:
        """The documents a customer or supplier usually asks for about this record (invoice,
        packing slip, BOL, COA, confirmation, ...), found in captured attachments,
        SharePoint and originals around it as you may read them, and a drafted reply.
        Nothing is sent: the draft goes through ``/api/actions/*`` and your confirm."""
        return backend.record_pack(who, object_type, object_id)

    @app.get("/api/alerts/{alert_id}/playbook", response_model=m.AlertPlaybook,
             tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                          404: {"model": m.ErrorResponse}})
    def alert_playbook(alert_id: str = PathParam(min_length=1, max_length=200),
                       who: Reader = Depends(reader)) -> m.AlertPlaybook:
        """One alert's ready-to-use next steps, worded from its own cited sources as you
        read them: a drafted email to the supplier or customer, a Teams note to the record's
        owner, links to the record here and in its source system, the change to make in the
        source system yourself, and snooze / assign / mark handled. Nothing is sent or
        written: its actions go through ``/api/actions/*`` (card id = its ``id``) and your
        confirm."""
        return backend.alert_playbook(who, alert_id)

    @app.get("/api/brief/meetings", response_model=m.UpcomingMeetings, tags=["briefs"],
             responses={401: {"model": m.ErrorResponse}})
    def upcoming_meetings(who: Reader = Depends(reader)) -> m.UpcomingMeetings:
        """Calendar events in the next 14 days you may read, soonest first, each with the
        link to its meeting prep brief."""
        return backend.upcoming_meetings(who)

    @app.get("/api/labour", response_model=m.LabourPage, tags=["briefs"],
             responses={401: {"model": m.ErrorResponse}})
    def labour(who: Reader = Depends(reader)) -> m.LabourPage:
        """Labour hours from the department time sheets: department and week totals of
        hours, overtime hours and headcount-days, the overtime trend and the overtime-spike
        alert (30% over a department's 8-week median). Hours only, never pay; no per-person
        figure; groups of fewer than 3 people are not shown. Only a reader granted hr or
        ops-management sees any figure (state not_granted otherwise)."""
        return backend.labour(who)

    @app.get("/api/brief/weekly", response_model=m.Brief, tags=["briefs"],
             responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def weekly_review(week: Optional[date] = Query(None, description="Any day of the week "
                                                   "(Monday to Sunday); default: last week"),
                      who: Reader = Depends(reader)) -> m.Brief:
        """The weekly ops review: orders booked, shipped and invoiced, on-time shipping, late
        purchase orders, top alerts, cash position and margin (finance), quality holds, each
        weekly figure against the week before. Generated from the records you may read, no
        model call; every figure cites its sources and nothing hidden is counted."""
        return backend.weekly_review(who, week)

    @app.get("/api/meetings/follow-up/{meeting_id}", response_model=m.MeetingFollowUp,
             tags=["briefs"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse}})
    def meeting_follow_up(meeting_id: str, who: Reader = Depends(reader)) -> m.MeetingFollowUp:
        """A recorded meeting's follow-up: summary, decisions, action items with the work
        prepared for each (a draft to approve and send, a document pack, a write intent for
        the write desk, a proposed Planner task or an answer computed now) and their status.
        Only the meeting's organiser and attendees see it; an HR meeting needs the hr
        marking. Nothing is sent or written here."""
        if len(meeting_id) > 200:
            return problem("invalid_request", 422)
        return backend.meeting_follow_up(who, meeting_id)

    @app.get("/api/meetings/action-items", response_model=m.MyActionItems, tags=["briefs"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def my_action_items(person: Optional[str] = Query(None, max_length=200),
                        who: Reader = Depends(reader)) -> m.MyActionItems:
        """My action items from recorded meetings (``person``: another person's, shown only
        for meetings you organised), open and overdue first."""
        return backend.my_action_items(who, person)

    @app.get("/api/brief/{object_type}/{object_id:path}", response_model=m.Brief,
             tags=["briefs"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse}})
    def meeting_brief(object_type: str, object_id: str,
                      who: Reader = Depends(reader)) -> m.Brief:
        """The meeting prep brief for a calendar event or meeting (its customers and
        suppliers matched by the attendee links) or for a customer or supplier itself: open
        orders and purchase orders, late items and alerts, recent mail and Teams subjects
        and dates, payments and balance (finance), open promises, quality issues and the
        last meeting's notes when captured. Cited, bounded, no model call. Not found reads
        the same as a record you cannot see."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        return backend.meeting_brief(who, object_type, object_id)

    @app.post("/api/actions/preview", response_model=m.ActionPreview, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def action_preview(body: m.ActionRequest, who: Reader = Depends(reader)
                       ) -> m.ActionPreview:
        """One action on one of your suggestion cards, as it would be done: the exact final
        payload (to, cc, subject, body, the message it replies to), the Graph request, the
        cited facts it still states, what blocks it and what a live send lacks. Nothing is
        done."""
        return backend.action_preview(who, body)

    @app.post("/api/actions/confirm", response_model=m.ActionResult, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}})
    def action_confirm(body: m.ActionConfirm, who: Reader = Depends(reader)
                       ) -> m.ActionResult:
        """Do the action you previewed, as yourself, once per idempotency key; recorded in
        the append-only actions journal. A message goes from your own mailbox only when
        sending is enabled and permitted; otherwise it is recorded as a dry run that names
        what is missing."""
        return backend.action_confirm(who, body)

    @app.get("/api/actions/state", response_model=m.ActionStates, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def action_states(who: Reader = Depends(reader)) -> m.ActionStates:
        """Each of your cards' latest action by you (sent, dry run, failed, done, ...)."""
        return backend.action_states(who)

    @app.get("/api/actions/log", response_model=m.ActionsLog, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def actions_log(limit: int = Query(200, ge=1, le=1000),
                    who: Reader = Depends(reader)) -> m.ActionsLog:
        """Your confirmed actions, newest first, from the actions journal."""
        return backend.actions_log(who, limit)

    # ---- approve-to-write (operating.write_intents) ------------------------------------
    _wr = {401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
           404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
           422: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}}

    @app.post("/api/writes", response_model=m.WriteIntentView, tags=["writes"], responses=_wr)
    def write_prepare(body: m.WritePrepare, who: Reader = Depends(reader)) -> m.WriteIntentView:
        """Record an exact entry for a business system (a BC purchase order, sales order,
        purchase order line change, vendor or customer note) with its precondition, reason
        and citations, as you. The same entry again returns the same intent. Nothing is
        written."""
        return backend.write_prepare(who, body)

    @app.get("/api/writes", response_model=m.WriteIntentList, tags=["writes"], responses=_wr)
    def write_list(who: Reader = Depends(reader)) -> m.WriteIntentList:
        """The write entries you prepared or hold a write grant for, newest first."""
        return backend.write_list(who)

    @app.get("/api/writes/{intent_id}/preview", response_model=m.WritePreview,
             tags=["writes"], responses=_wr)
    def write_preview(intent_id: str, who: Reader = Depends(reader)) -> m.WritePreview:
        """The entry field by field against the record as read now (read-only), its
        warnings and refusals, the exact BC API v2.0 request and the undo note."""
        return backend.write_preview(who, intent_id)

    @app.post("/api/writes/{intent_id}/approve", response_model=m.WriteIntentView,
              tags=["writes"], responses=_wr)
    def write_approve(intent_id: str, body: m.WriteApprove, who: Reader = Depends(reader)
                      ) -> m.WriteIntentView:
        """Approve the entry you previewed: only with a write grant for its entity, never
        your own entry, cleared by the people exclusion, warnings acknowledged."""
        return backend.write_approve(who, intent_id, body)

    @app.post("/api/writes/{intent_id}/execute", response_model=m.WriteIntentView,
              tags=["writes"], responses=_wr)
    def write_execute(intent_id: str, body: m.WriteExecute, who: Reader = Depends(reader)
                      ) -> m.WriteIntentView:
        """Run an approved entry once per entry: today recorded as "would write" with the
        exact request; live writing is off."""
        return backend.write_execute(who, intent_id, body)

    # ---- watchlists and alert rules ----------------------------------------------------

    @app.get("/api/watch", response_model=m.Watchlist, tags=["watch"],
             responses={401: {"model": m.ErrorResponse}})
    def watchlist(who: Reader = Depends(reader)) -> m.Watchlist:
        """Your follows, your alert rules and your alerts (newest first), each alert shown
        only as far as your grant admits now. Kept in the Workbench, never in a source."""
        return backend.watchlist(who)

    @app.post("/api/watch/follow", response_model=m.WatchFollow, tags=["watch"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def watch_follow(body: m.WatchFollowRequest, who: Reader = Depends(reader)
                     ) -> m.WatchFollow:
        """Follow a record you can open: you are told when it or a record linked to it
        changes, and when a new record (a mail, a shipment, an invoice) is linked to it."""
        return backend.watch_follow(who, body)

    @app.delete("/api/watch/follow/{object_id:path}", response_model=m.WatchFollow,
                tags=["watch"], responses={401: {"model": m.ErrorResponse}})
    def watch_unfollow(object_id: str = PathParam(min_length=1, max_length=400),
                       who: Reader = Depends(reader)) -> m.WatchFollow:
        """Stop following a record."""
        return backend.watch_unfollow(who, object_id)

    @app.post("/api/watch/rules/compile", response_model=m.WatchRuleDraft, tags=["watch"],
              responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def watch_rule_compile(body: m.WatchRuleCompileRequest, who: Reader = Depends(reader)
                           ) -> m.WatchRuleDraft:
        """An alert rule in plain words, compiled into the restricted rule spec and shown
        back in plain words for you to confirm; nothing is kept. A rule the grammar cannot
        hold, or one over figures your markings do not cover, is refused with the reason."""
        return backend.watch_rule_compile(who, body)

    @app.post("/api/watch/rules", response_model=m.WatchRule, tags=["watch"],
              responses={401: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def watch_rule_create(body: m.WatchRuleRequest, who: Reader = Depends(reader)
                          ) -> m.WatchRule:
        """Keep the rule you were shown (its spec and digest), validated again."""
        return backend.watch_rule_create(who, body)

    @app.delete("/api/watch/rules/{rule_id}", response_model=m.WatchRuleDeleted,
                tags=["watch"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse}})
    def watch_rule_delete(rule_id: str = PathParam(min_length=1, max_length=40),
                          who: Reader = Depends(reader)) -> m.WatchRuleDeleted:
        """Delete one of your rules."""
        return backend.watch_rule_delete(who, rule_id)

    @app.post("/api/watch/alerts/read", response_model=m.WatchReadResult, tags=["watch"],
              responses={401: {"model": m.ErrorResponse}})
    def watch_read(body: m.WatchReadRequest, who: Reader = Depends(reader)
                   ) -> m.WatchReadResult:
        """Mark some of your alerts read."""
        return backend.watch_read(who, body)

    @app.post("/api/briefing/seen", response_model=m.BriefingSeen, tags=["home"],
              responses={401: {"model": m.ErrorResponse}})
    def briefing_seen(who: Reader = Depends(reader)) -> m.BriefingSeen:
        """Mark your briefing seen now: the next "since your last visit" starts here."""
        return backend.briefing_seen(who)

    @app.post("/api/briefing/watcher/{item_id}", response_model=m.WatcherItemState,
              tags=["home"], responses={400: {"model": m.ErrorResponse},
                                        401: {"model": m.ErrorResponse},
                                        404: {"model": m.ErrorResponse}})
    def watcher_item_state(item_id: str, body: m.WatcherItemStateRequest,
                           who: Reader = Depends(reader)) -> m.WatcherItemState:
        """Dismiss or snooze one item the AI watcher flagged in your briefing (``open``
        undoes it). Only your own briefing changes; nothing is sent."""
        return backend.watcher_item_state(who, item_id, body)

    @app.get("/api/object/{object_type}/{object_id:path}", response_model=m.ObjectPage,
             tags=["object"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse}})
    def object_page(object_type: str, object_id: str,
                    quick: bool = Query(False, description="A first read without the "
                                        "checks (complete=false), shown while the full "
                                        "page is read"),
                    who: Reader = Depends(reader)) -> Response:
        """One record's page. ``object_id``: the URL-encoded object id, or a business
        reference (e.g. a PO number) resolved within ``object_type``."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            page = (backend.object_page(who, object_type, object_id, quick=True) if quick
                    else backend.object_page(who, object_type, object_id))
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(page.model_dump(mode="json"))

    @app.get("/api/explore/{object_type}/{object_id:path}", response_model=m.Explore,
             tags=["object"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse}})
    def explore(object_type: str, object_id: str, who: Reader = Depends(reader)) -> Response:
        """The link explorer: a record's neighbours one hop out, grouped by kind of link,
        capped per kind, as this reader may see them. Walk on by asking for a neighbour.
        Not found reads the same as a record the reader cannot see."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            out = backend.explore(who, object_type, object_id)
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

    @app.get("/api/history/{object_type}/{object_id:path}", response_model=m.RecordHistory,
             tags=["object"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse},
                                         422: {"model": m.ErrorResponse}})
    def record_history(object_type: str, object_id: str,
                       cursor: Optional[str] = Query(None, min_length=1, max_length=512,
                                                     description="next_cursor of the page "
                                                     "before, for older changes"),
                       who: Reader = Depends(reader)) -> Response:
        """A record's History tab: every change to its values and links over time, newest
        first, each with when and which system wrote it, as this reader may read them (a
        value marked beyond them then or since is left out, and the page says some are).
        Not found reads the same as a record the reader cannot see."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            out = backend.record_history(who, object_type, object_id, cursor)
        except ValueError:
            return problem("invalid_request", 422)
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

    @app.get("/api/as-of/{object_type}/{object_id:path}", response_model=m.RecordAsOf,
             tags=["object"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse},
                                         422: {"model": m.ErrorResponse}})
    def record_as_of(object_type: str, object_id: str,
                     at: str = Query(..., min_length=10, max_length=40,
                                     pattern=r"^\d{4}-\d{2}-\d{2}([T ][0-9:.]+(Z|[+-]\d{2}:?\d{2}))?$",
                                     description="A date (its end) or an ISO time with zone"),
                     who: Reader = Depends(reader)) -> Response:
        """The record as the Workbench had recorded it at a time: its values and its links
        counted per kind, as this reader may read them. Not found reads the same as a
        record the reader cannot see, or one not yet recorded then."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            out = backend.record_as_of(who, object_type, object_id, at)
        except ValueError:
            return problem("invalid_request", 422)
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

    @app.get("/api/object-section/{section}/{object_type}/{object_id:path}",
             response_model=m.ObjectSection, tags=["object"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def object_section(section: Literal["late", "people", "landed_cost", "trail", "margin",
                                        "knows"],
                       object_type: str, object_id: str,
                       who: Reader = Depends(reader)) -> Response:
        """One slow section of a record's page (late check, people, landed cost, how we
        know), read after its quick first read and merged into it. Not found reads the
        same as a record the reader cannot see."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            out = backend.object_section(who, object_type, object_id, section)
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

    @app.get("/api/object-impact/{mode}/{object_type}/{object_id:path}",
             response_model=m.ImpactPanel, tags=["object"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def object_impact(mode: Literal["what_if", "why"], object_type: str, object_id: str,
                      change: Optional[Literal["delay", "out_of_stock", "customer_lost",
                                               "price_change"]] = None,
                      days: Optional[int] = Query(None, ge=1, le=365),
                      percent: Optional[str] = Query(None, pattern=r"^-?\d{1,4}(\.\d{1,2})?$"),
                      who: Reader = Depends(reader)) -> Response:
        """A record's Impact panel, read on demand: what a change (a delay of N days, out
        of stock, a customer lost, a price change) puts at risk downstream, or why an order
        is late or unprofitable. Not found reads the same as a record the reader cannot
        see; nothing is written."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        spec: Optional[dict[str, Any]] = None
        if mode == "what_if":
            kind = change or "delay"
            spec = {"kind": kind}
            if kind == "delay":
                spec["days"] = days or 7
            elif kind == "price_change":
                if percent is None:
                    return problem("invalid_request", 422)
                spec["percent"] = percent
        elif change is not None:
            return problem("invalid_request", 422)
        try:
            out = backend.object_impact(who, object_type, object_id, mode, spec)
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

    @app.get("/api/source", response_model=m.SourceView, tags=["object"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def source(handle: str = Query(..., min_length=3, max_length=2000),
               text_offset: int = Query(0, ge=0, le=10_000_000),
               who: Reader = Depends(reader)) -> Response:
        """A cited source as this reader may open it (a Source's open_url page reads it)."""
        try:
            view = backend.source(who, handle, text_offset)
        except (KeyError, LookupError, ValueError):
            return problem("not_found", 404)
        return JSONResponse(view.model_dump(mode="json"))

    @app.post("/api/feedback", response_model=m.FeedbackResponse, tags=["feedback"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def feedback(body: m.FeedbackRequest, who: Reader = Depends(reader)) -> Response:
        """Rate an answer you were given: up or down, what was wrong, the claims that were
        wrong and the right answer, kept as you saw the answer. The rating and its record
        are one write: when either fails, neither is kept."""
        try:
            entry, out = fb.record_answer(feedback_store, backend, who, body,
                                          then=lambda: backend.feedback(who, body))
        except (KeyError, LookupError):
            return problem("not_found", 404)
        out = out.model_copy(update={"feedback_id": entry["id"],
                                     "stored_in": [*out.stored_in, "feedback_store"]})
        return JSONResponse(out.model_dump(mode="json"))

    @app.post("/api/feedback/item", response_model=m.FeedbackResponse, tags=["feedback"],
              responses={401: {"model": m.ErrorResponse}})
    def feedback_item(body: m.ItemFeedbackRequest, who: Reader = Depends(reader)) -> Response:
        """A briefing or alert item: useful, not useful or wrong. Not useful or wrong
        suppresses it in your briefing (the AI watcher's and the proactive engine's)."""
        entry = fb.record_item(feedback_store, backend, who, body)
        return JSONResponse(m.FeedbackResponse(ok=True, feedback_id=entry["id"],
                                               stored_in=["feedback_store"]).model_dump())

    @app.post("/api/feedback/note", response_model=m.FeedbackResponse, tags=["feedback"],
              responses={401: {"model": m.ErrorResponse}})
    def feedback_note(body: m.NoteFeedbackRequest, who: Reader = Depends(reader)) -> Response:
        """A note on a page, a record, a list, the app itself or anything else: general
        feedback needs only the note; a page or record is attached when sent."""
        entry = fb.record_note(feedback_store, backend, who, body)
        return JSONResponse(m.FeedbackResponse(ok=True, feedback_id=entry["id"],
                                               stored_in=["feedback_store"]).model_dump())

    @app.get("/api/feedback", response_model=m.FeedbackList, tags=["feedback"],
             responses={401: {"model": m.ErrorResponse}})
    def feedback_list(kind: Optional[m.FeedbackKind] = None,
                      page: Optional[str] = Query(None, max_length=300),
                      status: Optional[m.FeedbackStatus] = None,
                      order: Literal["newest", "oldest"] = "newest",
                      offset: int = Query(0, ge=0, le=100_000),
                      limit: int = Query(50, ge=1, le=200),
                      who: Reader = Depends(reader)) -> Response:
        """Your feedback, newest first; the owner and the admins see every reader's (an
        admin who is not the owner reads another reader's answer as their own markings
        allow)."""
        admin = fb.is_admin(who)
        rows, total, counts = feedback_store.list(
            reader=None if admin else who.key, kind=kind, page=page, status=status,
            oldest_first=order == "oldest", limit=limit, offset=offset)
        viewer = fb.Viewer(backend, who, rows)
        out = m.FeedbackList(all_readers=admin, total=total, offset=offset, counts=counts,
                             entries=[_feedback_entry(viewer.entry(r), who, admin)
                                      for r in rows])
        return JSONResponse(out.model_dump(mode="json"))

    @app.post("/api/feedback/{feedback_id}/status", response_model=m.FeedbackEntry,
              tags=["feedback"], responses={401: {"model": m.ErrorResponse},
                                            403: {"model": m.ErrorResponse},
                                            404: {"model": m.ErrorResponse}})
    def feedback_status(feedback_id: str, body: m.FeedbackStatusRequest,
                        who: Reader = Depends(reader)) -> Response:
        """Triage (the owner and the admins): new, accepted, fixed or won't fix."""
        if not fb.is_admin(who):
            return problem("forbidden", 403)
        try:
            row = feedback_store.set_status(feedback_id, body.status, who.display_name)
        except KeyError:
            return problem("not_found", 404)
        shown = fb.Viewer(backend, who, [row]).entry(row)
        return JSONResponse(_feedback_entry(shown, who, True).model_dump(mode="json"))

    def _feedback_entry(row: dict, who: Reader, admin: bool) -> m.FeedbackEntry:
        ctx = row.get("context") or {}
        return m.FeedbackEntry(
            id=row["id"], at=row["at"], kind=row["kind"], status=row["status"],
            mine=row["reader"] == who.key,
            reader_name=row.get("reader_name") if admin or row["reader"] == who.key else None,
            **{k: row.get(k) for k in ("rating", "verdict", "note", "expected", "page",
                                       "object_type", "object_id", "label", "item_id",
                                       "question", "answer")},
            claims=row.get("claims") or [], flagged=row.get("flagged") or [],
            withheld=row.get("withheld") or [],
            served_version=ctx.get("served_version"), app_version=ctx.get("app_version"))

    return app


def openapi_document(app: FastAPI) -> dict[str, Any]:
    """The OpenAPI document with every SSE event model in ``components.schemas``."""
    from fastapi.openapi.utils import get_openapi
    from pydantic.json_schema import models_json_schema

    doc = get_openapi(title=app.title, version=app.version, description=app.description,
                      routes=app.routes)
    _, defs = models_json_schema([(model, "serialization") for model in
                                  [*m.ASK_EVENTS.values(), m.ErrorResponse]],
                                 ref_template="#/components/schemas/{model}")
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    for name, schema in (defs.get("$defs") or {}).items():
        schemas.setdefault(name, schema)
    return doc
