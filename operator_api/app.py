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

import json
import logging
import secrets
import time
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
    "dev_login_not_served": "Demo sign-in is off. Sign in with Microsoft.",
    "not_found": "We couldn't find that, or you don't have access to it.",
    "invalid_request": "That request wasn't understood. Check it and try again.",
    "origin_refused": "This request came from a page the Workbench does not trust.",
    "not_served": "That part of the Workbench isn't switched on here yet.",
    "internal": "Something went wrong on our side. Try again; if it keeps happening, "
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
            host = request.headers.get("host") or ""
            if origin not in allowed_origins and urlsplit(origin).netloc != host:
                return problem("origin_refused", 403)
        try:
            response = await call_next(request)
        except Exception as error:  # noqa: BLE001 - never a stack trace to a person
            response = problem("internal", 500, incident=_incident(error, request.url.path))
        route = request.scope.get("route")
        latency_log.info(json.dumps({
            "method": request.method, "route": getattr(route, "path", "unmatched"),
            "status": response.status_code,
            "ms": round((time.perf_counter() - started) * 1000, 1)}))
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    def reader(request: Request) -> Reader:
        return auth.authenticate(request.headers, request.cookies)

    # ---- auth -----------------------------------------------------------------------

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
                            max_age=8 * 60 * 60)
        return m.DevLoginResponse(ok=True, me=backend.me(who))

    @app.post("/api/auth/logout", response_model=m.LogoutResponse, tags=["auth"])
    def logout(request: Request, response: Response,
               who: Reader = Depends(reader)) -> m.LogoutResponse:
        """Ends a dev session (Entra sign-out is the front end's MSAL logout)."""
        if isinstance(auth, DevAuth):
            auth.logout(request.cookies.get(SESSION_COOKIE))
            response.delete_cookie(SESSION_COOKIE, path="/api")
        return m.LogoutResponse(ok=True)

    @app.get("/api/me", response_model=m.Me, tags=["identity"],
             responses={401: {"model": m.ErrorResponse}, 403: {"model": m.ErrorResponse}})
    def me(who: Reader = Depends(reader)) -> m.Me:
        return backend.me(who)

    # ---- ask --------------------------------------------------------------------------

    ask_doc = {
        200: {"description": "Server-sent events. Each event's `event:` is its name and "
                             "`data:` one JSON object: understanding (UnderstandingEvent), "
                             "status (StatusEvent), sources (SourcesEvent), token "
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

    @app.get("/api/object/{object_type}/{object_id:path}", response_model=m.ObjectPage,
             tags=["object"], responses={401: {"model": m.ErrorResponse},
                                         404: {"model": m.ErrorResponse}})
    def object_page(object_type: str, object_id: str,
                    who: Reader = Depends(reader)) -> Response:
        """One record's page. ``object_id``: the URL-encoded object id, or a business
        reference (e.g. a PO number) resolved within ``object_type``."""
        if len(object_type) > 60 or len(object_id) > 700 or not object_id:
            return problem("invalid_request", 422)
        try:
            page = backend.object_page(who, object_type, object_id)
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(page.model_dump(mode="json"))

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
        try:
            out = backend.feedback(who, body)
        except (KeyError, LookupError):
            return problem("not_found", 404)
        return JSONResponse(out.model_dump(mode="json"))

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
