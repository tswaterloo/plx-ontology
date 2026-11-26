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

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
import asyncio
import contextvars
from datetime import date, datetime, timezone
import itertools
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterator, Literal, Mapping, Optional
from urllib.parse import quote, urlsplit

from fastapi import Body, Depends, FastAPI, Path as PathParam, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse as _JSONResponse, PlainTextResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import feedback as fb
from . import view_as as va
from .door_value import DOOR_HEADER, SESSION_HEADER, SESSION_REFUSED, door_value
from .events import EventCenter

EVENTS_POLL_SECONDS = 5.0
from .routes import page as page_routes
from . import ask_runs as ar
from . import work_log as wl
from . import memory as mem
from .ask_drafts import cited_handles
from .stream_encoding import stream_encoding
from operating.card_text import mask_served, mask_served_in
from . import models as m
from .briefing_view import HOME_GROUP_PAGE_ROWS, home_group_pages
from .auth import SESSION_COOKIE, AuthRequired, DevAuth, Reader
from .errors import BUSY, RecordNotFound, UserFacing, source_refused

log = logging.getLogger("operator_api")
latency_log = logging.getLogger("operator_api.latency")

# The only routes the Ask canary's service session is served (session_identity.SERVICE_CANARY)
CANARY_ROUTES = frozenset({("GET", "/api/me"), ("POST", "/api/ask")})
# how far a list may be paged (late list, its supplier and search pages): every row, not 20,000
LIST_OFFSET_MOST = 10_000_000

# The page measure's service session (session_identity.SERVICE_MEASURE): the screens' reads,
# GET (a path exactly one of MEASURE_ROUTES or under one of MEASURE_PREFIXES) and the
# MEASURE_POSTS; never a write, an action, an upload or the view-as list
MEASURE_ROUTES = frozenset({
    "/api/me", "/api/home", "/api/home/bundle", "/api/home/needs", "/api/sidebar", "/api/briefing", "/api/briefing/items",
    "/api/briefing/pages", "/api/briefing/unjudged", "/api/suggestions", "/api/insights",
    "/api/insight-news", "/api/alert-kinds", "/api/late", "/api/data-health",
    "/api/data-health/export", "/api/labour",
    "/api/my-work", "/api/suggest",
    "/api/biz/page", "/api/records/list", "/api/records/views", "/api/brief/meetings", "/api/brief/weekly", "/api/team", "/api/workload",
    "/api/workload/grants", "/api/approvals", "/api/replies", "/api/autopilot",
    "/api/autopilot/catalog", "/api/autopilot/schedules", "/api/autopilot/drafts",
    "/api/trust", "/api/ops/status", "/api/watch", "/api/actions/state", "/api/prepared",
    "/api/writes", "/api/meetings/action-items", "/api/templates", "/api/templates/learned",
    # the screens' own reads the measure was refused (11-23, owner: all 18 screens valid): the
    # reader's chats list, feeds, Teams connection state, recurring instructions, action log,
    # memory (and the people it names), the answer bank and the work log; GET only, read as
    # the owner the measure's ticket names, nothing remembered or written for the session
    "/api/ask/chats", "/api/feeds", "/api/teams/connection", "/api/instructions/recurring",
    "/api/events",
    "/api/actions/log", "/api/memory", "/api/memory/people", "/api/templates/answer-bank",
    "/api/work-log",
    # a source and the records read out of it (business rule 11-23: every tool gets everything,
    # the measure session reads as the owner): GET only; no file download or upload route
    "/api/source", "/api/source/records", "/api/time-saved",
    "/api/sharepoint/folders",
    # every screen as the owner sees it (owner 11-23): the feedback list, the autopilot's
    # audit; the work log's threads and days are under MEASURE_PREFIXES
    "/api/feedback", "/api/autopilot/audit"})
# what the measure may POST: an Ask (a service session's Ask is nobody's: no work log,
# memory, chat, upload or kept session, and always the answer path, never the
# instruction engine) and the briefing's "seen" mark, answered without marking anything
MEASURE_POSTS = frozenset({"/api/ask", "/api/briefing/seen"})
MEASURE_PREFIXES = ("/api/work-log/", "/api/object/", "/api/object-section/", "/api/object-impact/",
                    "/api/explore/", "/api/history/", "/api/brief/", "/api/insights/",
                    "/api/team/", "/api/as-of/", "/api/label-changes/", "/api/label-revisions/",
                    "/api/autopilot/workflows/", "/api/meetings/follow-up/", "/api/alerts/")


def service_route(session: Any, method: str, path: str) -> bool:
    """Whether a service session (the Ask canary, the page measure) is served this route."""
    if getattr(session, "measure", False):
        if method == "POST":
            return path in MEASURE_POSTS
        return method == "GET" and ".." not in path and (
            path in MEASURE_ROUTES or path.startswith(MEASURE_PREFIXES))
    return (method, path) in CANARY_ROUTES


# /api/teams/callback: Microsoft's cross-site redirect carries no session; the signed,
# single-use state it returns binds the sign-in to the person who started it
# (``integrations.teams_delegated.finish``)
PUBLIC_ROUTES = frozenset({("GET", "/api/auth/config"), ("POST", "/api/auth/dev-login"),
                           ("GET", "/api/health"), ("GET", "/api/ready"),
                           ("GET", "/api/teams/callback"), ("POST", "/graph/notify")})
# the loopback addresses a deploy's probe of /api/ready comes from (never through Caddy,
# which answers /api/ready itself and adds X-Forwarded-For to everything it proxies)
LOOPBACK = frozenset({"127.0.0.1", "::1"})

MESSAGES = {
    "sign_in_required": "Please sign in with your PLX Microsoft account.",
    "canary_route_refused": "This service session is served its own routes only.",
    "no_workbench_access": "Your account is signed in but has no Workbench access yet. "
                           "Ask your manager or IT to grant it.",
    "insufficient_scope": "Your sign-in does not include Workbench access. Sign out and "
                          "sign in again.",
    "dev_ticket_required": "Signing in here needs the access link you were given.",
    "dev_ticket_not_accepted": "This demo sign-in does not take an access link.",
    "access_link_refused": "This access link has expired or is not valid. Ask for a new one.",
    "dev_login_not_served": "Demo sign-in is off. Sign in with Microsoft.",
    "not_found": "We couldn't find that, or you don't have access to it.",
    "moved": "This is read from {where} now; reload the app.",
    "invalid_request": "That request wasn't understood. Check it and try again.",
    "request_too_large": "Attachments can be up to ten files, 50 MB together.",
    "origin_refused": "This request came from a page the Workbench does not trust.",
    "not_served": "That part of the Workbench isn't switched on here yet.",
    "forbidden": "Only the owner and the admins can do that.",
    "internal": "The Workbench server failed on this request. Try again; if it keeps happening, "
                "quote incident {incident}.",
}


@contextmanager
def first_partial() -> Iterator[None]:
    """A Today list or a record's page asked for by the app: with no page kept for the
    reader, answered ``reading`` after a moment rather than held for its whole first read
    (``WorkspaceBackend._serving``); the app asks again."""
    from .serving import FIRST_PARTIAL
    token = FIRST_PARTIAL.set(True)
    try:
        yield
    finally:
        FIRST_PARTIAL.reset(token)


def problem(code: str, status: int, **values: Any) -> JSONResponse:
    message = MESSAGES.get(code, MESSAGES["invalid_request"]).format(**values)
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _incident(error: BaseException, where: str) -> str:
    incident = secrets.token_hex(4)
    log.error("incident %s in %s: %s", incident, where, type(error).__name__, exc_info=error)
    return incident


class JSONResponse(_JSONResponse):
    """Every JSON body the API sends, with credential-shaped text and payment card numbers
    masked (SEC-S-09, ``operating.card_text``: a secret or a card in captured mail, an OCR
    reading or a document is never shown, whichever route reads it)."""

    def render(self, content: Any) -> bytes:
        return super().render(mask_served_in(content))


def sse(event: str, payload: Any) -> str:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    data = mask_served_in(data)
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# How often a stream with nothing new to send says it is still alive (an SSE comment the
# app's parser skips): the app stops waiting after a minute of silence (``ask`` in
# app/src/lib/api.ts), so a frozen or restarted server reads as "the answer was cut off",
# never a question that waits for ever, while an answer still being worked on keeps going.
KEEP_ALIVE_SECONDS = 15.0
KEEP_ALIVE = ": keep-alive\n\n"
_STREAM_DONE = object()


def kept_alive(chunks: Iterator[str], every: float = KEEP_ALIVE_SECONDS) -> Iterator[str]:
    """``chunks`` as they come, read on a thread of their own (in this context), with
    ``KEEP_ALIVE`` whenever none came for ``every`` seconds. Closed early (the reader went
    away): ``chunks`` is closed at its next chunk, as a closed stream was before."""
    import contextvars
    import queue
    import threading

    box: "queue.Queue[Any]" = queue.Queue(maxsize=64)
    stop = threading.Event()

    def put(item: Any) -> None:
        while not stop.is_set():
            try:
                box.put(item, timeout=0.5)
                return
            except queue.Full:
                continue

    def pump() -> None:
        try:
            for chunk in chunks:
                if stop.is_set():
                    break
                put(chunk)
        except BaseException as error:  # noqa: BLE001 - handed to the reading side
            put((_STREAM_DONE, error))
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
            put(_STREAM_DONE)

    threading.Thread(target=contextvars.copy_context().run, args=(pump,), daemon=True,
                     name="plx-stream").start()
    try:
        while True:
            try:
                item = box.get(timeout=every)
            except queue.Empty:
                yield KEEP_ALIVE
                continue
            if item is _STREAM_DONE:
                return
            if isinstance(item, tuple) and len(item) == 2 and item[0] is _STREAM_DONE:
                raise item[1]
            yield item
    finally:
        stop.set()


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


# POST routes that change nothing the reader's screens show until they finish on their own
# terms (the Ask counts its answer as a write itself; warming a session is a read-only setup)
READ_POSTS = frozenset({"/api/ask", "/api/ask/files", "/api/ask/warm", "/api/mcp"})
# Writes that change only some of the reader's screens (the first part of a screen's name,
# ``WorkspaceBackend._serving``); every other write leaves none of them current. Home marks
# the briefing seen on each visit: its "new since your visit" marks and counts move; My work,
# Autopilot, the suggestion cards (made from the open alerts, never from the visit) and the
# rest stay current.
WRITE_SCREENS: dict[str, frozenset[str]] = {
    "/api/briefing/seen": frozenset({"briefing", "briefing_items", "home"}),
    "/api/home/item-state": frozenset({"home_needs", "home", "briefing", "briefing_items"}),
}
# Renaming, pinning or deleting an Ask conversation changes none of the reader's screens.
CHAT_WRITES = "/api/ask/chats/"
# Explicitly stopping an Ask run changes none of the reader's screens.
ASK_RUN_WRITES = "/api/ask/runs/"
# Who besides the owner reads the reliability alarms: the Team view's readers (object ids or
# addresses), one list for both (operator_api.team_view, 2026-11-11).
OPS_READERS_ENV = "PLX_TEAM_VIEW_READERS"


# Every request body but a file's (``STREAMED``): notes, decisions, questions and sign-in
# bodies are a few kilobytes to a few megabytes (SEC-007). A file goes to ``STREAMED`` and is
# written to disk as it arrives; no size limit (owner 11-23).
BODY_MAX = 1024 * 1024
# a question's JSON body: its text and history (a file is sent by upload, or inline when small)
ASK_JSON_MAX = 64 * 1024 * 1024   # a transport guard on memory, not a read limit
STREAMED = frozenset({"/api/ask/files"})
# Threads for the synchronous routes at once (AnyIO's default is 40). A screen route's
# thread waits on the read workers (up to its read limit), so at 40 such waits every other
# route queued behind them, the ones that read nothing too (11-16 walk: /api/auth/config
# up to 40 s, /api/health 29 s, /api/me 44 s with 93 requests in flight). A waiting
# thread holds no worker and little memory; the read workers still bound the real work.
ROUTE_THREADS = 256


# A record id no record can have (malformed for its type, ``ObjectWorkspace``): read as
# not found, as an unseen record is (no oracle), never a 500 incident (11-13: a Sage AR id
# with padded spaces in /api/object raised one).
BAD_OBJECT_IDS = frozenset({"object_id_invalid", "object_id_required"})
# A reader's people and conversations (Memory, Home's side panel) worked out from one read
# of their mail are served again for this long (``_people_of``).
PEOPLE_SECONDS = 600

READ_TIMEOUT = UserFacing("timeout", "That took too long to read. Try again in a moment; "
                          "if it keeps happening, tell the Workbench team.", 504)


# Postgres at its connection limit (11-18 3:46-4:12 PM ET "sorry, too many clients
# already", SQLSTATE 53300, also when the store's reconnect was refused for it): the
# Workbench is busy, try again (503), never "the server failed" (500). Any other connection
# failure (wrong password, server down, a pool that got none: its cause unknown) stays an
# incident, so a misconfiguration is logged, never hidden as busy.
# Matched on the error's words too, as libpq gives no SQLSTATE before a session.
_DB_REFUSED = ("too many clients", "remaining connection slots are reserved")


def _db_refused(error: BaseException) -> bool:
    """``error`` or what it was raised from is Postgres refusing a connection at its
    connection limit."""
    seen = 0
    while error is not None and seen < 8:
        if getattr(error, "sqlstate", None) == "53300" \
                or type(error).__name__ == "TooManyConnections" \
                or any(w in str(error) for w in _DB_REFUSED):
            return True
        error = error.__cause__ or error.__context__  # type: ignore[assignment]
        seen += 1
    return False


def _known(error: BaseException) -> Optional[JSONResponse]:
    """The honest answer to a failure with a known meaning, else None (an incident): a
    malformed record id or a ``RecordNotFound`` is not found (404), any other ``KeyError``
    an incident; a store read stopped at its time limit (a SQLite interrupt, a Postgres
    statement cancelled: ``sqlite3.OperationalError`` "interrupted"; a Postgres read that
    waited past the role's ``lock_timeout`` behind a schema change) timed out (504), never
    "the server failed"."""
    import sqlite3
    if _db_refused(error):
        return JSONResponse({"error": {"code": BUSY.code, "message": BUSY.message}},
                            status_code=BUSY.status, headers={"Cache-Control": "no-store",
                                                              "Retry-After": "5"})
    if isinstance(error, RecordNotFound):   # a record not one the reader may read
        return problem("not_found", 404)
    if isinstance(error, ValueError) and str(error) in BAD_OBJECT_IDS:
        return problem("not_found", 404)
    if isinstance(error, sqlite3.OperationalError) and (
            str(error) == "interrupted"
            or str(error).startswith(("postgres: LockNotAvailable", "postgres: QueryCanceled"))):
        return JSONResponse({"error": {"code": READ_TIMEOUT.code,
                                       "message": READ_TIMEOUT.message}},
                            status_code=READ_TIMEOUT.status,
                            headers={"Cache-Control": "no-store"})
    return None


class BodyLimit:
    """A request body over its route's limit is refused (413) before any of it is parsed
    (every route, signed in or not: ``limits`` by path, else ``limit``): a stated length
    over it at once, a streamed one once it passes it. The body is read in full only up
    to the limit, then handed on. GET and HEAD carry no body and pass through."""

    def __init__(self, app: Any, limit: int, limits: Optional[Mapping[str, int]] = None,
                 streamed: frozenset = frozenset()) -> None:
        self.app, self.limit, self.limits = app, limit, dict(limits or {})
        self.streamed = streamed

    async def __call__(self, scope: Any, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http" or scope.get("method") in ("GET", "HEAD") \
                or scope.get("path") in self.streamed:   # a file's route reads its own stream
            await self.app(scope, receive, send)
            return
        limit = self.limits.get(scope.get("path"), self.limit)
        stated = dict(scope.get("headers") or ()).get(b"content-length")
        try:
            too_big = stated is not None and int(stated) > limit
        except ValueError:
            too_big = True
        chunks: list[bytes] = []
        size, more = 0, True
        while more and not too_big:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            too_big = size > limit
            chunks.append(chunk)
            more = message.get("more_body", False)
        if too_big:
            await problem("request_too_large", 413)(scope, receive, send)
            return
        body, sent = b"".join(chunks), False

        async def replay() -> dict:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await self.app(scope, replay, send)


# A refusal reason public health may show as it is: a lower-case code. Any other text (a
# parser's message quoting the file) is shown there as ``POLICY_REFUSED`` only.
_POLICY_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
POLICY_REFUSED = "employee_policy_unreadable"


def public_policy_status(status: Any) -> dict[str, Any] | None:
    """The employee policy's status for public health: state, counts, times and a refusal
    code, never the refusal's text or anything else the status holds."""
    if not isinstance(status, Mapping):
        return None
    error = status.get("reload_error")
    if error is not None:
        error = error if isinstance(error, str) and _POLICY_CODE.fullmatch(error) \
            else POLICY_REFUSED
    alarm = status.get("alarm_error")
    return {"digest": status.get("digest") if isinstance(status.get("digest"), str) else None,
            "reload_error": error,
            "reloads": status.get("reloads") if isinstance(status.get("reloads"), int) else 0,
            "state": status.get("state") if status.get("state") in (
                "in_force", "kept_last_good") else "unknown",
            "refused_since": status.get("refused_since")
            if isinstance(status.get("refused_since"), str) else None,
            # an exception's class name, else the code
            "alarm_error": None if alarm is None else alarm if isinstance(alarm, str)
            and alarm.isidentifier() and len(alarm) <= 64 else POLICY_REFUSED}


def _route_threads() -> None:
    """At least ``ROUTE_THREADS`` threads for the synchronous routes (on the event loop)."""
    from anyio import to_thread
    limiter = to_thread.current_default_thread_limiter()
    if limiter.total_tokens < ROUTE_THREADS:
        limiter.total_tokens = ROUTE_THREADS


_home_group_pages = home_group_pages   # (the name the tests and the route use)


def _briefing_pages(backend: Any, who: Reader, types: list[str], limit: int,
                    since_visit: bool, must_one: bool = False) -> list[Any]:
    """The first page of each of several event types, read at once; one that could not be
    read is left out (its group opens on its own). ``must_one``: every one failing is that
    failure."""
    got: dict[str, Any] = {}

    def one(kind: str) -> None:
        try:
            got[kind] = backend.briefing_items(who, kind, offset=0, limit=limit,
                                               since_visit=since_visit)
        except (UserFacing, TimeoutError) as error:
            got[kind] = error

    with ThreadPoolExecutor(max_workers=min(len(types), 4)) as pool:
        for future in [pool.submit(contextvars.copy_context().run, one, kind)
                       for kind in types]:
            future.result()
    pages = [got[t] for t in types if isinstance(got.get(t), m.BriefingItems)]
    if not pages and must_one:
        raise next(e for e in (got[t] for t in types) if isinstance(e, Exception))
    return pages


def create_app(backend: Any, auth: Any, *, allowed_origins: frozenset[str] = frozenset(),
               auth_config: Optional[dict[str, Any]] = None,
               secure_cookie: bool = True) -> FastAPI:
    """``auth_config``: the public sign-in configuration (``AuthConfig`` fields beyond the
    authenticator's own mode and dev-login switch)."""
    center = EventCenter(backend)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            center.shutdown()

    app = FastAPI(title="PLX Workbench app API", version=m.API_VERSION,
                  description="Workbench API for the web app. Every route but "
                              "auth/config, auth/dev-login and health needs a signed-in "
                              "reader. Errors are {error:{code,message}}. Generated from "
                              "operator_api/models.py by `python3 -m operator_api openapi "
                              "--write`; never edit openapi.json by hand.",
                  docs_url=None, redoc_url=None, openapi_url=None,
                  default_response_class=JSONResponse, lifespan=lifespan)
    app.state.backend, app.state.auth = backend, auth
    app.state.event_center = center
    from .graph_notify import GRAPH_NOTIFY_BODY_MAX, GRAPH_NOTIFY_PATH, register_graph_notify_route
    app.add_middleware(BodyLimit, limit=BODY_MAX,
                       limits={"/api/ask": ASK_JSON_MAX,
                               GRAPH_NOTIFY_PATH: GRAPH_NOTIFY_BODY_MAX},
                       streamed=STREAMED)
    register_graph_notify_route(app, backend)
    # a read whose connection closes cancels its work (``request_cancel``)
    from .request_cancel import CancelOnDisconnect
    app.add_middleware(CancelOnDisconnect)
    # Operator freshness targets (operating.ops_health): the page latency window this
    # process measures, and the checks over the served state dir (none for a fixture).
    from operating.ops_health import CachedHealth, LatencyWindow
    pages = LatencyWindow()
    state_dir = getattr(backend, "state_dir", None)
    ops = CachedHealth(state_dir, pages) if state_dir else None
    # Each person's own writing style for the drafts written as them and their humanize
    # rewrite (``operating.person_style``: learned from the mail they sent; none for a
    # fixture without a state dir, or PLX_PERSON_STYLE=off)
    if state_dir:
        from operating import person_style
        person_style.register(state_dir)
        from operating import measure_candidates as _mc
        _mc.register(state_dir)
    app.state.page_latency = pages
    feedback_store = getattr(backend, "feedback_store", None) \
        or fb.FeedbackStore.for_backend(backend)
    if getattr(backend, "feedback_store", None) is None:
        backend.feedback_store = feedback_store
    app.state.feedback = feedback_store
    from operating import measure_candidates as _mc_mod
    candidate_store = getattr(backend, "candidate_store", None) \
        or _mc_mod.CandidateStore.for_backend(backend)
    app.state.measure_candidates = candidate_store
    # The work log (``operator_api.work_log``): every path below appends what it did.
    work = getattr(backend, "work_log", None) or wl.WorkLog.for_backend(backend)
    app.state.work_log = work
    ask_runs = ar.AskRunRegistry()
    app.state.ask_runs = ask_runs
    if getattr(backend, "work_log", None) is None:
        backend.work_log = work     # the Ask drafts' cards read the reader's kept Asks
    mailbox = getattr(backend, "_mailbox", None) or (lambda who: who.email)
    notes = wl.Notes(work, mailbox, getattr(backend, "mail_conversation", None))
    # Each person's memory (``operator_api.memory``, over the backend's Postgres; none
    # without one): told what every path did, read by the Ask.
    if getattr(backend, "memory", None) is None:
        try:
            backend.memory = mem.Memory.for_backend(backend)
        except Exception as error:  # noqa: BLE001 - no memory: the app runs without it
            logging.getLogger("operator_api.memory").warning(
                "memory off: %s", type(error).__name__)
            backend.memory = None
    notes.memory = backend.memory
    if backend.memory is not None and getattr(backend.memory, "stated", False) is None:
        # what a person states about their drafts ("sign off with Thanks, Ana") is a rule
        # the drafter applies over anything learned (``operating.learning.stated_rules``)
        from operating import learning as _learning
        rules_dir = _learning.ledger_dir(state_dir)

        def _stated(who: Any, item: int, text: Optional[str]) -> Any:
            return _learning.set_stated(rules_dir, _learning.person_id(who.key), item, text)
        backend.memory.stated = _stated if rules_dir is not None else None

        def _unlearn(who: Any, subject: str) -> Any:
            return _learning.unlearn(rules_dir, _learning.person_id(who.key), subject)
        backend.memory.unlearn = _unlearn if rules_dir is not None else None

    # "View as <person>" (UI-07): who the owner may view as and the audit of every view.
    viewing = getattr(backend, "view_as", None) or va.ViewAs(audit=va.Audit(state_dir))

    def is_owner(who: Reader) -> bool:
        decide = getattr(backend, "_is_owner", None)
        try:
            return bool(decide(who)) if callable(decide) else fb.owner_open(who.session)
        except Exception:  # noqa: BLE001 - an undecidable reader is not the owner
            return False

    def ops_reader(who: Reader) -> bool:
        """Who reads the reliability alarms (2026-11-11, the
        Team view's rule): the owner-open reader and the readers ``PLX_TEAM_VIEW_READERS``
        names (object ids or addresses), decided from the signed-in session only."""
        decide = getattr(backend, "_is_owner", None)
        try:
            if callable(decide) and decide(who):
                return True
        except Exception:  # noqa: BLE001 - an undecidable reader is not the owner
            return False
        named = {v.strip().lower() for v in (os.environ.get(OPS_READERS_ENV) or "").split(",")
                 if v.strip()}
        if not named:
            return False
        session = who.session
        held = {str(v).lower() for v in getattr(session, "identities", ()) or ()}
        employee = getattr(session, "employee", None)
        if getattr(employee, "object_id", None):
            held.add(str(employee.object_id).lower())
        return bool(held & named)

    # ---- errors ---------------------------------------------------------------------

    @app.exception_handler(AuthRequired)
    async def _auth(request: Request, error: AuthRequired) -> JSONResponse:
        response = problem(error.code, error.status)
        if (request.headers.get("authorization") or request.cookies.get(SESSION_COOKIE)) \
                and error.code != "canary_route_refused":
            # credentials were sent and refused (expired, revoked, no access): the desktop app
            # forgets what it kept. A request with none (Microsoft mode's first read, before
            # the token is attached) says nothing about the person. A route-scoped refusal on
            # a read-only service session does not invalidate the session itself.
            response.headers[SESSION_HEADER] = SESSION_REFUSED
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
        return _known(error) or problem("internal", 500,
                                        incident=_incident(error, request.url.path))

    # ---- middleware -------------------------------------------------------------------

    @app.middleware("http")
    async def _guard_and_time(request: Request, call_next: Callable) -> Response:
        started = time.perf_counter()
        _route_threads()
        origin = request.headers.get("origin")
        if origin is not None and request.method not in ("GET", "HEAD", "OPTIONS"):
            if not origin_allowed(origin, request.headers, allowed_origins):
                return problem("origin_refused", 403)
        try:
            response = await call_next(request)
        except Exception as error:  # noqa: BLE001 - never a stack trace to a person
            response = _known(error) or problem("internal", 500,
                                                incident=_incident(error, request.url.path))
        wrote = getattr(request.state, "plx_wrote", None)
        if wrote is not None:
            backend.reader_wrote(*wrote)
            if response.status_code < 400:
                center.publish_mine(wrote[0].key)
            again = getattr(backend, "reread_after_write", None)
            if again is not None and wrote[1] is not None and response.status_code < 400:
                again(*wrote)   # the screens it changed, read for the next visit
        route = request.scope.get("route")
        ms = round((time.perf_counter() - started) * 1000, 1)
        path = getattr(route, "path", "unmatched")
        line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "method": request.method, "route": path, "status": response.status_code,
                "ms": ms}
        src = getattr(request.state, "plx_src", None)
        if src is not None:   # where a served page came from (``served.SRC``)
            line["src"] = src
        latency_log.info(json.dumps(line))
        if request.method == "GET" and path.startswith("/api/") and path not in (
                "/api/health", "/api/ready", "/api/ask",
                "/api/ask/runs/{conversation_id}/stream") and response.status_code < 500:
            pages.add(ms)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        door = getattr(request.state, "plx_door", None)
        if door is not None:   # the session's own door (``door_value``), for the desktop store
            response.headers[DOOR_HEADER] = door
        return response

    def reader(request: Request) -> Reader:
        who = auth.authenticate(request.headers, request.cookies)
        # none on a view-as read: its answer is the viewed person's, never kept on the machine
        if request.headers.get(va.HEADER) is not None:
            request.state.plx_door = None
        else:
            doors = getattr(backend, "doors", None)
            access = doors.access_mark(who) if hasattr(doors, "access_mark") else None
            request.state.plx_door = door_value(who, access)
        if getattr(who.session, "warm_ahead", False):   # read-ahead serves nobody
            raise AuthRequired("sign_in_required")
        if getattr(who.session, "view_as", False):   # only ever made below, per request
            raise AuthRequired("sign_in_required")
        if getattr(who.session, "canary", False) and (
                not service_route(who.session, request.method, request.url.path)
                or request.headers.get(va.HEADER) is not None):
            # a service session: the Ask canary (operating.ask_canary) its one question,
            # the page measure the screens' reads; never the reader's log or memory, a
            # write or a view-as
            raise AuthRequired("canary_route_refused", 403)
        asked = request.headers.get(va.HEADER)
        if asked is not None:
            # the owner viewing as someone (``operator_api.view_as``): read only, audited
            try:
                return viewing.reader_for(who, asked, method=request.method,
                                          route=request.url.path, is_owner=is_owner,
                                          reader_type=Reader)
            except va.Refused as refused:
                raise UserFacing("view_as_refused", refused.message, refused.status) from None
        if request.method not in ("GET", "HEAD", "OPTIONS") and \
                request.url.path not in READ_POSTS:
            # a write: the reader's kept screens are not current (before it runs and,
            # in the middleware, after it ran). An Ask under way is no write: its answer
            # counts once given (``WorkspaceBackend.ask``), so a long Ask never makes the asker's
            # other screens wait for a read
            wrote = getattr(backend, "reader_wrote", None)
            if wrote is not None:
                screens = WRITE_SCREENS.get(request.url.path, frozenset() if request.url.path
                                            .startswith((CHAT_WRITES, ASK_RUN_WRITES)) else None)
                wrote(who, screens)
                request.state.plx_wrote = (who, screens)
        return who

    # ---- auth -----------------------------------------------------------------------

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        """Public liveness ("status" stays "ok" while the process serves), the operator
        freshness targets (``ops``: each check's value, target and breach; ages and
        counts only, never a record) and, with an employee resolver, the employee policy's
        digest in force and why its file's latest edit was not applied, if it was not."""
        out: dict[str, Any] = {"status": "ok"}
        # the employee policy in force (its digest) and why a file edit was not applied
        # (reload_error), so an operator sees an edit take or not (SEC-005)
        # (AUDIT4-13: public health holds status, ages and counts; the refusal is a code,
        # its text only on /api/ops/status)
        policy_of = getattr(getattr(auth, "resolver", None), "policy_status", None)
        if callable(policy_of):
            try:
                out["employee_policy"] = public_policy_status(policy_of())
            except Exception as error:  # noqa: BLE001 - liveness never fails on a check
                log.warning("employee policy status failed: %s", type(error).__name__)
                out["employee_policy"] = None
        if ops is None:
            return out
        try:
            return {**out, "ops": ops.get()}
        except Exception as error:  # noqa: BLE001 - liveness never fails on a check
            log.warning("ops health failed: %s", type(error).__name__)
            return {**out, "ops": None}

    @app.get("/api/ready", include_in_schema=False)
    def ready(request: Request) -> Response:
        """Public readiness, for a deploy (``deploy/release/plx-release``, ARCH-T6): 200 once
        this API may be routed readers (``WorkspaceBackend.ready``: it serves and the
        known readers' first read-ahead is kept under this code version), else 503; the
        body is that and the code version only. Asked directly on loopback (not through
        Caddy), it also starts the warm-up now rather than after a first live request:
        no reader is routed to an API a deploy is still probing."""
        from .page_store import code_version
        client = request.client.host if request.client is not None else ""
        if client in LOOPBACK and "x-forwarded-for" not in request.headers:
            start = getattr(backend, "start_warm_now", None)
            if callable(start):
                try:
                    start()
                except Exception as error:  # noqa: BLE001 - the warm-up starts on its own
                    log.warning("warm-up start failed: %s", type(error).__name__)
        try:
            check = getattr(backend, "ready", None)
            is_ready = bool(check()) if callable(check) else True
        except Exception as error:  # noqa: BLE001 - unknown is not ready
            log.warning("readiness check failed: %s", type(error).__name__)
            is_ready = False
        return JSONResponse({"ready": is_ready, "code_version": code_version()},
                            status_code=200 if is_ready else 503,
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/ops/status", include_in_schema=False,
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def ops_status(summary: bool = False, who: Reader = Depends(reader)) -> dict[str, Any]:
        """The reliability alarms' last pass (``operating.ops_alarms``): open incidents,
        recent recoveries, every check and ``running`` (the pass is under 15 minutes
        old). The owner and the Team readers only (403 for every other reader); unit names,
        ages and counts, never a record. ``configured: False`` without a served state dir.
        ``queue``: the host job queue's last snapshot (``operating.jobq.status``: depth,
        running and waiting jobs, refusals, dead letters; class and unit names and numbers
        only), ``fresh: False`` when its worker stopped writing it.

        ``summary=1`` (speed plan 9.3 A4, the shell's nav dot): the same pass's ``configured``,
        ``running``, ``checked_at`` and the ``open`` incidents (and their count), without the
        recovered list, the checks, the queue and the policy: the same source, the same
        readers, less to read and send."""
        if not ops_reader(who):
            raise UserFacing("forbidden", "Only the owner and the Team readers see the "
                             "reliability alarms.", 403)
        if summary:
            if not state_dir:
                return {"configured": False, "running": False, "open": [], "open_count": 0}
            from operating.ops_alarms import read_status
            full = read_status(state_dir)
            opened = full.get("open") or []
            return {"configured": full.get("configured", True),
                    "running": bool(full.get("running")), "checked_at": full.get("checked_at"),
                    "open": opened, "open_count": len(opened)}
        if not state_dir:
            return {"configured": False, "running": False, "open": [], "recovered": [],
                    "checks": [], "queue": {"configured": False, "fresh": False}}
        from operating.jobq.status import read_snapshot
        from operating.ops_alarms import read_status
        out = {**read_status(state_dir), "queue": read_snapshot(state_dir)}
        # the employee policy's refusal in full (public health shows only its code)
        policy_of = getattr(getattr(auth, "resolver", None), "policy_status", None)
        if callable(policy_of):
            try:
                out["employee_policy"] = policy_of()
            except Exception as error:  # noqa: BLE001 - the status page never fails on it
                log.warning("employee policy status failed: %s", type(error).__name__)
                out["employee_policy"] = None
        return out

    @app.get("/api/feeds", response_model=m.FeedStates, tags=["trust"],
             responses={401: {"model": m.ErrorResponse}})
    def feed_states(who: Reader = Depends(reader)) -> m.FeedStates:
        """Whether the background jobs behind Home's lists (reply drafts, autopilot entries,
        findings, late purchase orders and alerts) are running, from the reliability alarms'
        last pass: a stopped job is said, never shown as an empty list. The same for every
        signed-in reader; unit state only, never a record."""
        from . import feed_state
        return m.FeedStates(**feed_state.build(state_dir))

    @app.get("/api/trust", response_model=m.TrustPage, tags=["trust"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def trust_page(who: Reader = Depends(reader)) -> Response:
        """The Trust page: freshness per source system, answer scores per question set,
        alert precision per type, the autopilot's workflows with their countdown, what is
        live, gated and pending, and each system's write switch. The owner and the Team
        readers only (404 for every other reader); counts, times and switches only."""
        if not ops_reader(who):
            raise UserFacing("not_found", "Not found.", 404)
        from . import trust
        page = trust.build(backend, who)
        return JSONResponse(page.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/view-as/people", response_model=m.ViewAsPeople, tags=["identity"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def view_as_people(who: Reader = Depends(reader)) -> m.ViewAsPeople:
        """Whom the owner may view as (active in the Portal, cleared by the people
        exclusion, never the owner): an opaque key and a name each. The owner only (403
        for every other reader). Viewing is read only and audited."""
        if not is_owner(who):
            raise UserFacing("forbidden", "Only the owner can view as someone else.", 403)
        mine = set(getattr(who.session, "identities", ()) or ())
        return m.ViewAsPeople(available=viewing.available,
                              people=[m.ViewAsPerson(**p) for p in viewing.people(mine)])

    @app.post("/api/view-as/{event}", response_model=m.ViewAsMarked, tags=["identity"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}})
    def view_as_mark(event: Literal["start", "stop"], body: m.ViewAsMark,
                     who: Reader = Depends(reader)) -> m.ViewAsMarked:
        """The owner starting or stopping a view as someone, written to the view-as audit
        (the reads in between are audited one by one). The owner only; nothing else
        changes."""
        try:
            viewing.mark(who, body.key, event, is_owner=is_owner)
        except va.Refused as refused:
            raise UserFacing("view_as_refused", refused.message, refused.status) from None
        return m.ViewAsMarked(recorded=True, event=event)

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
        doors = getattr(backend, "doors", None)
        if doors is not None:
            doors.decide(who)   # before the cookie: the reader's first page never decides it
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
        roles = getattr(backend, "my_roles", None)
        return backend.me(who).model_copy(update={"feedback_admin": fb.is_admin(who),
                                                  "roles": roles(who) if roles else [],
                                                  "ops_reader": ops_reader(who),
                                                  "owner": is_owner(who),
                                                  "can_view_as": viewing.available
                                                  and is_owner(who)})

    # ---- events -----------------------------------------------------------------------

    events_doc = {
        200: {"description": "Server-sent events: release, cycle, mine and door; heartbeats are SSE comments.",
              "content": {"text/event-stream": {"schema": {
                  "type": "string", "x-sse-events": {
                      name: {"$ref": f"#/components/schemas/{model.__name__}"}
                      for name, model in m.PAGE_EVENTS.items()}}}}},
        401: {"model": m.ErrorResponse}}

    @app.get("/api/events", tags=["events"], responses=events_doc, response_class=StreamingResponse)
    async def page_events(request: Request, who: Reader = Depends(reader)) -> StreamingResponse:
        """The session's own event stream: no content, only marks that say what to re-read."""
        q = center.subscribe(who.key)

        async def stream():
            from .page_store import code_version
            door = getattr(request.state, "plx_door", None)
            checked = beat = time.monotonic()
            try:
                if center.stopping:
                    return
                yield sse("release", {"release": code_version()})
                if door:
                    yield sse("door", {"door": door})
                while not center.stopping and not await request.is_disconnected():
                    poll_s = EVENTS_POLL_SECONDS
                    item = None
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=poll_s)
                    except asyncio.TimeoutError:
                        pass
                    if center.is_stop(item) or center.stopping:
                        break
                    if item is not None:
                        yield sse(item[0], item[1])
                    now = time.monotonic()
                    if now - checked >= poll_s:
                        checked = now
                        doors = getattr(backend, "doors", None)
                        access = doors.access_mark(who) if doors is not None and hasattr(doors, "access_mark") else None
                        moved = door_value(who, access)
                        if moved and moved != door:
                            door = moved
                            yield sse("door", {"door": moved})
                    if now - beat >= 20.0:
                        beat = now
                        yield KEEP_ALIVE
            finally:
                center.unsubscribe(q)

        sent = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=sent)

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

    def _run_ask_events(who: Reader, body: m.AskRequest,
                        emit: Callable[[str, Any], None],
                        cancelled: Callable[[], bool]) -> None:
        """Execute one Ask turn and emit masked SSE events through ``emit``, writing the
        finished answer (or failure) to the work log even if no HTTP stream is attached."""
        started, steps = time.perf_counter(), []
        failed: Optional[str] = None
        answered = False
        try:
            does = body.clarified is None and (body.instruction is not None
                                               or body.mode == "do") \
                and not getattr(who.session, "canary", False)
            events = (backend.instruct_ask(who, body) if does
                      else asks_or_hands_off(who, body))
            held: list[str] = []
            whole: Optional[str] = None
            sent_text: Optional[str] = None
            for event, payload in itertools.chain(events, [("", None)]):
                if cancelled() and not answered:
                    break
                if held and event != "token":
                    for word in re.findall(r"^\s+|\S+\s*", mask_served("".join(held))):
                        emit("token", m.TokenEvent(text=word))
                    held = []
                if whole is not None and event != "token":
                    full = mask_served(whole)
                    if full.rstrip() != (sent_text or "").rstrip():
                        sent_text = full
                        emit("token", m.TokenEvent(text=sent_text, whole=True))
                    whole = None
                if event == "token" and getattr(payload, "whole", False):
                    held, whole = [], payload.text
                    ended = re.match(r"(?s).*\s", whole)
                    part = mask_served(ended.group(0)) if ended is not None else ""
                    if part.strip() and not (sent_text or "").startswith(part):
                        sent_text = part
                        emit("token", m.TokenEvent(text=part, whole=True))
                    if ended is not None and ended.group(0) == whole:
                        whole = None
                    continue
                if event == "token":
                    held.append(payload.text)
                    continue
                if event not in m.ASK_EVENTS:
                    continue
                if event == "status":
                    steps.append({"step": payload.step, "message": payload.message,
                                  "elapsed_ms": payload.elapsed_ms})
                if event == "answer":
                    answered = True
                    stream_ms = int((time.perf_counter() - started) * 1000)
                    if getattr(payload, "latency_ms", None) in (None, 0) and stream_ms > 0:
                        try:
                            payload = payload.model_copy(update={"latency_ms": stream_ms})
                        except Exception:  # noqa: BLE001
                            pass
                    if not getattr(payload, "outcome", None):
                        try:
                            payload = payload.model_copy(update={"outcome": "answered"})
                        except Exception:  # noqa: BLE001
                            pass
                    from . import answer_cost
                    spent = answer_cost.take(payload.answer_id)
                    notes.ask(who, payload, review={
                        "steps": steps[:40],
                        "stream_ms": stream_ms,
                        **({"cost": spent} if spent else {})})
                emit(event, payload)
            if cancelled() and not answered:
                emit("error", m.ErrorBody(
                    code="stopped", message="Stopped. Ask again whenever you're ready."))
        except UserFacing as error:
            if cancelled():
                emit("error", m.ErrorBody(
                    code="stopped", message="Stopped. Ask again whenever you're ready."))
            else:
                failed = error.code
                emit("error", m.ErrorBody(code=error.code, message=error.message))
        except TimeoutError:
            failed = BUSY.code
            emit("error", m.ErrorBody(code=BUSY.code, message=BUSY.message))
        except (KeyError, LookupError):
            failed = "not_found"
            emit("error", m.ErrorBody(code="not_found",
                                      message=MESSAGES["not_found"]))
        except Exception as error:  # noqa: BLE001 - plain words, never a trace
            if cancelled():
                emit("error", m.ErrorBody(
                    code="stopped", message="Stopped. Ask again whenever you're ready."))
            else:
                incident = _incident(error, "/api/ask")
                failed = f"internal:{incident}"
                emit("error", m.ErrorBody(
                    code="internal", message=MESSAGES["internal"].format(incident=incident)))
        finally:
            if failed and not cancelled():
                notes.ask_failed(who, body, failed, steps=steps[:40], stream_ms=int(
                    (time.perf_counter() - started) * 1000))

    @app.post("/api/ask", tags=["ask"], responses=ask_doc, response_class=StreamingResponse)
    def ask(body: m.AskRequest, request: Request,
            who: Reader = Depends(reader)) -> StreamingResponse:
        """Ask a question; the answer streams as server-sent events.

        User runs execute decoupled from the HTTP connection so a page reload or tab close
        never drops an in-flight answer; only an explicit Stop request cancels it."""
        if getattr(who.session, "canary", False):
            # Service sessions (canary / page measure) run inline with no registry or work log.
            def stream() -> Iterator[str]:
                out: list[str] = []
                # Yield inline through kept_alive as before.
                started, steps = time.perf_counter(), []
                failed: Optional[str] = None
                try:
                    events = asks_or_hands_off(who, body)
                    held: list[str] = []
                    whole: Optional[str] = None
                    sent_text: Optional[str] = None
                    for event, payload in itertools.chain(events, [("", None)]):
                        if held and event != "token":
                            for word in re.findall(r"^\s+|\S+\s*", mask_served("".join(held))):
                                yield sse("token", m.TokenEvent(text=word))
                            held = []
                        if whole is not None and event != "token":
                            full = mask_served(whole)
                            if full.rstrip() != (sent_text or "").rstrip():
                                sent_text = full
                                yield sse("token", m.TokenEvent(text=sent_text, whole=True))
                            whole = None
                        if event == "token" and getattr(payload, "whole", False):
                            held, whole = [], payload.text
                            ended = re.match(r"(?s).*\s", whole)
                            part = mask_served(ended.group(0)) if ended is not None else ""
                            if part.strip() and not (sent_text or "").startswith(part):
                                sent_text = part
                                yield sse("token", m.TokenEvent(text=part, whole=True))
                            if ended is not None and ended.group(0) == whole:
                                whole = None
                            continue
                        if event == "token":
                            held.append(payload.text)
                            continue
                        if event not in m.ASK_EVENTS:
                            continue
                        if event == "status":
                            steps.append({"step": payload.step, "message": payload.message,
                                          "elapsed_ms": payload.elapsed_ms})
                        yield sse(event, payload)
                except UserFacing as error:
                    failed = error.code
                    yield sse("error", m.ErrorBody(code=error.code, message=error.message))
                except TimeoutError:
                    failed = BUSY.code
                    yield sse("error", m.ErrorBody(code=BUSY.code, message=BUSY.message))
                except (KeyError, LookupError):
                    failed = "not_found"
                    yield sse("error", m.ErrorBody(code="not_found",
                                                   message=MESSAGES["not_found"]))
                except Exception as error:  # noqa: BLE001 - plain words, never a trace
                    incident = _incident(error, "/api/ask")
                    failed = f"internal:{incident}"
                    yield sse("error", m.ErrorBody(
                        code="internal", message=MESSAGES["internal"].format(incident=incident)))
                finally:
                    _ = (out, failed, steps, started)
            sent = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
            return StreamingResponse(stream_encoding(request.headers, kept_alive(stream()), sent),
                                     media_type="text/event-stream", headers=sent)

        conversation_id = body.conversation_id or secrets.token_hex(8)
        run_body = (body if body.conversation_id == conversation_id
                    else body.model_copy(update={"conversation_id": conversation_id}))

        def runner(run: ar.AskRun) -> None:
            _run_ask_events(
                who,
                run_body,
                lambda ev, pl: run.publish(ev, pl, sse(ev, pl)),
                run.cancel.is_set,
            )

        run = ask_runs.start(who.key, conversation_id, body.question, runner)
        sent = {
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-PLX-Conversation-Id": conversation_id,
        }
        return StreamingResponse(
            stream_encoding(request.headers, run.subscribe(every=KEEP_ALIVE_SECONDS), sent),
            media_type="text/event-stream",
            headers=sent,
        )

    @app.get("/api/ask/runs/{conversation_id}/stream", tags=["ask"],
             responses={**ask_doc, 404: {"model": m.ErrorResponse}},
             response_class=StreamingResponse)
    def ask_run_stream(request: Request,
                       conversation_id: str = PathParam(..., pattern=r"^[A-Za-z0-9_-]{1,80}$"),
                       who: Reader = Depends(reader)) -> Response:
        """Reattach to an in-flight (or just-finished) Ask run for one of your conversations."""
        run = ask_runs.get(who.key, conversation_id)
        if run is None:
            return problem("not_found", 404)
        sent = {
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-PLX-Conversation-Id": conversation_id,
        }
        stream = run.subscribe(
            snapshot=True,
            every=KEEP_ALIVE_SECONDS,
            make_token_frame=lambda text, whole: sse("token", m.TokenEvent(text=text, whole=whole)),
        )
        return StreamingResponse(
            stream_encoding(request.headers, stream, sent),
            media_type="text/event-stream",
            headers=sent,
        )

    @app.post("/api/ask/runs/{conversation_id}/stop", response_model=m.AskRunStop, tags=["ask"],
              responses={401: {"model": m.ErrorResponse}})
    def ask_run_stop(conversation_id: str = PathParam(..., pattern=r"^[A-Za-z0-9_-]{1,80}$"),
                     who: Reader = Depends(reader)) -> m.AskRunStop:
        """Explicitly stop an in-flight Ask run for one of your conversations."""
        stopped = ask_runs.stop(who.key, conversation_id)
        return m.AskRunStop(stopped=stopped, conversation_id=conversation_id)

    def ask_files() -> Any:
        """The store of streamed uploads: the backend's own (the Ask reads from it), else one
        under the served state dir."""
        own = getattr(backend, "ask_uploads", None)
        if callable(own):
            return own()
        if getattr(app.state, "ask_files", None) is None:
            from operating import ask_uploads
            app.state.ask_files = ask_uploads.Uploads.for_state_dir(
                getattr(backend, "state_dir", None))
        return app.state.ask_files

    @app.post("/api/ask/files", tags=["ask"], response_model=m.AskUpload,
              responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    async def ask_upload(request: Request, name: str = Query(..., min_length=1),
                         who: Reader = Depends(reader)) -> m.AskUpload:
        """Send one file to attach to a question: the request body IS the file (any size),
        written to disk as it arrives, held for the person until they remove it (or a week).
        The answer is its id, which ``/api/ask`` names in ``attachments``."""
        from operating.disk_guard import DiskRefused
        from . import ask_files as files
        uploads = ask_files()
        try:
            meta = await files.receive(request.stream(), uploads, who.key, name)
        except DiskRefused as why:
            raise UserFacing("disk_full", str(why), 507) from None
        except ConnectionError:
            raise UserFacing("upload_cut_off", "The upload stopped before it ended: attach "
                             "the file again.", 400) from None
        if not meta["size"]:
            uploads.forget(who.key, meta["id"])
            raise UserFacing("invalid_request", "That file arrived empty: attach it again.")
        return m.AskUpload(**meta)

    @app.delete("/api/ask/files/{upload_id}", status_code=204, tags=["ask"],
                responses={401: {"model": m.ErrorResponse}})
    def ask_upload_remove(upload_id: str = PathParam(..., pattern=r"^[0-9a-f]{32}$"),
                          who: Reader = Depends(reader)) -> Response:
        """Take a file off a question: remove what ``POST /api/ask/files`` kept."""
        ask_files().forget(who.key, upload_id)
        return Response(status_code=204)

    def asks_or_hands_off(who: Reader, body: m.AskRequest) -> Iterator[tuple[str, Any]]:
        """The Ask's events; when its model gave the line to the drafting desk
        (``plx.hand_off``: the answer's ``instruction`` part is a ``route`` one,
        ``simple_ask.answer_payload``) the instruction engine answers it in its place, as
        though the person had picked "Do it" (the Ask's own answer is never shown or kept)."""
        for event, payload in backend.ask(who, body):
            part = getattr(payload, "instruction", None) if event == "answer" else None
            if part is not None and part.status == "route" and body.clarified is None \
                    and not getattr(who.session, "canary", False):
                yield from backend.instruct_ask(who, body.model_copy(update={"mode": "do"}))
                return
            yield event, payload

    @app.get("/api/suggest", response_model=m.SuggestResponse, tags=["search"],
             responses={401: {"model": m.ErrorResponse}})
    def suggest(q: str = Query(..., min_length=1, max_length=100),
                limit: int = Query(8, ge=1, le=20),
                who: Reader = Depends(reader)) -> m.SuggestResponse:
        """Typo-tolerant autocomplete over the records this reader may see."""
        return backend.suggest(who, q, limit, person=True)

    @app.get("/api/biz/page", response_model=m.BizPage, tags=["search"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                        503: {"model": m.ErrorResponse}})
    def biz_page(kind: Literal["customer", "chain", "supplier", "purchase_order",
                               "sales_order", "product", "batch",
                               "supplier_invoice"],
                 request: Request,
                 h: str = Query("", max_length=4000), chain: str = Query("", max_length=4000),
                 record: str = Query("", max_length=4000),
                 who: Reader = Depends(reader)) -> m.BizPage:
        """A customer's, chain's or supplier's account page, or a PO's or customer order's
        page (header, then lines): every number from the business tables, as you may see
        them, in one read. ``record``: a record id (object id) instead of ``h``: the page
        of the row it was read into."""
        from . import served
        try:
            return backend.biz_page(who, kind, handle=h, chain=chain, record=record)
        finally:
            request.state.plx_src = served.SRC.get()

    page_routes.register(app, backend, reader)

    @app.get("/api/records/views", response_model=m.RecordViews, tags=["search"],
             responses={401: {"model": m.ErrorResponse}})
    def records_views(who: Reader = Depends(reader)) -> m.RecordViews:
        """Every list Records offers: one per business view the door serves, from the
        views' own metadata."""
        return m.RecordViews(views=backend.records_views(who))

    @app.get("/api/records/list", response_model=m.RecordList, tags=["search"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                        503: {"model": m.ErrorResponse}})
    def records_list(view: str = Query(..., max_length=60),
                     f: list[str] = Query([], max_length=100),
                     q: str = Query("", max_length=80),
                     sort: str = Query("", max_length=60),
                     direction: Literal["", "asc", "desc"] = Query(""),
                     facets: bool = Query(True),
                     offset: int = Query(0, ge=0, le=LIST_OFFSET_MOST), limit: int = Query(25, ge=1, le=100),
                     who: Reader = Depends(reader)) -> m.RecordList:
        """A list of one business view's records as you may see them: its columns, sort,
        filters (``f=<column>:<value>``, repeated) and text search come from the view's
        metadata. ``facets=false`` leaves the filters' choices out (a later page of the same
        list)."""
        chosen = {name: value for name, _, value in (x.partition(":") for x in f)}
        return backend.records_list(who, view, chosen, q, sort, direction, offset, limit, facets)

    @app.get("/api/home", response_model=m.Home, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def home(who: Reader = Depends(reader)) -> m.Home:
        with first_partial():
            return backend.home(who)

    @app.get("/api/home/needs", response_model=m.HomeNeeds, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def home_needs(who: Reader = Depends(reader)) -> m.HomeNeeds:
        """Home's unified Needs-you list: Assist matters of every age, late purchase orders,
        alerts routed to you, and (for the owner) new feedback and undefined gap alerts, merged
        and ranked server-side with items older than 90 days folded rather than dropped."""
        with first_partial():
            return backend.home_needs(who, feedback_store)

    @app.post("/api/home/item-state", response_model=m.HomeItemStateResponse, tags=["home"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def home_item_state(body: m.HomeItemStateRequest,
                        who: Reader = Depends(reader)) -> m.HomeItemStateResponse:
        """Close/dismiss, snooze, or restore any Home Needs-you item for this reader and
        immediately invalidate their cached Home Needs-you view."""
        if getattr(getattr(who, "session", None), "view_as", False):
            raise UserFacing("view_as_refused", va.READ_ONLY, 403)
        from . import home_needs as hn
        return hn.set_home_item_state(backend, who, body)

    @app.get("/api/sidebar", response_model=m.Sidebar, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def sidebar(who: Reader = Depends(reader)) -> m.Sidebar:
        """The sidebar's numbers on every page: how many late purchase orders you may see
        and the suppliers with the most, your recent questions and the data's as-of. Small
        and kept for you (never Home's lists); null count when it could not be read."""
        with first_partial():
            return backend.sidebar(who)

    @app.get("/api/data-health", response_model=m.DataHealth, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def data_health(who: Reader = Depends(reader),
                    check: Optional[str] = Query(None, max_length=80),
                    offset: int = Query(0, ge=0, le=LIST_OFFSET_MOST),
                    limit: int = Query(25, ge=1, le=100),
                    q: Optional[str] = Query(None, max_length=200),
                    object_type: Optional[str] = Query(None, max_length=80)) -> m.DataHealth:
        """Every data-health check: the findings you may see, and how many more it found on
        records you may not open (counted, never named). An Admin page: the owner and the
        Team readers only, as its menu entry (403 for every other reader). ``check``: one
        check's findings from ``offset`` (a page of them), cut from the page's one read;
        optional ``q`` and ``object_type`` filter across all rows before slicing."""
        if not ops_reader(who):
            raise UserFacing("forbidden", "Only the owner and the Team readers see data "
                             "health.", 403)
        from . import data_health as dh
        if check is None:
            return backend.data_health(who)   # open counts only: findings are read per check
        return dh.window(backend.data_health(who, check), check, offset, limit,
                         q=q, object_type=object_type)

    @app.get("/api/data-health/export", tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def data_health_export(who: Reader = Depends(reader),
                           check: Optional[str] = Query(None, max_length=80),
                           q: Optional[str] = Query(None, max_length=200),
                           object_type: Optional[str] = Query(None, max_length=80)) -> Response:
        """The findings you may see, one check's or all, as a spreadsheet (CSV): the same
        rows the page lists, never more."""
        if not ops_reader(who):
            raise UserFacing("forbidden", "Only the owner and the Team readers see data "
                             "health.", 403)
        import csv
        import io
        from . import data_health as dh
        full = backend.data_health(who, check) if check else (
            backend.data_health_all(who) if hasattr(backend, "data_health_all")
            else backend.data_health(who))
        if full.status != "read":
            raise UserFacing("page_unavailable", "The checks couldn't be read just now.", 503)
        buf = io.StringIO()
        csv.writer(buf).writerows(dh.export_rows(full, check, q=q, object_type=object_type))
        name = "data-health" + (f"-{re.sub(r'[^a-z0-9_-]', '', check.lower())}" if check else "")
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"',
                                 "Cache-Control": "no-store"})

    @app.get("/api/late", response_model=m.LateList, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def late_list(offset: int = Query(0, ge=0, le=LIST_OFFSET_MOST), limit: int = Query(25, ge=1, le=100),
                  stale: bool = False, who: Reader = Depends(reader)) -> m.LateList:
        """Every late purchase order you may see that is neither closed nor probably closed, a
        page at a time, longest late first (Home shows the first ones: "show all").
        ``stale=true``: the very late ones that show no sign of life instead (probably closed
        or missed in the data)."""
        with first_partial():
            return (backend.late_list(who, offset=offset, limit=limit, stale=True) if stale
                    else backend.late_list(who, offset=offset, limit=limit))

    @app.get("/api/purchase-orders/closure", response_model=m.PoClosure, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def po_closure(handle: Optional[str] = Query(None, max_length=700),
                   object_id: Optional[str] = Query(None, max_length=700),
                   who: Reader = Depends(reader)) -> m.PoClosure:
        """Whether a person closed this purchase order in the Workbench, who, when and why, and
        any receipt or message that arrived after."""
        from . import po_closing
        return po_closing.status(backend, who, po_closing.handle_of(backend, who, handle, object_id),
                                 must_see=True)

    @app.post("/api/purchase-orders/close", response_model=m.PoClosure, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def po_close(body: m.PoCloseRequest, who: Reader = Depends(reader)) -> m.PoClosure:
        """Close a purchase order you may see, for everyone, in the Workbench only (nothing is
        written to Business Central, the Portal or Sage). It leaves every late list and count;
        undo it with ``/api/purchase-orders/reopen``."""
        from . import po_closing
        return po_closing.close(backend, who, po_closing.handle_of(
            backend, who, body.handle, body.object_id), body.reason)

    @app.post("/api/purchase-orders/reopen", response_model=m.PoClosure, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def po_reopen(body: m.PoCloseRequest, who: Reader = Depends(reader)) -> m.PoClosure:
        """Undo a close: the order is back in the late lists if it is still late."""
        from . import po_closing
        return po_closing.reopen(backend, who, po_closing.handle_of(
            backend, who, body.handle, body.object_id))

    @app.get("/api/briefing", response_model=m.Briefing, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def briefing(view: Literal["all", "since_visit"] = "all", pages: bool = False,
                 who: Reader = Depends(reader)) -> m.Briefing:
        """Your briefing: the events routed to you that you may see, ranked by impact; the
        top ones each with why, a suggested next action and how we know, every other one
        counted in its type's group; each check's state; and a preview of the daily
        digest (never sent). ``view=since_visit``: only what is new since your last visit.
        ``pages=true``: also the first page of each group Home lists past its top items
        (``pages``), so Home asks once."""
        with first_partial():
            got = backend.briefing(who, since_visit=view == "since_visit")
        if not pages or got.status != "read":
            return got
        kinds = _home_group_pages(got)
        if not kinds:
            return got
        return got.model_copy(update={"pages": _briefing_pages(
            backend, who, kinds, HOME_GROUP_PAGE_ROWS, view == "since_visit")})

    @app.get("/api/briefing/items", response_model=m.BriefingItems, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def briefing_items(event_type: m.EventType, offset: int = Query(0, ge=0, le=5000),
                       limit: int = Query(25, ge=1, le=100),
                       view: Literal["all", "since_visit"] = "all",
                       who: Reader = Depends(reader)) -> m.BriefingItems:
        """One event type's items of your briefing, a page at a time (a group's "+N more")."""
        return backend.briefing_items(who, event_type, offset=offset, limit=limit,
                                      since_visit=view == "since_visit")

    @app.get("/api/briefing/pages", response_model=m.BriefingPages, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def briefing_pages(event_type: list[m.EventType] = Query(min_length=1, max_length=12),
                       limit: int = Query(25, ge=1, le=100),
                       view: Literal["all", "since_visit"] = "all",
                       who: Reader = Depends(reader)) -> m.BriefingPages:
        """The first page of each of several event types' items of your briefing in one
        request (Home's groups, one answer for what were four): each as
        ``/api/briefing/items`` answers it. A type that could not be read is left out; every
        one failing is that failure."""
        pages = _briefing_pages(backend, who, list(dict.fromkeys(event_type)), limit,
                                view == "since_visit", must_one=True)
        return m.BriefingPages(pages=pages)

    @app.get("/api/briefing/unjudged", response_model=m.BriefingUnjudged, tags=["home"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                        422: {"model": m.ErrorResponse}})
    def briefing_unjudged(check: str = Query(min_length=1, max_length=64),
                          reason: Optional[str] = Query(None, min_length=1, max_length=64),
                          offset: int = Query(0, ge=0, le=100000),
                          limit: int = Query(50, ge=1, le=200),
                          who: Reader = Depends(reader)) -> m.BriefingUnjudged:
        """The records one check could not judge that you may read, a page at a time (a
        data note's list on the briefing)."""
        return backend.briefing_unjudged(who, check, reason, offset=offset, limit=limit)

    @app.get("/api/suggestions", response_model=m.Suggestions, tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def suggestions(who: Reader = Depends(reader)) -> m.Suggestions:
        """What needs your attention and what to do: late or unconfirmed purchase orders
        and the customer orders they hold up, "ETA confirmed?", customer orders at risk and
        margin alerts, ranked by impact; each with how we know, confidence, what is missing,
        a draft message and the actions it offers (``/api/actions/*``: nothing is sent
        without your confirm)."""
        with first_partial():
            return backend.suggestions(who)

    @app.get("/api/approvals", response_model=m.ApprovalsQueue, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def approvals_queue(who: Reader = Depends(reader)) -> m.ApprovalsQueue:
        """Every pending approval on your alert list (mail asks with no answer from the
        approver, Mission Control approvals), by who it waits on and how long, each with a
        drafted nudge. Nothing is sent."""
        with first_partial():
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
        with first_partial():
            return backend.reply_triage(who)

    @app.get("/api/home/bundle", tags=["home"],
             responses={401: {"model": m.ErrorResponse}})
    def home_bundle(who: Reader = Depends(reader)) -> Response:
        """Home's seven first reads in ONE answer (home, sidebar, briefing with its group pages,
        the first 100 late orders, suggestions, replies, approvals): each part is exactly what
        its own route answers this reader, from the same kept pages and the same permission
        checks, with its own status (a part that failed carries its own error; the others
        stand). One round trip instead of seven; the parts run side by side."""
        import contextvars
        from concurrent.futures import ThreadPoolExecutor
        parts: dict[str, Callable[[], Any]] = {
            "home": lambda: home(who),
            "sidebar": lambda: sidebar(who),
            "briefing": lambda: briefing("all", True, who),
            "late": lambda: late_list(offset=0, limit=100, who=who),
            "suggestions": lambda: suggestions(who),
            "replies": lambda: reply_triage(who),
            "approvals": lambda: approvals_queue(who)}

        # viewing as someone: only the parts view-as may read (view_as.ROUTES); the sidebar
        # (the viewed person's own recent questions) is not one: refused as its route is
        viewing_as = bool(getattr(getattr(who, "session", None), "view_as", False))
        as_allowed = {"home", "briefing", "late", "suggestions", "replies", "approvals"}

        part_ms: dict[str, float] = {}

        def one(name: str) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                return _one(name)
            finally:
                part_ms[name] = round((time.perf_counter() - started) * 1000, 1)

        def _one(name: str) -> dict[str, Any]:
            if viewing_as and name not in as_allowed:
                return {"status": 403, "body": {"error": {
                    "code": "view_as_refused", "message": va.READ_ONLY}}}
            try:
                got = parts[name]()
                return {"status": 200, "body": got.model_dump(mode="json", by_alias=True)}
            except UserFacing as error:
                return {"status": error.status,
                        "body": {"error": {"code": error.code, "message": error.message}}}
            except AuthRequired as error:
                return {"status": error.status, "body": {"error": {"code": error.code}}}
            except Exception as error:  # noqa: BLE001 - this part's answer; the others stand
                known = _known(error) or problem("internal", 500, incident=_incident(
                    error, f"/api/home/bundle:{name}"))
                return {"status": known.status_code, "body": json.loads(known.body)}
        with ThreadPoolExecutor(max_workers=len(parts)) as pool:
            futures = {n: pool.submit(contextvars.copy_context().run, one, n) for n in parts}
            out = {n: f.result() for n, f in futures.items()}
        # each part's own time (speed plan 9.3 A1): in the log and as Server-Timing, never in the body
        latency_log.info(json.dumps({"route": "/api/home/bundle", "parts_ms": part_ms}))
        return JSONResponse({"parts": out}, headers={"Server-Timing": ", ".join(
            f"{n};dur={ms}" for n, ms in part_ms.items())})

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

    @app.get("/api/my-work/{item_id}/order-import", tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                        409: {"model": m.ErrorResponse}})
    def my_work_order_import(item_id: str, who: Reader = Depends(reader)) -> Response:
        """Your item's prepared order as an import file (CSV, one row per order line) to
        paste or import into the Portal or Business Central. Nothing is entered."""
        name, data = backend.my_work_order_import(who, item_id)
        data = mask_served(data.decode("utf-8")).encode("utf-8") \
            if isinstance(data, bytes) else mask_served(data)
        return Response(data, media_type="text/csv; charset=utf-8",
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": f'attachment; filename="{name}"'})

    from . import assist_routes   # the list from matters; a matter's item actions first
    assist_routes.install(app, backend, reader, notes)
    from . import assist_jobs   # a whole job handed to Assist (ASSIST10)
    assist_jobs.install(app, backend, reader)
    from . import assist_workflows   # a person's own repeatable workflows (PLAN-1124 (b))
    assist_workflows.install(app, backend, reader)
    from . import sharepoint_upload_routes   # SharePoint folder discovery + artifact upload
    sharepoint_upload_routes.install(app, backend, reader)

    @app.get("/api/autopilot", response_model=m.AutopilotArea, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def autopilot_area(who: Reader = Depends(reader)) -> m.AutopilotArea:
        """The autopilot area: today's prepared items you may see (the owner: everyone's),
        each with its evidence, drafted action and decision; per role and workflow the dev
        and fresh scores, state and switches. Read only."""
        area = backend.autopilot_area(who)
        if not getattr(who.session, "view_as", False):   # nothing logged as the person viewed
            notes.autopilot_prepared(who, area)
        return area

    @app.post("/api/autopilot/never", response_model=m.AutopilotDrafts, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                         409: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def autopilot_never(body: m.AutopilotNeverRequest,
                        who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        """Never (or again) prepare anything for you from one sender: your list, back."""
        return backend.autopilot_never(who, body)

    @app.get("/api/autopilot/colleagues", response_model=m.AutopilotColleagues,
             tags=["actions"], responses={401: {"model": m.ErrorResponse}})
    def autopilot_colleagues(who: Reader = Depends(reader)) -> m.AutopilotColleagues:
        """Whom an alert on your list can be passed to: a key and a name each."""
        return backend.autopilot_colleagues(who)

    @app.post("/api/autopilot/items/{item_id}/hand", response_model=m.AutopilotDrafts,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse},
                                           422: {"model": m.ErrorResponse}})
    def autopilot_hand(item_id: str, body: m.AutopilotHandRequest,
                       who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        """Pass an alert on your list to a colleague: on their list too, under their own
        access, only to set aside there; nothing is decided, sent or written."""
        return backend.autopilot_hand(who, item_id, body)

    @app.post("/api/autopilot/items/{item_id}/take", response_model=m.AutopilotDrafts,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse}})
    def autopilot_take(item_id: str, who: Reader = Depends(reader)) -> m.AutopilotDrafts:
        """Take an item your role holds (no record names who handles it): yours alone from
        now, to check and confirm like any of your items; nothing is sent or written."""
        return backend.autopilot_take(who, item_id)

    @app.post("/api/autopilot/items/{item_id}/decision", response_model=m.AutopilotItem,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse},
                                           422: {"model": m.ErrorResponse}})
    def autopilot_decide(item_id: str, body: m.AutopilotDecisionRequest,
                         who: Reader = Depends(reader)) -> m.AutopilotItem:
        """Accept, edit, reject or snooze an item of your own. Accept runs it through the
        gated write path: a dry run (the decision recorded, nothing written) unless its
        workflow passed the fresh gate for you and you switched it on for yourself."""
        item = backend.autopilot_decide(who, item_id, body)
        notes.autopilot_decided(who, item)
        return item

    @app.post("/api/autopilot/items/{item_id}/reply-from-line", response_model=m.AutopilotItem,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse},
                                           422: {"model": m.ErrorResponse}})
    def autopilot_reply_from_line(item_id: str, body: m.AutopilotLineRequest,
                                  who: Reader = Depends(reader)) -> m.AutopilotItem:
        """Write the reply to a "your call" card of your own from your one line: kept as your
        edit of its draft for you to change and confirm. Nothing is sent."""
        return backend.autopilot_reply_from_line(who, item_id, body)

    _pw_errors = {401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                  409: {"model": m.ErrorResponse}}

    @app.get("/api/teams/connection", response_model=m.TeamsConnection, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def teams_connection(who: Reader = Depends(reader)) -> m.TeamsConnection:
        """Your own Teams sign-in: connected or not, the scopes, when (never the token)."""
        return backend.teams_connection(who)

    @app.post("/api/teams/connect", response_model=m.TeamsConnectStart, tags=["actions"],
              responses={**_pw_errors, 403: {"model": m.ErrorResponse}})
    def teams_connect(who: Reader = Depends(reader)) -> m.TeamsConnectStart:
        """Start your one-time Teams sign-in: open the address it answers."""
        return backend.teams_connect(who)

    @app.get("/api/teams/callback", include_in_schema=False)
    def teams_callback(code: Optional[str] = Query(None, max_length=4096),
                       state: Optional[str] = Query(None, max_length=256),
                       error: Optional[str] = Query(None, max_length=200)) -> Response:
        """Microsoft's redirect back after a sign-in. No session reaches it (a cross-site
        redirect); the signed one-time state completes it only for the person who started
        it, and only with their own Microsoft account."""
        target = backend.teams_callback(code, state, error)
        return Response(status_code=303, headers={"Location": target,
                                                  "Cache-Control": "no-store"})

    @app.delete("/api/teams/connection", response_model=m.TeamsConnection, tags=["actions"],
                responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def teams_disconnect(who: Reader = Depends(reader)) -> m.TeamsConnection:
        """Forget your Teams sign-in: nothing is posted as you after this."""
        return backend.teams_disconnect(who)

    @app.post("/api/autopilot/items/{item_id}/jobs/{index}/prepare",
              response_model=m.PreparedWrite, tags=["actions"],
              responses={**_pw_errors, 422: {"model": m.ErrorResponse}})
    def autopilot_job_prepare(item_id: str, index: int,
                              who: Reader = Depends(reader)) -> m.PreparedWrite:
        """Review one system change an autopilot proposed on an item of yours: it is
        prepared as you (the record read from its system, before and after); confirm it in
        two steps on /api/prepared/{id}. Nothing is written here."""
        return backend.autopilot_job_prepare(who, item_id, index)

    @app.get("/api/prepared", response_model=m.PreparedWrites, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def prepared_list(who: Reader = Depends(reader)) -> m.PreparedWrites:
        """Your open prepared changes (Ask and your autopilots prepared them): nothing in
        them is done before you confirm it."""
        return backend.prepared_list(who)

    @app.get("/api/prepared/{prepared_id}", response_model=m.PreparedWrite,
             tags=["actions"], responses=_pw_errors)
    def prepared_preview(prepared_id: str, who: Reader = Depends(reader)) -> m.PreparedWrite:
        """One prepared change of yours, before and after, with what confirming it does
        now (written at once, or recorded only, and why) and the hash its confirm carries.
        Another person's is not found."""
        return backend.prepared_preview(who, prepared_id)

    @app.post("/api/prepared/{prepared_id}/confirm", response_model=m.PreparedWrite,
              tags=["actions"], responses={**_pw_errors, 422: {"model": m.ErrorResponse}})
    def prepared_confirm(prepared_id: str, body: m.PreparedConfirmRequest,
                         who: Reader = Depends(reader)) -> m.PreparedWrite:
        """Your second step: the preview's hash and confirm=true. Runs the change once,
        as you, under every write guard; a changed preview is refused (409)."""
        return backend.prepared_confirm(who, prepared_id, body)

    @app.post("/api/prepared/{prepared_id}/dismiss", response_model=m.PreparedWrite,
              tags=["actions"], responses=_pw_errors)
    def prepared_dismiss(prepared_id: str, who: Reader = Depends(reader)) -> m.PreparedWrite:
        """Drop a prepared change of yours; nothing is written."""
        return backend.prepared_dismiss(who, prepared_id)

    @app.post("/api/prepared/{prepared_id}/undo", response_model=m.PreparedWrite,
              tags=["actions"], responses=_pw_errors)
    def prepared_undo(prepared_id: str, who: Reader = Depends(reader)) -> m.PreparedWrite:
        """Undo a change of yours that was written, through the inverse recorded when it
        ran (refused when the system has none or someone changed it since)."""
        return backend.prepared_undo(who, prepared_id)

    @app.post("/api/prepared/{prepared_id}/retry", response_model=m.PreparedWrite,
              tags=["actions"], responses=_pw_errors)
    def prepared_retry(prepared_id: str, who: Reader = Depends(reader)) -> m.PreparedWrite:
        """Prepare again a change of yours that was dismissed, expired, failed or only
        recorded: a new prepared change, checked anew; nothing is written."""
        return backend.prepared_retry(who, prepared_id)

    @app.get("/api/instruct/uploads/{upload_id}", response_model=m.UploadPreview,
             tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                          404: {"model": m.ErrorResponse}})
    def upload_preview(upload_id: str, who: Reader = Depends(reader)) -> m.UploadPreview:
        """A document's upload to SharePoint: before and after, re-checked as you now."""
        return backend.upload_preview(who, upload_id)

    @app.post("/api/instruct/uploads/{upload_id}/approve", response_model=m.UploadPreview,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse}})
    def upload_approve(upload_id: str, body: m.UploadApprove,
                       who: Reader = Depends(reader)) -> m.UploadPreview:
        """Approve the upload you previewed (your confirm, for that preview). Recorded as
        would upload: this build has no SharePoint upload path."""
        return backend.upload_approve(who, upload_id, body)

    @app.get("/api/instructions/recurring", response_model=m.RecurringInstructions,
             tags=["actions"], responses={401: {"model": m.ErrorResponse}})
    def recurring_list(who: Reader = Depends(reader)) -> m.RecurringInstructions:
        """Your recurring instructions (only yours)."""
        return m.RecurringInstructions(instructions=backend.recurring_list(who))

    @app.post("/api/instructions/recurring", response_model=m.RecurringInstruction,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           422: {"model": m.ErrorResponse}})
    def recurring_keep(body: m.RecurringKeep,
                       who: Reader = Depends(reader)) -> m.RecurringInstruction:
        """Keep a recurring instruction ("every Monday ..."): when it runs is read from the
        line. Each run prepares its work for you to confirm; nothing is sent on its own."""
        return m.RecurringInstruction(**backend.recurring_keep(
            who, body.text, body.label, body.hour, body.sender, body.subject))

    @app.patch("/api/instructions/recurring/{rid}", response_model=m.RecurringInstruction,
               tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                            404: {"model": m.ErrorResponse},
                                            422: {"model": m.ErrorResponse}})
    def recurring_change(rid: str, body: m.RecurringChange,
                         who: Reader = Depends(reader)) -> m.RecurringInstruction:
        """Change one of yours: its hour, weekday or name, or pause and resume it."""
        return m.RecurringInstruction(**backend.recurring_change(
            who, rid, **body.model_dump(exclude_none=True)))

    @app.delete("/api/instructions/recurring/{rid}", status_code=204, tags=["actions"],
                responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def recurring_stop(rid: str, who: Reader = Depends(reader)) -> Response:
        """Stop one of yours: it is deleted and never runs again."""
        backend.recurring_stop(who, rid)
        return Response(status_code=204)

    @app.get("/api/digests", response_model=m.Digests, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def digests_list(who: Reader = Depends(reader)) -> m.Digests:
        """The digests on offer (morning briefing; decisions, customer feedback and waiting
        requests): all off until you switch one on; each report is yours alone."""
        return m.Digests(digests=backend.digest_list(who))

    @app.put("/api/digests/{digest_id}", response_model=m.Digests, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                        422: {"model": m.ErrorResponse}})
    def digests_set(digest_id: str, body: m.DigestSet,
                    who: Reader = Depends(reader)) -> m.Digests:
        """Switch one of your digests on (at an hour, US Eastern) or off. It runs as you."""
        return m.Digests(digests=backend.digest_set(who, digest_id, body.on, body.hour))

    @app.get("/api/instruct/files/{file_id}", tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def instruct_file(file_id: str, who: Reader = Depends(reader)) -> Response:
        """A document an instruction made for you."""
        from operating.instruct_files import MEDIA
        data, kind, name = backend.instruct_file(who, file_id)
        # saved under the document's own name (an ASCII stand-in for older clients)
        plain = re.sub(r'[^A-Za-z0-9 ._()-]', "_", name).strip() or f"{file_id}.{kind}"
        return Response(data, media_type=MEDIA.get(kind, "application/octet-stream"),
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": f'attachment; filename="{plain}"; '
                                 f"filename*=UTF-8''{quote(name or plain, safe='')}"})

    @app.post("/api/doc-fill", response_model=m.InstructFill, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}})
    def doc_fill(body: m.DocFillRequest, who: Reader = Depends(reader)) -> m.InstructFill:
        """Fill a form for a record: a new file with every blank listed beside its source,
        for you to review. Nothing is saved or sent; saving it beside its SharePoint
        original is the upload you approve."""
        return backend.doc_fill_request(who, body)

    # ---- templates (``operator_api.templates``): each person's own, shared with a role ----

    errs = {401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
            404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
            422: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}}

    def _insights() -> Any:
        if not hasattr(backend, "insights_list"):
            raise UserFacing("insights_not_served", "Insights aren't served here.", 503)
        return backend

    @app.get("/api/insights", response_model=m.InsightList, tags=["insights"],
             responses={401: {"model": m.ErrorResponse}})
    def insights_list(who: Reader = Depends(reader)) -> Response:
        """Your dashboard: the built-in team, org and customer insights you may see, then
        the ones you pinned or saved."""
        out = _insights().insights_list(who)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/insights/all/stream", tags=["insights"], response_class=StreamingResponse,
             responses={401: {"model": m.ErrorResponse}})
    def insights_stream(request: Request, eager: Optional[int] = None,
                        who: Reader = Depends(reader)) -> StreamingResponse:
        """The whole dashboard in one response (one JSON object per line): your list, then
        each chart as it is read (its own result or its own error, each read as you)."""
        backend_ = _insights()
        lines = backend_.insights_stream(who, eager)

        def body() -> Iterator[bytes]:
            for line in lines:
                yield json.dumps(line, separators=(",", ":")).encode() + b"\n"
        sent = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream_encoding(request.headers, body(), sent),
                                 media_type="application/x-ndjson", headers=sent)

    @app.post("/api/insights/save-answer", response_model=m.InsightAnswerPage, tags=["insights"],
              responses=errs)
    def insight_save_answer(body: m.InsightAnswerSave, who: Reader = Depends(reader)) -> Response:
        """Save a whole Ask answer (title, summary, charts, and queries or catalogue measures)
        as one cohesive Insights page strictly scoped to the signed-in reader."""
        out = _insights().insight_save_answer(who, body)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/insights/pages", response_model=m.InsightAnswerPageList, tags=["insights"],
             responses={401: {"model": m.ErrorResponse}})
    def insight_pages_list(who: Reader = Depends(reader)) -> Response:
        """List saved Ask answer pages owned by the signed-in reader."""
        out = _insights().insight_pages_list(who)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/insights/pages/{page_id}", response_model=m.InsightAnswerPage, tags=["insights"],
             responses=errs)
    def insight_page_get(page_id: str = PathParam(pattern=r"^p[a-f0-9]{12}$"),
                         who: Reader = Depends(reader)) -> Response:
        """Read one saved Ask answer page owned by the signed-in reader."""
        out = _insights().insight_page_get(who, page_id)
        if out is None:
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.delete("/api/insights/pages/{page_id}", response_model=m.InsightDeleted, tags=["insights"],
                responses=errs)
    def insight_page_delete(page_id: str = PathParam(pattern=r"^p[a-f0-9]{12}$"),
                            who: Reader = Depends(reader)) -> Response:
        """Remove one of your saved Ask answer pages and all of its tiles."""
        if not _insights().insight_page_delete(who, page_id):
            return problem("not_found", 404)
        return JSONResponse(m.InsightDeleted(deleted=True).model_dump(mode="json"))

    @app.get("/api/insights/{insight_id}", response_model=m.InsightResult, tags=["insights"],
             responses=errs)
    def insight_result(insight_id: str = PathParam(pattern=r"^[a-z0-9_]{1,61}$"),
                       again: bool = False, who: Reader = Depends(reader)) -> Response:
        """One insight as you read it (its stored value with its read time; never read in this
        request: a Refresh starts the read behind it, a tile with nothing stored says 202): its definition (the statement), rows, chart and the people they name."""
        from .insights import FILLING, NotStored
        backend_ = _insights()
        try:
            out = backend_.insight_result(who, insight_id, again, stored_only=True)
        except NotStored:
            if not again and callable(getattr(backend_, "wait_filling", None)) \
                    and backend_.wait_filling(who, insight_id):
                try:
                    out = backend_.insight_result(who, insight_id, False, stored_only=True)
                except NotStored:
                    raise FILLING from None
            else:
                raise FILLING   # its read is running in the background; nothing is computed here
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/insights/{insight_id}/why", tags=["insights"],
             response_class=StreamingResponse, responses=errs)
    def insight_why(request: Request, insight_id: str = PathParam(pattern=r"^[a-z0-9_]{1,61}$"),
                    drill: Optional[str] = Query(None, max_length=64),
                    period: Optional[str] = Query(None, max_length=64),
                    against: Optional[str] = Query(None, max_length=64),
                    filter: Optional[str] = Query(None, max_length=2000),
                    who: Reader = Depends(reader)) -> StreamingResponse:
        """Why did this figure change: step by step (one JSON object per line), each step read
        as you and sent as it is read with the best answer so far; stop reading at any step.
        ``period`` (and ``against``) names the change, ``filter`` (JSON, a dimension to its
        values) narrows the rows explained, ``drill`` reads one ranked dimension's parts."""
        narrowed = None
        if filter is not None:
            try:
                narrowed = json.loads(filter)
            except ValueError:
                narrowed = None
            if not isinstance(narrowed, dict):
                raise UserFacing("invalid_request", "filter is JSON: a dimension to its values.", 422)
        lines = _insights().insight_why_stream(who, insight_id, drill, period, against, narrowed)
        first = next(lines, None)       # a figure that cannot be explained is its own error

        def body() -> Iterator[bytes]:
            if first is not None:
                yield json.dumps(first, separators=(",", ":")).encode() + b"\n"
            for line in lines:
                yield json.dumps(line, separators=(",", ":")).encode() + b"\n"
        sent = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream_encoding(request.headers, body(), sent),
                                 media_type="application/x-ndjson", headers=sent)

    @app.post("/api/insights/{insight_id}/filtered", response_model=m.InsightResult,
              tags=["insights"], responses=errs)
    def insight_filtered(body: m.FilterEdits,
                         insight_id: str = PathParam(pattern=r"^[a-z0-9_]{1,61}$"),
                         who: Reader = Depends(reader)) -> Response:
        """One insight read now with filters you changed or took out (its own statement's
        conditions only), as you: nothing is saved or kept."""
        out = _insights().insight_filtered(who, insight_id, body.edits)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/statement/read", response_model=m.StatementRead, tags=["insights"],
              responses=errs)
    def statement_read(body: m.StatementEdit, who: Reader = Depends(reader)) -> Response:
        """An answer's query read now with filters you changed or took out, as you (your
        door, as your Ask reads): nothing is saved."""
        if not hasattr(backend, "statement_read"):
            raise UserFacing("insights_not_served", "Insights aren't served here.", 503)
        out = backend.statement_read(who, body.sql, body.edits)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/insights", response_model=m.InsightInfo, tags=["insights"],
              responses=errs)
    def insight_save(body: m.InsightSave, who: Reader = Depends(reader)) -> Response:
        """Pin a chart from an answer (or save a statement) to your dashboard: it reads
        again as whoever opens it."""
        out = _insights().insight_save(who, body)
        from . import measure_candidates_api as mc_api
        mc_api.capture_from_saved_tile(
            candidate_store, backend, who,
            tile_id=out.id, title=out.title, question=out.question or "", sql=body.sql)
        return JSONResponse(out.model_dump(mode="json"))

    @app.put("/api/insights/layout", response_model=m.InsightList, tags=["insights"],
             responses=errs)
    def insight_layout(body: m.InsightLayout, who: Reader = Depends(reader)) -> Response:
        """Keep your own arrangement of the dashboard (the order of your charts and the ones
        you took off); gives the dashboard back as it now is."""
        out = _insights().insight_layout(who, body)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.delete("/api/insights/{insight_id}", response_model=m.InsightDeleted,
                tags=["insights"], responses=errs)
    def insight_delete(insight_id: str = PathParam(pattern=r"^s[0-9]{1,18}$"),
                       who: Reader = Depends(reader)) -> Response:
        """Remove one of your saved insights."""
        if not _insights().insight_delete(who, insight_id):
            return problem("not_found", 404)
        return JSONResponse(m.InsightDeleted(deleted=True).model_dump(mode="json"))

    @app.put("/api/insights/{insight_id}/schedule", response_model=m.InsightInfo,
              tags=["insights"], responses=errs)
    def insight_schedule(body: m.InsightSchedule,
                         insight_id: str = PathParam(pattern=r"^s[0-9]{1,18}$"),
                         who: Reader = Depends(reader)) -> Response:
        """When one of your saved insights runs by itself (read as you, through your own
        access) and when what it finds comes to you on Home."""
        out = _insights().insight_schedule(who, insight_id, body)
        return JSONResponse(out.model_dump(mode="json"))

    @app.get("/api/insight-news", response_model=m.InsightNewsList, tags=["insights"],
             responses={401: {"model": m.ErrorResponse}})
    def insight_news(who: Reader = Depends(reader)) -> Response:
        """What your scheduled insights found that came to you (counts only: open the
        insight to read its rows, live, as you)."""
        out = _insights().insight_news(who)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/insight-news/{run_id}/seen", response_model=m.InsightNewsSeen,
              tags=["insights"], responses=errs)
    def insight_news_seen(run_id: str = PathParam(pattern=r"^r[0-9]{1,18}$"),
                          who: Reader = Depends(reader)) -> Response:
        """Put one away: it leaves Home."""
        if not _insights().insight_news_seen(who, run_id):
            return problem("not_found", 404)
        return JSONResponse(m.InsightNewsSeen(seen=True).model_dump(mode="json"))

    @app.post("/api/charts/file", tags=["insights"], responses={
        **errs, 200: {"content": {media: {} for media in (
            "application/pdf", "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet")}, "description": "The file."}})
    def chart_file(body: m.ChartFile, who: Reader = Depends(reader)) -> Response:
        """A chart you were shown as a file for you: Excel (its rows and a live chart over
        them), PowerPoint, Word or PDF, with where its numbers come from. Nothing is read,
        kept or sent."""
        from operator_api.charts import NotDrawn, as_file
        from operating.instruct_files import MEDIA, file_name
        try:
            data = as_file(body.chart.model_dump(mode="json"), body.format)
        except NotDrawn as why:
            raise UserFacing("chart_file", str(why), 422) from None
        name = file_name(body.chart.title or "Chart", body.format)
        plain = re.sub(r'[^A-Za-z0-9 ._()-]', "_", name).strip() or f"chart.{body.format}"
        return Response(data, media_type=MEDIA[body.format],
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": f'attachment; filename="{plain}"'})

    @app.post("/api/export/xlsx", tags=["insights"], responses={
        **errs, 200: {"content": {"application/vnd.openxmlformats-officedocument."
                                  "spreadsheetml.sheet": {}}, "description": "Excel workbook."}})
    def export_xlsx(body: m.ExportXlsxRequest, who: Reader = Depends(reader)) -> Response:
        """Export any chart, table or answer as a formatted Excel (.xlsx) workbook with
        typed values, live native charts and a Sources sheet of full SQL statements. Nothing
        is kept or sent."""
        from operating import excel_export as ex
        from operating.instruct_files import MEDIA, file_name
        from .charts import NotDrawn, _check_shape
        chart_dicts = [c.model_dump(mode="json") for c in body.charts]
        try:
            for c in chart_dicts:
                _check_shape(c)
            data = ex.workbook_xlsx(
                title=body.title,
                summary=body.summary,
                tables=[t.model_dump(mode="json") for t in body.tables],
                charts=chart_dicts,
                citations=body.citations,
            )
        except (ValueError, NotDrawn) as why:
            raise UserFacing("export_xlsx", str(why), 422) from None
        name = file_name(body.title or "Export", "xlsx")
        plain = re.sub(r'[^A-Za-z0-9 ._()-]', "_", name).strip() or "export.xlsx"
        return Response(data, media_type=MEDIA["xlsx"],
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": f'attachment; filename="{plain}"'})

    @app.get("/api/templates", response_model=m.TemplateList, tags=["templates"],
             responses={401: {"model": m.ErrorResponse}})
    def templates_list(doc_type: Optional[m.TemplateDocType] = None,
                       who: Reader = Depends(reader)) -> Response:
        """Your saved templates and those shared with a role of yours, most used first,
        with the document types, the record fields a placeholder may name and the roles
        you may share with."""
        out = backend.templates_list(who, doc_type)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/templates/learned", response_model=m.LearnedTemplates, tags=["templates"],
             responses={401: {"model": m.ErrorResponse}})
    def templates_learned(who: Reader = Depends(reader)) -> Response:
        """The kinds of document learned from your own sent documents (daily), each with
        the words that ask Ask for one."""
        out = backend.learned_templates(who)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/templates", response_model=m.Template, tags=["templates"],
              responses=errs)
    def templates_create(body: m.TemplateSave, who: Reader = Depends(reader)) -> m.Template:
        """Save a template (private unless shared with a role of yours). Pay and finance
        are refused, and every placeholder must name a record field."""
        return backend.template_save(who, body)

    @app.put("/api/templates/{template_id}", response_model=m.Template, tags=["templates"],
             responses=errs)
    def templates_update(body: m.TemplateSave, template_id: int = PathParam(ge=1),
                         who: Reader = Depends(reader)) -> m.Template:
        """Change one of your own templates (one shared with you reads as not found)."""
        return backend.template_save(who, body, template_id)

    @app.delete("/api/templates/{template_id}", response_model=m.TemplateDeleted,
                tags=["templates"], responses=errs)
    def templates_delete(template_id: int = PathParam(ge=1),
                         who: Reader = Depends(reader)) -> Response:
        """Delete one of your own templates."""
        if not backend.template_delete(who, template_id):
            return problem("not_found", 404)
        return JSONResponse(m.TemplateDeleted(deleted=True).model_dump(mode="json"))

    @app.post("/api/templates/propose", response_model=m.TemplateProposal,
              tags=["templates"], responses=errs)
    def templates_propose(body: m.TemplateProposeRequest,
                          who: Reader = Depends(reader)) -> m.TemplateProposal:
        """A draft's text as a template: each value the record holds exactly becomes its
        placeholder. Nothing is saved."""
        return backend.template_propose(who, body)

    @app.post("/api/templates/{template_id}/fill", response_model=m.TemplateFilled,
              tags=["templates"], responses=errs)
    def templates_fill(body: m.TemplateFillRequest, template_id: int = PathParam(ge=1),
                       who: Reader = Depends(reader)) -> m.TemplateFilled:
        """A template filled from a record you can read: each placeholder from one value
        the records agree on, with its source, else marked check. Nothing is sent."""
        return backend.template_fill(who, template_id, body.record)

    @app.get("/api/templates/suggest", response_model=m.TemplateSuggestions,
             tags=["templates"], responses=errs)
    def templates_suggest(doc_type: m.TemplateDocType, who: Reader = Depends(reader)
                          ) -> Response:
        """The templates to offer when you start a document of this type: yours and those
        shared with you, then the layout learned from what you sent of that kind."""
        out = backend.template_suggest(who, doc_type)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/autopilot/switches", response_model=m.AutopilotSwitches,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           403: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse}})
    def autopilot_global(body: m.AutopilotSwitchRequest,
                         who: Reader = Depends(reader)) -> m.AutopilotSwitches:
        """The global kill switch of the write path. Owner only (403 for every other
        reader)."""
        return backend.autopilot_global(who, body)

    @app.post("/api/autopilot/notices/ack", response_model=m.AutopilotArea, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def autopilot_ack_notice(body: m.AutopilotNoticeAck,
                             who: Reader = Depends(reader)) -> m.AutopilotArea:
        """The owner acknowledges that a workflow passed its fresh gate for a role (told
        once). Owner only (403 for every other reader); turns nothing on."""
        return backend.autopilot_ack_notice(who, body)

    @app.post("/api/autopilot/opt-in", response_model=m.AutopilotArea, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def autopilot_opt_in(body: m.AutopilotOptInRequest,
                         who: Reader = Depends(reader)) -> m.AutopilotArea:
        """Switch one of your own workflows on (only while it passes its gate for you: 409
        otherwise) or off. Always your own; recorded on the audit log. It acts only while
        the host's master switch is on, and only on routine items."""
        return backend.autopilot_opt_in(who, body)

    @app.post("/api/autopilot/drafts", response_model=m.AutopilotArea, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def autopilot_drafts(body: m.AutopilotDraftsRequest,
                         who: Reader = Depends(reader)) -> m.AutopilotArea:
        """Switch one choice of prepared work on or off for yourself (``my_drafts``):
        drafts and prepared actions wait in My work for your confirm; nothing is sent or
        written by itself. Always your own; recorded on the audit log."""
        return backend.autopilot_drafts(who, body)

    @app.post("/api/autopilot/mode", response_model=m.AutopilotArea, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def autopilot_mode(body: m.AutopilotChoiceModeRequest,
                       who: Reader = Depends(reader)) -> m.AutopilotArea:
        """Set one category of your own work to off, confirm (prepared in full, one click
        of yours performs it) or auto (performed by itself only where your replay score
        earned it: 409 otherwise). Always your own; recorded on the audit log."""
        return backend.autopilot_mode(who, body)

    @app.post("/api/autopilot/conditions", response_model=m.AutopilotArea, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def autopilot_conditions(body: m.AutopilotConditionsRequest,
                             who: Reader = Depends(reader)) -> m.AutopilotArea:
        """Set your own limits on one category's auto: only these senders, these days,
        these hours (US Eastern), your own stricter bar. Limits only narrow auto."""
        return backend.autopilot_conditions(who, body)

    @app.post("/api/autopilot/items/{item_id}/always", response_model=m.AutopilotArea,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           403: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse},
                                           422: {"model": m.ErrorResponse}})
    def autopilot_always(item_id: str, body: m.AutopilotAlwaysRequest,
                         who: Reader = Depends(reader)) -> m.AutopilotArea:
        """Always do this: auto for this item's sender, or its whole category, where your
        replay score earned auto (409 otherwise). Money and unclear items still wait."""
        return backend.autopilot_always(who, item_id, body)

    @app.post("/api/autopilot/pause", response_model=m.AutopilotArea, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         409: {"model": m.ErrorResponse}})
    def autopilot_pause(body: m.AutopilotPauseRequest,
                        who: Reader = Depends(reader)) -> m.AutopilotArea:
        """Pause (or resume) everything you switched on, at once. Your own only."""
        return backend.autopilot_pause(who, body)

    @app.get("/api/autopilot/workflows/{workflow}", response_model=m.AutopilotWorkflowDetail,
             tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                          404: {"model": m.ErrorResponse}})
    def autopilot_workflow_detail(workflow: str,
                                  who: Reader = Depends(reader)) -> m.AutopilotWorkflowDetail:
        """One workflow: trigger, conditions, actions, gate, mode, per role its row, what it
        caught and missed, its history (owner). A reader outside its roles gets 404."""
        return backend.autopilot_workflow_detail(who, workflow)

    @app.put("/api/autopilot/workflows/{workflow}/mode",
             response_model=m.AutopilotWorkflowDetail, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                        404: {"model": m.ErrorResponse}, 409: {"model": m.ErrorResponse},
                        422: {"model": m.ErrorResponse}})
    def autopilot_set_mode(workflow: str, body: m.AutopilotModeRequest,
                           who: Reader = Depends(reader)) -> m.AutopilotWorkflowDetail:
        """Set a workflow's mode (owner only): off, draft, or auto with approval (after its
        fresh gate passed for the role). A mode this server lacks is refused (409)."""
        return backend.autopilot_set_mode(who, workflow, body)

    @app.post("/api/autopilot/workflows", response_model=m.AutopilotDraftKept,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           403: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse},
                                           422: {"model": m.ErrorResponse}})
    def autopilot_draft(body: m.AutopilotWorkflowDraft,
                        who: Reader = Depends(reader)) -> m.AutopilotDraftKept:
        """Keep a drafted workflow (owner only). It starts in Draft mode, never live."""
        return backend.autopilot_draft(who, body)

    @app.post("/api/autopilot/workflows/{workflow}/replay", response_model=m.AutopilotReplay,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           403: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse}})
    def autopilot_replay(workflow: str, body: m.AutopilotReplayRequest,
                         who: Reader = Depends(reader)) -> m.AutopilotReplay:
        """Try a workflow on past days: caught, missed and false alarms. Owner only; nothing
        is sent or written."""
        return backend.autopilot_replay(who, workflow, body)

    @app.get("/api/autopilot/catalog", response_model=m.AutopilotCatalog, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def autopilot_catalog(who: Reader = Depends(reader)) -> m.AutopilotCatalog:
        """The triggers, conditions and actions the workflow builder offers."""
        return backend.autopilot_catalog(who)

    @app.get("/api/autopilot/schedules", response_model=m.AutopilotSchedules,
             tags=["actions"], responses={401: {"model": m.ErrorResponse}})
    def autopilot_schedules(who: Reader = Depends(reader)) -> m.AutopilotSchedules:
        """The workflows that run on a schedule, as the reader may see them."""
        return backend.autopilot_schedules(who)

    @app.get("/api/autopilot/audit", response_model=m.AutopilotAudit, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def autopilot_audit(who: Reader = Depends(reader)) -> m.AutopilotAudit:
        """The write path's audit log, newest first, with undo where the system has one.
        Owner only (403 for every other reader)."""
        return backend.autopilot_audit(who)

    @app.post("/api/autopilot/audit/{key}/undo", response_model=m.AutopilotAudit,
              tags=["actions"], responses={401: {"model": m.ErrorResponse},
                                           403: {"model": m.ErrorResponse},
                                           409: {"model": m.ErrorResponse}})
    def autopilot_undo(key: str, who: Reader = Depends(reader)) -> m.AutopilotAudit:
        """Undo a live action once through its recorded inverse. Owner only."""
        return backend.autopilot_undo(who, key)

    @app.get("/api/workload", response_model=m.Workload, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def workload(who: Reader = Depends(reader)) -> m.Workload:
        """Open threads awaiting reply, open tasks and alerts per team; per person only
        your own and the teams the owner granted you. Counts only."""
        return backend.workload(who)

    @app.get("/api/team", response_model=m.TeamView, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}})
    def team_view(who: Reader = Depends(reader)) -> m.TeamView:
        """Per person: activity in every captured system over 7 and 28 days, open work,
        median reply time, a daily trend and busiest hours. The owner and the named Team
        readers see everyone; every other reader only their own row. Each row carries a
        weekly tracker (done, received, closed, reply time, time saved)."""
        view = backend.team_view(who)
        if getattr(view, "state", None) != "ready" or not view.people:
            return view
        view = view.model_copy(deep=True)
        team_saved(view.people, who, everyone=view.scope == "everyone")
        return view

    def team_saved(people: list, who: Reader, *, everyone: bool) -> None:
        """Time saved joined into the tracker per request (the work log moves faster than
        the snapshot); everyone's only for the owner (as /api/time-saved): a named Team
        reader sees everyone's activity but only their own time saved."""
        from .team_view import attach_saved
        alone = None if everyone and is_owner(who) else who.key
        try:
            found = wl.time_saved(work, reader=alone, weeks=4)
        except Exception:  # noqa: BLE001 - the page stands without the figure (None)
            found = None
        attach_saved(people, found, alone)

    @app.get("/api/team/{key}", response_model=m.TeamPersonDetail, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def team_person(key: str, who: Reader = Depends(reader)) -> m.TeamPersonDetail:
        """One person's drill-down: each day of the last 28 across systems, who they work
        with most and their open items (marked items you cannot open stay withheld). 404 for
        a person you may not open."""
        found = backend.team_person(who, key).model_copy(deep=True)
        # only a person the reader may open reaches here (404 otherwise)
        team_saved([found.person], who, everyone=True)
        return found

    @app.get("/api/templates/answer-bank", response_model=m.DocList, tags=["templates"],
             responses={401: {"model": m.ErrorResponse}})
    def templates_answer_bank(question: Optional[str] = Query(None),
                              who: Reader = Depends(reader)) -> m.DocList:
        """The questionnaire answers we gave that any customer may get, as you may read
        them (read only), each with the document it came from; ranked against ``question``
        when given. An answer specific to one customer is on that customer's page."""
        with first_partial():
            return backend.answer_bank(who, question)

    @app.get("/api/workload/grants", response_model=m.WorkloadGrantSheet, tags=["actions"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                        404: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}})
    def workload_grant_sheet(who: Reader = Depends(reader)) -> m.WorkloadGrantSheet:
        """The owner's grant editor: each person the exclusion clears, their team and the
        teams they see per person. Owner only (403 for every other reader)."""
        return backend.workload_grant_sheet(who)

    @app.post("/api/workload/grants", response_model=m.WorkloadGrants, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}})
    def assign_workload_grants(body: m.WorkloadGrants,
                               who: Reader = Depends(reader)) -> m.WorkloadGrants:
        """The owner assigns each person's team and the teams each manager sees per
        person (kept in the Workbench's own data directory). Entries for people the grant
        sheet never shows are kept as they are."""
        return backend.assign_workload_grants(who, body)

    @app.get("/api/label-changes/{object_id:path}", response_model=m.LabelChanges,
             tags=["actions"], responses={401: {"model": m.ErrorResponse}})
    def label_changes(object_id: str, who: Reader = Depends(reader)) -> m.LabelChanges:
        """The item page's label and artwork section: changes requested, approved, done,
        stale or with no approval, from your own alert list."""
        return backend.label_changes(who, object_id)

    @app.get("/api/label-revisions/{object_id:path}", response_model=m.LabelRevisions,
             tags=["actions"], responses={401: {"model": m.ErrorResponse}})
    def label_revisions(object_id: str, who: Reader = Depends(reader)) -> m.LabelRevisions:
        """A record's label and artwork revision history: every revision of each file, who
        approved it and when, and on an order the superseded revisions it points at, from
        your own alert list."""
        return backend.label_revisions(who, object_id)

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
        with first_partial():
            return backend.upcoming_meetings(who)

    @app.get("/api/labour", response_model=m.LabourPage, tags=["briefs"],
             responses={401: {"model": m.ErrorResponse}})
    def labour(weeks: int = Query(12, ge=0, le=1000, description="The last this many weeks "
                                  "(0: every week)"), who: Reader = Depends(reader)
               ) -> m.LabourPage:
        """Labour hours from the department time sheets: department and week totals of
        hours, overtime hours and headcount-days, the overtime trend and the overtime-spike
        alert (30% over a department's 8-week median). Hours only, never pay; no per-person
        figure; groups of fewer than 3 people are not shown. Only a reader granted hr or
        ops-management sees any figure (state not_granted otherwise). The last 12 weeks
        by default (``older`` says more are held; weeks=0 gives every week)."""
        return backend.labour(who, weeks)

    @app.get("/api/brief/weekly", response_model=m.Brief, tags=["briefs"],
             responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse}})
    def weekly_review(week: Optional[date] = Query(None, description="Any day of the week "
                                                   "(Monday to Sunday); default: last week"),
                      who: Reader = Depends(reader)) -> m.Brief:
        """The weekly ops review: orders booked, shipped and invoiced, on-time shipping, late
        purchase orders, top alerts, cash position and margin (finance), quality holds, each
        weekly figure against the week before. Generated from the records you may read, no
        model call; every figure cites its sources and nothing hidden is counted."""
        with first_partial():
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

    @app.post("/api/email-draft", response_model=m.EmailDraftPrepared, tags=["actions"],
              responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}})
    def prepare_email_draft(body: m.EmailDraftRequest,
                            who: Reader = Depends(reader)) -> m.EmailDraftPrepared:
        """Draft an email for a brief or answer (with its chart attached as an Office file)
        for confirm-before-send through ``/api/actions/preview`` and ``/api/actions/confirm``.
        Email only; live send remains gated by ``PLX_ACTIONS_LIVE`` and ``PLX_ACTIONS_SENDERS``."""
        return backend.prepare_email_draft(who, body)

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
        try:
            card = backend._card(who, body.card_id)
        except Exception:  # noqa: BLE001 - the confirm below says why
            card = None
        result = backend.action_confirm(who, body)
        try:
            # the row this confirm ended on, kept by the desk: never a read of the whole
            # journal per confirm
            row = backend._desk().recorded(body.idempotency_key)
        except Exception:  # noqa: BLE001 - the log keeps what the result says
            row = None
        notes.action(who, result, row, card)
        return result

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
        view = backend.write_prepare(who, body)
        notes.write(who, "prepared", view)
        return view

    @app.get("/api/entries/{entry_id}", response_model=m.EntryHold, tags=["writes"],
             responses=_wr)
    def entry_view(entry_id: str, who: Reader = Depends(reader)) -> m.EntryHold:
        """An order or PO you prepared that is held until its checks are settled: each
        value it needs, cited, confirmed by you or still a check with its suggestions."""
        return backend.entry_view(who, entry_id)

    @app.post("/api/entries/{entry_id}/confirm", response_model=m.EntryHold,
              tags=["writes"], responses=_wr)
    def entry_confirm(entry_id: str, body: m.EntryConfirm, who: Reader = Depends(reader)
                      ) -> m.EntryHold:
        """Enter or confirm one value of your held entry (recorded as yours, with when).
        Nothing is written to BC: with no check left the entry goes to the write desk,
        where a colleague who holds the write grant approves it."""
        return backend.entry_confirm(who, entry_id, body)

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
        view = backend.write_approve(who, intent_id, body)
        notes.write(who, "approved", view)
        return view

    @app.post("/api/writes/{intent_id}/execute", response_model=m.WriteIntentView,
              tags=["writes"], responses=_wr)
    def write_execute(intent_id: str, body: m.WriteExecute, who: Reader = Depends(reader)
                      ) -> m.WriteIntentView:
        """Run an approved entry once per entry: today recorded as "would write" with the
        exact request; live writing is off."""
        view = backend.write_execute(who, intent_id, body)
        notes.write(who, "executed", view)
        return view

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
        found = backend.watch_follow(who, body)
        notes.watch(who, "followed", found)
        return found

    @app.delete("/api/watch/follow/{object_id:path}", response_model=m.WatchFollow,
                tags=["watch"], responses={401: {"model": m.ErrorResponse}})
    def watch_unfollow(object_id: str = PathParam(min_length=1, max_length=400),
                       who: Reader = Depends(reader)) -> m.WatchFollow:
        """Stop following a record."""
        found = backend.watch_unfollow(who, object_id)
        notes.watch(who, "unfollowed", found)
        return found

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
        found = backend.watch_rule_create(who, body)
        notes.watch(who, "rule_added", found)
        return found

    @app.delete("/api/watch/rules/{rule_id}", response_model=m.WatchRuleDeleted,
                tags=["watch"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse}})
    def watch_rule_delete(rule_id: str = PathParam(min_length=1, max_length=40),
                          who: Reader = Depends(reader)) -> m.WatchRuleDeleted:
        """Delete one of your rules."""
        gone = backend.watch_rule_delete(who, rule_id)
        if gone.deleted:
            notes.watch(who, "rule_removed", {"rule_id": rule_id})
        return gone

    @app.post("/api/watch/alerts/read", response_model=m.WatchReadResult, tags=["watch"],
              responses={401: {"model": m.ErrorResponse}})
    def watch_read(body: m.WatchReadRequest, who: Reader = Depends(reader)
                   ) -> m.WatchReadResult:
        """Mark some of your alerts read."""
        read = backend.watch_read(who, body)
        notes.alerts_read(who, body.alert_ids, read.marked)
        return read

    @app.post("/api/briefing/seen", response_model=m.BriefingSeen, tags=["home"],
              responses={401: {"model": m.ErrorResponse}})
    def briefing_seen(who: Reader = Depends(reader)) -> m.BriefingSeen:
        """Mark your briefing seen now: the next "since your last visit" starts here."""
        if getattr(who.session, "canary", False):
            return m.BriefingSeen(ok=False)   # a service session marks nothing of anyone's
        return backend.briefing_seen(who)

    @app.post("/api/briefing/watcher/{item_id}", response_model=m.WatcherItemState,
              tags=["home"], responses={400: {"model": m.ErrorResponse},
                                        401: {"model": m.ErrorResponse},
                                        404: {"model": m.ErrorResponse}})
    def watcher_item_state(item_id: str, body: m.WatcherItemStateRequest,
                           who: Reader = Depends(reader)) -> m.WatcherItemState:
        """Dismiss or snooze one item the AI watcher flagged in your briefing (``open``
        undoes it). Only your own briefing changes; nothing is sent."""
        done = backend.watcher_item_state(who, item_id, body)
        notes.alert_state(who, done, body)
        return done

    @app.get("/api/object/{object_type}/{object_id:path}", response_model=m.ObjectPage,
             tags=["object"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse}})
    def object_page(object_type: str, object_id: str,
                    quick: bool = Query(False, description="A first read without the "
                                        "checks (complete=false), shown while the full "
                                        "page is read"),
                    core: bool = Query(False, description="The record's own details "
                                       "only (core=true), served before the quick read; "
                                       "status 'reading' until that lands"),
                    who: Reader = Depends(reader)) -> Response:
        """One record's page. ``object_id``: the URL-encoded object id, or a business
        reference (e.g. a PO number) resolved within ``object_type``."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            with first_partial():
                page = (backend.object_core(who, object_type, object_id) if core
                        else backend.object_page(who, object_type, object_id, quick=True)
                        if quick else backend.object_page(who, object_type, object_id))
        except RecordNotFound:   # only that: any other error is a logged 500
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
        except RecordNotFound:   # only that: any other error is a logged 500
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
        if object_type == "PurchaseOrder":
            # served from the store only (route_budgets.SERVES history:PurchaseOrder): never
            # computed here again (speed specs 9.3 V2)
            return problem("moved", 410, where="/api/page/history/")
        try:
            out = backend.record_history(who, object_type, object_id, cursor)
        except ValueError:
            return problem("invalid_request", 422)
        except RecordNotFound:   # only that: any other error is a logged 500
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
        except RecordNotFound:   # only that: any other error is a logged 500
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

    @app.get("/api/object-section/{section}/{object_type}/{object_id:path}",
             response_model=m.ObjectSection, tags=["object"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def object_section(section: Literal["late", "people", "landed_cost", "trail", "margin",
                                        "knows", "documents", "formula"],
                       object_type: str, object_id: str,
                       who: Reader = Depends(reader)) -> Response:
        """One slow section of a record's page (late check, people, landed cost, how we
        know), read after its quick first read and merged into it. Not found reads the
        same as a record the reader cannot see."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            out = backend.object_section(who, object_type, object_id, section)
        except RecordNotFound:   # only that: any other error is a logged 500
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
        except RecordNotFound:   # only that: any other error is a logged 500
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
        except (LookupError, ValueError) as error:   # only a not-found: else a logged 500
            if not source_refused(error):
                raise
            return problem("not_found", 404)
        return JSONResponse(view.model_dump(mode="json"))

    @app.get("/api/source/records", response_model=m.SourceRecords, tags=["object"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def source_records(handle: str = Query(..., min_length=3, max_length=2000),
                       who: Reader = Depends(reader)) -> Response:
        """The records citing a source (the ones read from it and the ones linked to
        those), as this reader may read them. A source the reader cannot open is not
        found, as on ``/api/source``; a record they cannot read is left out."""
        try:
            out = backend.source_records(who, handle)
        except (LookupError, ValueError) as error:   # only a not-found: else a logged 500
            if not source_refused(error):
                raise
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

    @app.post("/api/feedback", response_model=m.FeedbackResponse, tags=["feedback"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def feedback(body: m.FeedbackRequest, who: Reader = Depends(reader)) -> Response:
        """Rate an answer you were given: up or down, what was wrong, the claims that were
        wrong and the right answer, kept as you saw the answer. The rating and its record
        are one write: when either fails, neither is kept."""
        try:
            entry, out = fb.record_answer(feedback_store, backend, who, body,
                                          then=lambda: backend.feedback(who, body))
        except RecordNotFound:   # not an answer given to this reader; else a logged 500
            return problem("not_found", 404)
        notes.answer_rated(who, body, entry)
        if body.rating == 1:
            from . import measure_candidates_api as mc_api
            mc_api.capture_from_accepted_answer(
                candidate_store, backend, who, body.answer_id,
                (entry.get("context") or {}).get("markings"))
        out = out.model_copy(update={"feedback_id": entry["id"],
                                     "stored_in": [*out.stored_in, "feedback_store"]})
        return JSONResponse(out.model_dump(mode="json"))

    @app.post("/api/feedback/item", response_model=m.FeedbackResponse, tags=["feedback"],
              responses={401: {"model": m.ErrorResponse}})
    def feedback_item(body: m.ItemFeedbackRequest, who: Reader = Depends(reader)) -> Response:
        """A briefing or alert item: useful, not useful or wrong. Not useful or wrong
        suppresses it in your briefing (the AI watcher's and the proactive engine's)."""
        entry = fb.record_item(feedback_store, backend, who, body)
        notes.item_rated(who, body)
        return JSONResponse(m.FeedbackResponse(ok=True, feedback_id=entry["id"],
                                               stored_in=["feedback_store"]).model_dump())

    @app.post("/api/feedback/note", response_model=m.FeedbackResponse, tags=["feedback"],
              responses={401: {"model": m.ErrorResponse}})
    def feedback_note(body: m.NoteFeedbackRequest, who: Reader = Depends(reader)) -> Response:
        """A note on a page, a record, a list, the app itself or anything else: general
        feedback needs only the note; a page or record is attached when sent."""
        entry = fb.record_note(feedback_store, backend, who, body)
        notes.note(who, body, entry)
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

    @app.get("/api/answers/gaps", response_model=m.GapList, tags=["feedback"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def answer_gaps(who: Reader = Depends(reader)) -> Response:
        """What Ask and Genie were asked that no view or measure defines, with how many times
        (the owner and the admins)."""
        if not fb.is_admin(who):
            return problem("forbidden", 403)
        from operating import gap_list
        out = m.GapList(on=gap_list.path() is not None,
                        gaps=[m.GapRow(**g, defined=gap_list.defined(g["kind"], g["name"]))
                              for g in gap_list.counts()])
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/answers/observed", response_model=m.AskObserved, tags=["feedback"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def answers_observed(who: Reader = Depends(reader)) -> Response:
        """How the Ask has been doing: per day the answers, failures, time and steps, and every
        failed question (the owner and the admins; the question text and the error text only for the owner)."""
        if not fb.is_admin(who):
            return problem("forbidden", 403)
        got = work.observed()
        if got is None:
            out = m.AskObserved(available=False, days=[], failures=[])
        else:
            from .feedback import owner_open
            owner = bool(owner_open(getattr(who, "session", None)))
            out = m.AskObserved(available=True, days=[m.AskDay(**d) for d in got["days"]],
                                failures=[m.AskFailure(at=f["at"], error=f["error"] if owner else None,
                                                       question=f["question"] if owner else None)
                                          for f in got["failures"]])
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

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
        except RecordNotFound:   # only that: any other error is a logged 500
            return problem("not_found", 404)
        shown = fb.Viewer(backend, who, [row]).entry(row)
        return JSONResponse(_feedback_entry(shown, who, True).model_dump(mode="json"))

    # ---- candidate business measure definitions --------------------------------------

    @app.get("/api/measures/candidates", response_model=m.MeasureCandidateList,
             tags=["measures"], responses={401: {"model": m.ErrorResponse},
                                           403: {"model": m.ErrorResponse}})
    def measure_candidates_list(
        status: Optional[m.MeasureCandidateStatus] = None,
        who: Reader = Depends(reader),
    ) -> Response:
        """Candidate measure definitions captured from accepted answers, saved charts, or
        direct proposals (no silent cap; respects per-reader exclusion and masking)."""
        if getattr(getattr(who, "session", None), "view_as", False):
            raise UserFacing("view_as_refused", va.READ_ONLY, 403)
        from . import measure_candidates_api as mc_api
        owner = is_owner(who)
        admin = fb.is_admin(who)
        all_rows, _ = candidate_store.list(status=None)
        visible_all = mc_api.present_candidates(
            all_rows, backend=backend, reader=who, owner=owner, admin=admin)
        counts = {s: 0 for s in _mc_mod.CANDIDATE_STATES}
        for item in visible_all:
            counts[item.status] = counts.get(item.status, 0) + 1
        shown = [item for item in visible_all if status is None or item.status == status]
        out = m.MeasureCandidateList(
            can_review=owner, total=len(shown), counts=counts, candidates=shown)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/measures/candidates", response_model=m.MeasureCandidate,
              tags=["measures"], responses={401: {"model": m.ErrorResponse},
                                            403: {"model": m.ErrorResponse},
                                            404: {"model": m.ErrorResponse},
                                            422: {"model": m.ErrorResponse}})
    def measure_candidates_propose(
        body: m.MeasureCandidatePropose,
        who: Reader = Depends(reader),
    ) -> Response:
        """Propose or capture a candidate shared measure definition from SQL, a saved chart,
        or an accepted Ask answer."""
        if getattr(getattr(who, "session", None), "view_as", False):
            raise UserFacing("view_as_refused", va.READ_ONLY, 403)
        from . import measure_candidates_api as mc_api
        sql = (body.sql or "").strip()
        title = (body.title or "").strip()
        question = (body.question or "").strip()
        source_kind = body.source_kind
        source_ref = (body.source_ref or "").strip()
        markings = fb.served_markings(backend, who)

        if not sql and body.insight_id:
            store = backend.insight_store() if hasattr(backend, "insight_store") else None
            spec = store.get(who.key, body.insight_id) if store is not None else None
            if spec is None:
                return problem("not_found", 404)
            sql = spec.sql
            title = title or spec.title
            question = question or spec.question
            source_kind = "saved_tile"
            source_ref = source_ref or spec.id
        elif not sql and body.answer_id:
            try:
                kept = backend.answer_context(who, body.answer_id)
            except RecordNotFound:
                return problem("not_found", 404)
            queries = [q for q in (kept.get("queries") or ()) if isinstance(q, Mapping) and q.get("sql")]
            chosen = next((q for q in queries if not q.get("measure_id")), queries[0] if queries else None)
            if chosen is None:
                raise UserFacing("invalid_request", "That answer did not run a SQL query to propose.", 422)
            sql = str(chosen["sql"])
            title = title or str(chosen.get("title") or "")
            question = question or str(kept.get("question") or "")
            source_kind = "accepted_answer"
            source_ref = source_ref or body.answer_id
            kept_markings = kept.get("markings")
            if kept_markings is None or markings is None:
                markings = None
            else:
                markings = sorted((set(kept_markings) | set(markings)) & set(fb.SENSITIVE))

        if not sql:
            raise UserFacing("invalid_request", "Send a SQL statement, a saved chart id, or an answer id.", 422)

        guard = fb.exclusion()
        hidden = fb.reader_excluded(who, guard)
        try:
            row = candidate_store.capture(
                sql=sql,
                source_kind=source_kind,
                title=title,
                question=question,
                source_ref=source_ref,
                name=(body.name or "").strip(),
                description=(body.description or "").strip(),
                owner=(body.owner or "").strip(),
                proposer_key=who.key,
                proposer_name=None if hidden else who.display_name,
                proposer_hidden=hidden,
                markings=markings,
                guard=guard,
            )
        except _mc_mod.CandidateError as exc:
            raise UserFacing(exc.code, exc.message, exc.status) from None
        presented = mc_api.present_candidates(
            [row], backend=backend, reader=who, owner=is_owner(who), admin=fb.is_admin(who), guard=guard)
        if not presented:
            safe_row = m.MeasureCandidate(
                id=str(row["id"]),
                signature=str(row["signature"]),
                status=row["status"],
                source_kind=row["source_kind"],
                usage_count=int(row.get("usage_count") or 1),
                first_seen_at=str(row["first_seen_at"]),
                last_seen_at=str(row["last_seen_at"]),
                proposed_id="withheld",
                proposed_name="Existing candidate definition",
                proposed_description=None,
                proposed_owner="Company",
                proposed_unit=str(row.get("proposed_unit") or "number"),
                proposed_grain=str(row.get("proposed_grain") or "row"),
                withheld=["pending"],
            )
            return JSONResponse(safe_row.model_dump(mode="json"))
        return JSONResponse(presented[0].model_dump(mode="json"))

    @app.post("/api/measures/candidates/{candidate_id}/approve",
              response_model=m.MeasureCandidate, tags=["measures"],
              responses={401: {"model": m.ErrorResponse},
                         403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse},
                         409: {"model": m.ErrorResponse},
                         422: {"model": m.ErrorResponse}})
    def measure_candidates_approve(
        candidate_id: str,
        body: Optional[m.MeasureCandidateApprove] = None,
        who: Reader = Depends(reader),
    ) -> Response:
        """Owner-only: promote a candidate into the live governed measure overlay."""
        if getattr(getattr(who, "session", None), "view_as", False):
            raise UserFacing("view_as_refused", va.READ_ONLY, 403)
        if not is_owner(who):
            return problem("forbidden", 403)
        from . import measure_candidates_api as mc_api
        guard = fb.exclusion()
        edits = body.model_dump(exclude_none=True) if body is not None else None
        try:
            row = candidate_store.approve(
                candidate_id,
                reviewer_name=who.display_name,
                edits=edits,
                guard=guard,
            )
        except _mc_mod.CandidateError as exc:
            raise UserFacing(exc.code, exc.message, exc.status) from None
        presented = mc_api.present_candidates(
            [row], backend=backend, reader=who, owner=True, admin=True, guard=guard)
        return JSONResponse(presented[0].model_dump(mode="json"))

    @app.post("/api/measures/candidates/{candidate_id}/dismiss",
              response_model=m.MeasureCandidate, tags=["measures"],
              responses={401: {"model": m.ErrorResponse},
                         403: {"model": m.ErrorResponse},
                         404: {"model": m.ErrorResponse}})
    def measure_candidates_dismiss(
        candidate_id: str,
        body: Optional[m.MeasureCandidateDismiss] = None,
        who: Reader = Depends(reader),
    ) -> Response:
        """Owner-only: dismiss a candidate definition from the review queue."""
        if getattr(getattr(who, "session", None), "view_as", False):
            raise UserFacing("view_as_refused", va.READ_ONLY, 403)
        if not is_owner(who):
            return problem("forbidden", 403)
        from . import measure_candidates_api as mc_api
        guard = fb.exclusion()
        try:
            row = candidate_store.dismiss(
                candidate_id,
                reviewer_name=who.display_name,
                reason=body.reason if body is not None else None,
                guard=guard,
            )
        except _mc_mod.CandidateError as exc:
            raise UserFacing(exc.code, exc.message, exc.status) from None
        presented = mc_api.present_candidates(
            [row], backend=backend, reader=who, owner=True, admin=True, guard=guard)
        return JSONResponse(presented[0].model_dump(mode="json"))

    # ---- the work log ---------------------------------------------------------------

    def _work_event(row: dict, who: Reader, everyone: bool) -> m.WorkLogEvent:
        mine = row["reader"] == who.key
        return m.WorkLogEvent(
            mine=mine, reader_name=row.get("reader_name") if everyone else None,
            **{k: row[k] for k in ("seq", "at", "kind", "verb", "title", "thread", "source",
                                   "source_id", "records", "people", "thread_name",
                                   "pinned", "case_id", "loop")})

    @app.get("/api/memory", response_model=m.MemoryItems, tags=["work-log"],
             responses={401: {"model": m.ErrorResponse}})
    def memory_items(who: Reader = Depends(reader)) -> Response:
        """What the Workbench remembers about you and uses in every answer: your standing
        instructions, preferences, open commitments and facts, newest first. Only yours
        (the owner too sees only their own)."""
        store = getattr(backend, "memory", None)
        if store is None or getattr(who.session, "view_as", False):
            out = m.MemoryItems(items=[], available=False)
        else:
            try:
                decide = getattr(backend, "_memory_decide", None)
                rows = store.items(who, decide=decide(who, wait=True) if decide else None)
                out = m.MemoryItems(available=store._reading(who) is not None,
                                    items=[_memory_item(r) for r in rows])
            except Exception as error:  # noqa: BLE001 - unreadable now: said, never guessed
                logging.getLogger("operator_api.memory").warning(
                    "memory list failed: %s", type(error).__name__)
                out = m.MemoryItems(items=[], available=False)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    people_kept: dict[str, tuple[Any, float, Any]] = {}
    people_lock = threading.Lock()

    def _people_of(mail: tuple, oid: str) -> Any:
        """``my_people.people_and_threads`` of the reader's mail read, worked out once per
        read of their mail (the same kept read: ``HISTORY``) for PEOPLE_SECONDS: 10-12 s a
        request for a staff mailbox on the 11-18 walk, on every Home."""
        from operating import my_people
        now = time.monotonic()
        with people_lock:
            hit = people_kept.get(oid)
        if hit is not None and hit[0] is mail and now - hit[1] <= PEOPLE_SECONDS:
            return hit[2]
        found = my_people.people_and_threads(mail[0], oid, mail[1],
                                             now=datetime.now(timezone.utc),
                                             finance=mem.finance_text)
        with people_lock:
            people_kept[oid] = (mail, now, found)
            if len(people_kept) > 256:
                people_kept.pop(next(iter(people_kept)))
        return found

    @app.get("/api/memory/people", response_model=m.MemoryPeople, tags=["work-log"],
             responses={401: {"model": m.ErrorResponse}})
    def memory_people(who: Reader = Depends(reader)) -> Response:
        """The people you work with most and your conversations going (whose turn it is),
        read from your own mailbox for the last 90 days; nothing stored. Only yours."""
        from operating import my_people
        out = m.MemoryPeople(available=False)
        mail_of = getattr(backend, "_assist_mail", None)
        oid = str(getattr(who.session, "subject_oid", "") or "").lower()
        if mail_of is not None and oid and not getattr(who.session, "view_as", False):
            try:
                # never held on a cold mailbox: the read runs in the background and the
                # page asks again while ``reading`` (a pool thread waits 1 s at most)
                mail = mail_of(who, 1.0)
                reading = getattr(backend, "_assist_mail_reading", None)
                if mail is None and callable(reading) and reading(who):
                    return JSONResponse(m.MemoryPeople(available=False, reading=True)
                                        .model_dump(mode="json"),
                                        headers={"Cache-Control": "no-store"})
                found = None if mail is None else _people_of(mail, oid)
                if found is not None:
                    out = m.MemoryPeople(
                        available=True, threads=[m.MemoryThread(**t) for t in found["threads"]],
                        people=[m.MemoryPerson(name=my_people.label(p["address"]), **p)
                                for p in found["people"]])
            except Exception as error:  # noqa: BLE001 - unreadable now: said, never guessed
                logging.getLogger("operator_api.memory").warning(
                    "memory people failed: %s", type(error).__name__)
                out = m.MemoryPeople(available=False)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    def _memory_item(row: Mapping[str, Any]) -> m.MemoryItem:
        from .assistant_tools import autopilot_effect
        due = mem.due_of(row["text"], row["at"]) if row["kind"] == "commitment" else None
        rules = autopilot_effect(row["text"]) if row["kind"] in ("instruction",
                                                                 "preference") else []
        return m.MemoryItem(due=due, autopilot=rules, **{
            k: row[k] for k in m.MemoryItem.model_fields if k not in ("due", "autopilot")})

    def _memory_saved(who: Reader, body: m.MemorySave, replaces: Optional[int]) -> Response:
        store = getattr(backend, "memory", None)
        if store is None:
            raise UserFacing("memory_refused", mem.WRITE_REFUSED["unavailable"], 503)
        kept, why = store.write(who, kind=body.kind, text=body.text, replaces=replaces,
                                records=body.records)
        if kept is None:
            if why == "not_found":
                return problem("not_found", 404)
            raise UserFacing("memory_refused", mem.WRITE_REFUSED.get(why, why or ""),
                             503 if why == "unavailable" else 422)
        decide = getattr(backend, "_memory_decide", None)
        rows = [r for r in store.items(who, decide=decide(who, wait=True) if decide else None)
                if r["id"] == kept]
        if not rows:   # kept, but not openable to this reader now (it cites a source)
            raise UserFacing("memory_refused", mem.WRITE_REFUSED["unavailable"], 503)
        item = _memory_item(rows[0])
        from . import simple_ask
        simple_ask.WARM.note_memory_changed()
        return JSONResponse(item.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/memory", response_model=m.MemoryItem, tags=["work-log"],
              responses={401: {"model": m.ErrorResponse}, 422: {"model": m.ErrorResponse},
                         503: {"model": m.ErrorResponse}})
    def memory_write(body: m.MemorySave, who: Reader = Depends(reader)) -> Response:
        """Tell the Workbench something to remember in your own words: a standing
        instruction, a preference, a commitment or a fact. Used in every answer of yours,
        never anyone else's. Pay and finance, and anything naming someone the Workbench
        leaves out, are refused."""
        return _memory_saved(who, body, None)

    @app.put("/api/memory/{item_id}", response_model=m.MemoryItem, tags=["work-log"],
             responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse},
                        422: {"model": m.ErrorResponse}, 503: {"model": m.ErrorResponse}})
    def memory_correct(body: m.MemorySave, item_id: int = PathParam(ge=1),
                       who: Reader = Depends(reader)) -> Response:
        """Correct one of your items in your own words: the new text replaces it, and
        nothing the Workbench learns later writes over it. Another person's reads as not
        found."""
        return _memory_saved(who, body, item_id)

    @app.post("/api/memory/{item_id}/done", response_model=m.MemoryForgotten,
              tags=["work-log"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def memory_done(item_id: int = PathParam(ge=1), who: Reader = Depends(reader)) -> Response:
        """Mark one of your commitments done: it leaves Today and your memory, and your
        memory log keeps that it was done. Another person's reads as not found."""
        store = getattr(backend, "memory", None)
        if store is None or getattr(who.session, "view_as", False) \
                or not store.done(who, item_id):
            return problem("not_found", 404)
        from . import simple_ask
        simple_ask.WARM.note_memory_changed()
        return JSONResponse(m.MemoryForgotten(forgotten=True).model_dump(mode="json"))

    @app.delete("/api/memory/{item_id}", response_model=m.MemoryForgotten, tags=["work-log"],
                responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def memory_forget(item_id: int = PathParam(ge=1), who: Reader = Depends(reader)) -> Response:
        """Delete one thing the Workbench remembers about you; it is not used again. Only
        your own (another person's reads as not found)."""
        store = getattr(backend, "memory", None)
        if store is None or getattr(who.session, "view_as", False) \
                or not store.forget(who, item_id):
            return problem("not_found", 404)
        from . import simple_ask
        simple_ask.WARM.note_memory_changed()
        return JSONResponse(m.MemoryForgotten(forgotten=True).model_dump(mode="json"))

    @app.get("/api/work-log", response_model=m.WorkLogPage, tags=["work-log"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def work_log(q: Optional[str] = Query(None, max_length=200),
                 kind: Optional[m.WorkLogKind] = None,
                 record: Optional[str] = Query(None, max_length=700),
                 thread: Optional[str] = Query(None, max_length=420),
                 since: Optional[str] = Query(None, max_length=40),
                 until: Optional[str] = Query(None, max_length=40),
                 case_id: Optional[str] = Query(None, max_length=120),
                 pinned: bool = False, waiting: bool = False,
                 everyone: bool = Query(False, description="The owner only: every person's."),
                 offset: int = Query(0, ge=0, le=100_000), limit: int = Query(50, ge=1, le=200),
                 who: Reader = Depends(reader)) -> Response:
        """Your work log, newest first: what you asked, read, dismissed, approved, sent,
        noted, saved and watched, with the records and people involved. Search with ``q``;
        ``record`` is the record page's section; ``waiting``: your sent messages still
        open. The owner may read everyone's (``everyone``)."""
        if everyone and not is_owner(who):
            return problem("forbidden", 403)
        rows, total = work.events(reader=None if everyone else who.key, q=q, kind=kind,
                                  record=record, thread=thread, since=since, until=until,
                                  case_id=case_id, pinned=pinned, open_loops=waiting,
                                  limit=limit, offset=offset)
        out = m.WorkLogPage(events=[_work_event(r, who, everyone) for r in rows],
                            total=total, offset=offset, all_readers=everyone)
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/work-log/autopilot-days", response_model=m.AutopilotScorecard,
             tags=["work-log"], responses={401: {"model": m.ErrorResponse}})
    def autopilot_days(days: int = Query(14, ge=1, le=60),
                       who: Reader = Depends(reader)) -> m.AutopilotScorecard:
        """Your autopilot scorecard per day from your own work log: items prepared for
        you, accepted, edited and rejected. Yours only."""
        found = wl.autopilot_days(work, reader=who.key, days=days)
        if found is None:
            return m.AutopilotScorecard(available=False)
        return m.AutopilotScorecard(available=True,
                                    days=[m.AutopilotScoreDay(**d) for d in found])

    @app.get("/api/time-saved", response_model=m.TimeSaved, tags=["work-log"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def time_saved(everyone: bool = Query(False, description="The owner only: every "
                                          "person's."),
                   weeks: int = Query(4, ge=1, le=12),
                   who: Reader = Depends(reader)) -> m.TimeSaved:
        """The time the Workbench saved you each week (the owner: everyone), from your work
        log: an estimate, its method stated. Counts only what was used."""
        if everyone and not is_owner(who):
            raise UserFacing("forbidden", "Only the owner sees everyone's time saved.", 403)
        found = wl.time_saved(work, reader=None if everyone else who.key, weeks=weeks)
        if found is None:
            return m.TimeSaved(available=False, all_readers=everyone)
        # the owner is never one of the people in a people view: their own row stays out
        # of everyone's (their own view, everyone False, is theirs alone)
        people = [m.TimeSavedPerson(name=p["name"], mine=p["reader"] == who.key,
                                    weeks=[m.TimeSavedWeek(**w) for w in p["weeks"]])
                  for p in found["people"] if not (everyone and p["reader"] == who.key)]
        return m.TimeSaved(available=True, all_readers=everyone, weeks=found["weeks"],
                           people=people, minutes=found["minutes"], basis=found["basis"],
                           method=found["method"])

    @app.get("/api/work-log/threads/{thread:path}", response_model=m.WorkLogThread,
             tags=["work-log"], responses={401: {"model": m.ErrorResponse},
                                           404: {"model": m.ErrorResponse}})
    def work_log_thread(thread: str, who: Reader = Depends(reader)) -> Response:
        """One of your threads with what continuing it reopens: an Ask session's turns as
        you saw them, an autopilot item, a card, a write; and your other work on its
        records."""
        if not wl.valid_thread(thread):
            return problem("not_found", 404)
        rows, total = work.events(reader=who.key, thread=thread, limit=200)
        active_run = None
        if thread.startswith("ask:"):
            live = ask_runs.get(who.key, thread[len("ask:"):])
            if live is not None and not live.done:
                active_run = m.AskActiveRun(**live.active_summary())
        if not rows and active_run is None:
            return problem("not_found", 404)
        ids = {r["object_id"] for e in rows for r in e["records"]}
        related: dict[int, dict] = {}
        for oid in sorted(ids)[:10]:
            for e in work.events(reader=who.key, record=oid, limit=20)[0]:
                if e["thread"] != thread:
                    related[e["seq"]] = e
        meta = work._meta(who.key, thread)
        resumed = kept_turns(who, wl.resume(
            thread, [{**r, "seq": r["seq"]} for r in rows]))
        out = m.WorkLogThread(
            thread=thread, name=meta["name"], pinned=meta["pinned"], case_id=meta["case_id"],
            closed_at=meta["closed_at"], events=[_work_event(r, who, False) for r in rows],
            related=[_work_event(r, who, False)
                     for r in sorted(related.values(), key=lambda e: -e["seq"])[:20]],
            resume=m.WorkLogResume(**resumed, active_run=active_run))
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    def kept_turns(who: Reader, resumed: dict) -> dict:
        """An Ask session's kept turns as this reader may see them now: a turn whose
        cited source is no longer open to them (or cannot be decided) is withheld."""
        for turn in resumed.get("turns") or ():
            try:
                held = backend.ask_withheld(who, cited_handles(turn["answer"] or {}))
                again = getattr(backend, "instruction_withheld", None)
                held = held or (again(who, turn["answer"] or {}) if again else None)
            except Exception:  # noqa: BLE001 - undecidable: withheld
                held = "undecided"
            if held:
                turn["answer"] = None
                turn["withheld"] = ("A source of this answer is no longer open to you. Ask "
                                    "again to see what you can open now.")
            elif isinstance(turn.get("answer"), dict):
                if turn["answer"].get("outcome") is None:
                    turn["answer"]["outcome"] = "answered"
                if not isinstance(turn["answer"].get("latency_ms"), int):
                    review = turn["answer"].get("_review")
                    sms = review.get("stream_ms") if isinstance(review, dict) else None
                    turn["answer"]["latency_ms"] = sms if isinstance(sms, int) else 0
                turn["answer"].pop("_kept", None)
                turn["answer"].pop("_cited", None)
                turn["answer"].pop("_review", None)
        return resumed

    @app.post("/api/work-log/threads/{thread:path}", response_model=m.WorkLogThread,
              tags=["work-log"], responses={401: {"model": m.ErrorResponse},
                                            404: {"model": m.ErrorResponse}})
    def work_log_organise(thread: str, body: m.WorkLogOrganise,
                          who: Reader = Depends(reader)) -> Response:
        """Name, pin, attach to a case or close one of your own threads."""
        try:
            work.organise(who.key, thread, name=body.name, pinned=body.pinned,
                          case_id=body.case_id, closed=body.closed)
        except (KeyError, ValueError):
            return problem("not_found", 404)
        return work_log_thread(thread, who)

    @app.post("/api/ask/warm", response_model=m.AskWarmStatus, tags=["ask"],
              responses={401: {"model": m.ErrorResponse}})
    def ask_warm(who: Reader = Depends(reader)) -> Response:
        """Start a warm Ask session for this reader when the Ask box opens or focuses
        (Ask spec 19 S3). Non-blocking; no model call is made."""
        from . import simple_ask
        out = m.AskWarmStatus(**simple_ask.warm_status(backend, who))
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/ask/chats", response_model=m.AskChats, tags=["ask"],
             responses={401: {"model": m.ErrorResponse}})
    def ask_chats(q: Optional[str] = Query(None, max_length=200),
                  limit: int = Query(100, ge=1, le=500),
                  who: Reader = Depends(reader)) -> Response:
        """Your Ask conversations, pinned first then the most recent: open one with
        ``/ask?c=<conversation_id>`` and go on asking in it. ``q``: words in its name or
        its questions. Yours only."""
        from . import simple_ask
        simple_ask.ready(backend, who)   # the next question starts on a ready session
        found = work.chats(who.key, q=q, limit=limit)
        chats_list = [dict(c) for c in (found or ())]
        if found is not None:
            active = ask_runs.active_for(who.key)
            if active:
                words = [w.lower() for w in (q or "").split()[:8]]
                by_id = {c["conversation_id"]: c for c in chats_list}
                for live in active:
                    existing = by_id.get(live.conversation_id)
                    if existing is not None:
                        existing["running"] = True
                        if live.started_at > existing["last_at"]:
                            existing["last_at"] = live.started_at
                    else:
                        title = (live.question or "Untitled")[:120]
                        if words and any(w not in title.lower() for w in words):
                            continue
                        chats_list.append({
                            "conversation_id": live.conversation_id,
                            "title": title,
                            "named": False,
                            "pinned": False,
                            "questions": 1,
                            "last_at": live.started_at,
                            "running": True,
                        })
                chats_list.sort(key=lambda c: (bool(c.get("pinned")), str(c.get("last_at") or "")),
                                reverse=True)
                chats_list = chats_list[:limit]
        out = m.AskChats(available=found is not None,
                         chats=[m.AskChat(**c) for c in chats_list])
        return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/api/ask/chats/{conversation_id}", response_model=m.AskChats, tags=["ask"],
              responses={401: {"model": m.ErrorResponse}, 404: {"model": m.ErrorResponse}})
    def ask_chat_update(conversation_id: str, body: m.AskChatUpdate,
                        who: Reader = Depends(reader)) -> Response:
        """Rename, pin or delete one of your own Ask conversations; your chats list after."""
        thread = f"ask:{conversation_id}"
        try:
            if body.deleted:
                work.hide(who.key, thread)
            elif body.name is not None or body.pinned is not None:
                work.organise(who.key, thread, name=body.name, pinned=body.pinned)
        except (KeyError, ValueError):
            return problem("not_found", 404)
        return ask_chats(None, 100, who)

    @app.post("/api/work-log/saved", response_model=m.WorkLogPage, tags=["work-log"],
              responses={401: {"model": m.ErrorResponse}})
    def work_log_saved(body: m.WorkLogSave, who: Reader = Depends(reader)) -> Response:
        """A Save or Unsave on a record page or a question, kept in your work log."""
        if body.kind == "object" and body.object_id and body.object_type:
            thread = f"record:{body.object_type}/{body.object_id}"
            records = [{"object_id": body.object_id, "type": body.object_type,
                        "label": body.label}]
        else:
            thread, records = "record:saved/question", []
        if not wl.valid_thread(thread):
            # a record id outside the thread grammar (every projected id: ``{"id":...}``)
            # is kept under one saved-records thread with its record attached, as notes
            # and watches are; a 422 here lost every Save of a real record silently
            thread = "record:saved/record"
        notes._put(who, kind="save", verb="saved" if body.saved else "unsaved",
                   title=body.label, thread=thread, source="saved", records=records)
        rows, total = work.events(reader=who.key, thread=thread, limit=1)
        return JSONResponse(m.WorkLogPage(events=[_work_event(r, who, False) for r in rows],
                                          total=total).model_dump(mode="json"))

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
            review_requested=bool(row.get("review_requested")),
            served_version=ctx.get("served_version"), app_version=ctx.get("app_version"))

    @app.post("/api/mcp", include_in_schema=False)
    async def genie_mcp_endpoint(request: Request) -> JSONResponse:
        who = reader(request)
        if request.headers.get(va.HEADER) is not None:
            raise UserFacing(
                "view_as_refused",
                "MCP endpoint is read-only for the authenticated reader; view-as is not permitted",
                403,
            )
        from .genie_mcp import handle_mcp_request
        return await handle_mcp_request(request, backend, who)

    return app


def openapi_document(app: FastAPI) -> dict[str, Any]:
    """The OpenAPI document with every SSE event model in ``components.schemas``."""
    from fastapi.openapi.utils import get_openapi
    from pydantic.json_schema import models_json_schema

    doc = get_openapi(title=app.title, version=app.version, description=app.description,
                      routes=app.routes)
    _, defs = models_json_schema([(model, "serialization") for model in
                                  [*m.ASK_EVENTS.values(), *m.PAGE_EVENTS.values(), m.ErrorResponse]],
                                 ref_template="#/components/schemas/{model}")
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    for name, schema in (defs.get("$defs") or {}).items():
        schemas.setdefault(name, schema)
    return doc
