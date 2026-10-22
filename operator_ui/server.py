"""Loopback operator console: the case workspace and the object workspace.

The console owns presentation and request validation. ``case_workspace`` serves the
case routes for one session identity (``operating.session_identity``). A live console takes the
identity from the authenticated request: every ``/api/*`` request carries a signed
session ticket as its bearer credential, verified and re-resolved per request
(``ticket_sessions``). A fixture console serves one fixture session behind a random
access token. No connector or business system is contacted by this
module. The object workspace's controls (``OBJECT_POST_ROUTES``: confirm / reject a
link, propose a follow-up, record a question family's follow-ups and draft requests on a
case, take an intent's next step) are POSTs with the session's CSRF token, for the
request's session: the first three write only the Workbench's own review and case stores;
the fourth calls the action path (``ActionPath``), which requests approval
through the MC client's write door and dispatches only on a recorded approval. The ``UnifiedPlatform`` capability routes (objectives, campaigns, watches,
autonomy, transactions, streams, engineering) were retired with the platform in
deletion-8, and the injected-service views (overview, findings, recovery, entity,
workflow, approve, execute) in deletion-18; the console is launched by
``python3 -m operator_ui.case_console``.

    from operator_ui.server import serve
    serve(port=8765, ticket_sessions=sessions)                   # live
    serve(port=8765, case_workspace=workspace,
          session=fixture_session(...))                         # fixture
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import sys
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from operator_ui import objective_box
from operator_ui.home import home


HERE = Path(__file__).resolve().parent
MAX_BODY = 16_384
MAX_ID = 256
OBJECT_ROUTES = ("/api/objects", "/api/object", "/api/object-source", "/api/investigate",
                 "/api/object-actions", "/api/object-case", "/api/status-card")
# The person's controls (operator_ui.object_workspace): a review, a follow-up and an
# answer's follow-ups recorded on a case write the Workbench's own stores only; an action
# step goes through the action path; an ask runs the read-only agent loop for the
# session's reader (a POST: a model run, CSRF-bound), and a replay runs a kept session's
# read-only tool calls again for that reader; an objective runs the same loop on Home's
# free-form objective (operator_ui.objective_box), its follow-ups proposed only.
OBJECT_POST_ROUTES = ("/api/object-review", "/api/object-follow-up", "/api/object-action",
                      "/api/object-ask", "/api/investigate-record", "/api/object-replay",
                      "/api/objective", "/api/link-confirm")
# The console's static files, by route.
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
          "/workspace_actions.js": ("workspace_actions.js", "text/javascript; charset=utf-8"),
          "/link_confirm.js": ("link_confirm.js", "text/javascript; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"),
          "/favicon.ico": ("favicon.svg", "image/svg+xml")}


class TicketSessions(Protocol):
    def authenticate(self, ticket: str) -> tuple[Any, Any]: ...


def make_server(host: str = "127.0.0.1", port: int = 0, *, case_workspace: Any = None, session: Any = None,
                ticket_sessions: TicketSessions | None = None,
                object_workspace: Any = None, journal: Any = None) -> HTTPServer:
    """Return a bound server. Only loopback listeners are accepted.

    Case routes need a session identity (``operating.session_identity``):

    * ``ticket_sessions`` (live): ``authenticate(bearer)`` verifies the request's
      signed session ticket, or the employee's Entra access token when the console
      has an Entra resource (``case_console.TicketSessions``), and returns
      ``(SessionIdentity, CaseWorkspace)`` for that employee, or raises
      ``PermissionError`` (its ``reason`` is the 401's detail). Every ``/api/*``
      request is authenticated this way; there is no other credential.
    * ``case_workspace`` with a fixture ``session``: one fixture identity behind
      a random access token (the workspace's actor must be the session's).

    A case workspace without a session identity is refused: no case data is
    served to a request that does not carry who is asking.

    Object routes (``operator_ui.object_workspace.ObjectWorkspace``, read-only, over the
    generic ontology tools) follow the same identity: live, ``ticket_sessions`` may
    answer ``object_workspace(session)`` with that employee's object workspace (None:
    no object routes for it); fixture, ``object_workspace`` is the fixture session's
    and needs the fixture ``case_workspace`` (same actor, same access token).

    ``journal`` (live only, ``operator_ui.session_journal.SessionJournal``) records every
    answered ``/api/*`` request after its answer, as counts and enums; a journal that
    cannot be written never fails the request.
    """
    from operating.session_identity import CHANNEL_CONSOLE, SessionIdentity

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("operator_console_requires_loopback")
    if ticket_sessions is not None and (case_workspace is not None or session is not None):
        raise ValueError("console_session_source_ambiguous")
    if ticket_sessions is not None and not callable(getattr(ticket_sessions, "authenticate",
                                                            None)):
        raise ValueError("console_ticket_sessions_invalid")
    if case_workspace is not None:
        if not isinstance(session, SessionIdentity) or session.channel != CHANNEL_CONSOLE:
            raise ValueError("console_case_workspace_requires_session_identity")
        if not session.fixture:
            # A live employee is authenticated per request (ticket_sessions), never
            # bound to a process-wide token.
            raise ValueError("console_live_session_requires_ticket_sessions")
        if getattr(case_workspace, "actor", None) != session.actor:
            raise ValueError("console_workspace_actor_must_be_the_session_identity")
    elif session is not None:
        raise ValueError("console_session_without_case_workspace")
    if journal is not None and (ticket_sessions is None
                                or not callable(getattr(journal, "record", None))):
        # A fixture session has no session reference to record a person's session by.
        raise ValueError("console_journal_requires_ticket_sessions")
    if object_workspace is not None:
        if ticket_sessions is not None:
            # Live object workspaces are per employee (ticket_sessions.object_workspace).
            raise ValueError("console_session_source_ambiguous")
        if case_workspace is None:
            raise ValueError("console_object_workspace_requires_session_identity")
        if getattr(object_workspace, "actor", None) != session.actor:
            raise ValueError("console_workspace_actor_must_be_the_session_identity")
    csrf_token = secrets.token_urlsafe(32)
    # A fragment is never sent in HTTP requests; only the local terminal prints
    # it. The browser removes it immediately and sends the secret as a header.
    # An object workspace needs the fixture case workspace, so it is behind the same token.
    access_token = secrets.token_urlsafe(48) if case_workspace is not None else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Avoid logging query strings or service data to a shared terminal.
            return

        def _allowed_host(self) -> bool:
            try:
                hostname = urlsplit("http://" + self.headers.get("Host", "")).hostname
            except ValueError:
                return False
            return hostname in {"127.0.0.1", "localhost", "::1"}

        def _authenticate(self) -> tuple[Any, Any] | None:
            """``(session, workspace)`` for this request, or None (401).

            Live: the bearer is a signed session ticket, verified and resolved now.
            Fixture: the bearer is the process's random access token. Neither
            configured: no case routes exist and nothing needs a credential.
            """
            header = self.headers.get("Authorization", "")
            bearer = header[7:] if header.startswith("Bearer ") else None
            if ticket_sessions is not None:
                if not bearer:
                    self._refusal = "session_ticket_required"
                    return None
                try:
                    auth = ticket_sessions.authenticate(bearer)
                    self._session = auth[0]
                    return auth
                except PermissionError as error:
                    self._refusal = str(getattr(error, "reason", "")) or "session_refused"
                    return None
            if access_token is None:
                return (None, None)
            if bearer is not None and secrets.compare_digest(bearer, access_token):
                return (session, case_workspace)
            self._refusal = "access_token_required"
            return None

        def _headers(self, status: int, content_type: str, size: int,
                     *, disposition: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            if disposition is not None:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()

        def _send(self, status: int, value: Any) -> None:
            self._answered = (status, value)
            data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":"), default=str).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(data))
            self.wfile.write(data)

        def _file(self, name: str, media_type: str) -> None:
            data = (HERE / name).read_bytes()
            self._headers(200, media_type, len(data))
            self.wfile.write(data)

        @staticmethod
        def _id(path: str, prefix: str) -> str | None:
            if not path.startswith(prefix):
                return None
            tail = path[len(prefix):]
            if not tail or "/" in tail or len(tail) > MAX_ID:
                return None
            value = unquote(tail)
            if not value or "/" in value or "\\" in value or len(value) > MAX_ID:
                return None
            return value

        def _error(self, error: Exception) -> None:
            if isinstance(error, KeyError):
                self._send(404, {"error": "not_found"})
            elif isinstance(error, PermissionError):
                self._send(403, {"error": "forbidden", "detail": str(error)})
            elif isinstance(error, ValueError):
                self._send(400, {"error": "invalid_request", "detail": str(error)})
            else:
                self._send(500, {"error": "service_error"})

        def _case_get(self, path: str, case_workspace: Any) -> None:
            """Read-only case routes; every read is made for the request session's actor."""
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if any(len(values) != 1 for values in params.values()):
                raise ValueError("invalid_case_query")
            if path == "/api/cases":
                if set(params) - {"status", "limit"}:
                    raise ValueError("invalid_case_query")
                if case_workspace is None:
                    self._send(200, {"available": False, "cases": []})
                    return
                status = params.get("status", [None])[0]
                raw_limit = params.get("limit", ["50"])[0]
                if status not in (None, "open", "closed"):
                    raise ValueError("case_status_open_or_closed_required")
                if (not raw_limit.isascii() or not raw_limit.isdecimal()
                        or not 1 <= int(raw_limit) <= 200):
                    raise ValueError("case_list_limit_1_to_200_required")
                self._send(200, case_workspace.list_cases(status=status, limit=int(raw_limit)))
                return
            if case_workspace is None:
                self._send(501, {"error": "capability_unavailable"})
                return
            if path == "/api/case-citation":
                if set(params) - {"handle", "text_offset"} or "handle" not in params:
                    raise ValueError("citation_handle_required")
                raw_offset = params.get("text_offset", ["0"])[0]
                if (not raw_offset.isascii() or not raw_offset.isdecimal()
                        or int(raw_offset) > 10_000_000):
                    raise ValueError("invalid_text_offset")
                self._send(200, case_workspace.citation(params["handle"][0],
                                                        text_offset=int(raw_offset)))
                return
            if path == "/api/case-document-metadata":
                # Proposals only, for one original the actor can read (same audience,
                # source gate and byte re-hash as every read); nothing is saved.
                if set(params) != {"handle"}:
                    raise ValueError("original_citation_required")
                self._send(200, case_workspace.document_metadata(params["handle"][0]))
                return
            if path == "/api/case-original-pdf":
                if set(params) != {"handle"}:
                    raise ValueError("original_file_citation_required")
                # The store checks audience, source gate and employee principal before
                # it reads a byte; refused and missing originals are both 404.
                pdf = case_workspace.original_pdf(params["handle"][0])
                body = pdf["content"]
                if not isinstance(body, bytes) or not body.startswith(b"%PDF-"):
                    raise ValueError("case_original_must_be_pdf")
                self._headers(200, "application/pdf", len(body),
                              disposition='inline; filename="case-original.pdf"')
                self.wfile.write(body)
                self._answered = (200, None)  # journaled like every answered request
                return
            case_id = self._id(path, "/api/case/")
            if case_id is None or len(case_id) > 64:
                self._send(404, {"error": "route_not_found"})
                return
            if set(params) - {"section", "offset", "limit"} or (
                    ("offset" in params or "limit" in params) and "section" not in params):
                raise ValueError("invalid_case_query")
            page: dict[str, Any] = {}
            if "section" in params:
                page["section"] = params["section"][0]
                for name in ("offset", "limit"):
                    if name not in params:
                        continue
                    raw = params[name][0]
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 9:
                        raise ValueError(f"invalid_case_{name}")
                    page[name] = int(raw)
            self._send(200, case_workspace.case(case_id, **page))
        def _object_get(self, path: str, object_workspace: Any) -> None:
            """Read-only object routes; every tool call is made for the request session's
            actor (``object_workspace`` is that session's, or None)."""
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if any(len(values) != 1 for values in params.values()):
                raise ValueError("invalid_object_query")
            one = {name: values[0] for name, values in params.items()}
            if path == "/api/objects":
                if set(one) - {"q", "type", "cursor"}:
                    raise ValueError("invalid_object_query")
                if object_workspace is None:
                    self._send(200, {"available": False})
                    return
                self._send(200, object_workspace.find(one.get("q"), object_type=one.get("type"),
                                                      cursor=one.get("cursor")))
                return
            if object_workspace is None:
                self._send(501, {"error": "capability_unavailable"})
                return
            if path == "/api/object":
                if set(one) != {"id"}:
                    raise ValueError("object_id_required")
                self._send(200, object_workspace.workspace(one["id"]))
                return
            if path == "/api/object-source":
                if set(one) - {"handle", "text_offset"} or "handle" not in one:
                    raise ValueError("l1_citation_handle_required")
                raw = one.get("text_offset", "0")
                if not raw.isascii() or not raw.isdecimal() or int(raw) > 10_000_000:
                    raise ValueError("invalid_text_offset")
                try:
                    self._send(200, object_workspace.source(one["handle"], text_offset=int(raw)))
                except LookupError as error:
                    if isinstance(error, KeyError):
                        raise
                    self._send(501, {"error": "source_reader_not_configured"})
                return
            if path == "/api/object-actions":
                if set(one) != {"id"}:
                    raise ValueError("object_id_required")
                try:
                    self._send(200, object_workspace.actions_for(one["id"]))
                except LookupError as error:
                    if isinstance(error, KeyError):
                        raise
                    self._send(501, {"error": "actions_not_served"})
                return
            if path == "/api/investigate":
                if set(one) != {"family", "root"}:
                    raise ValueError("investigation_family_and_root_required")
                try:
                    self._send(200, object_workspace.investigate(one["family"], one["root"]))
                except LookupError as error:
                    if isinstance(error, KeyError):
                        raise
                    self._send(501, {"error": "investigate_not_served"})
                return
            if path == "/api/status-card":
                if set(one) not in ({"id"}, {"type", "ref"}):
                    raise ValueError("status_card_id_or_type_and_ref_required")
                self._send(200, object_workspace.status_card(
                    one.get("id"), object_type=one.get("type"), reference=one.get("ref")))
                return
            if path == "/api/object-case":
                if set(one) != {"case_id"}:
                    raise ValueError("case_id_required")
                try:
                    self._send(200, object_workspace.case_records(one["case_id"]))
                except LookupError as error:
                    if isinstance(error, KeyError):
                        raise
                    self._send(501, {"error": str(error)})
                return
            self._send(404, {"error": "route_not_found"})

        def _objects(self, request_session: Any) -> Any:
            """The request session's object workspace (None: not configured)."""
            if ticket_sessions is None:
                return object_workspace
            per_employee = getattr(ticket_sessions, "object_workspace", None)
            return (per_employee(request_session)
                    if per_employee is not None and request_session is not None else None)

        def _object_post(self, path: str, objects: Any, payload: dict[str, Any]) -> None:
            if objects is None:
                self._send(501, {"error": "capability_unavailable"})
                return
            try:
                if path == "/api/object-review":
                    result = objects.review(payload)
                    refusal = result["review"].get("error")
                    if refusal:
                        self._send(409, {"error": refusal.get("code"),
                                         "detail": refusal.get("message"),
                                         "review": result["review"]})
                        return
                elif path == "/api/link-confirm":
                    result = objects.link_confirm(payload)
                    answer = result["link_confirm"]
                    if answer.get("status") == "refused":
                        self._send(409, {"error": answer["code"], "detail": answer["says"],
                                         "link_confirm": answer})
                        return
                elif path == "/api/object-ask":
                    result = objects.ask(payload)
                elif path == "/api/objective":
                    result = objective_box.answer(objects, payload)
                elif path == "/api/object-replay":
                    result = objects.replay(payload)
                elif path == "/api/investigate-record":
                    result = objects.record_on_case(payload)
                elif path == "/api/object-action":
                    result = objects.act(payload)
                    if result["refusal"]:
                        # The action path refused the step: nothing was sent for it.
                        self._send(409, {"error": result["refusal"], "detail": result["refusal"],
                                         "action": result})
                        return
                else:
                    result = objects.follow_up(payload)
            except LookupError as error:
                if isinstance(error, KeyError):
                    raise
                self._send(501, {"error": str(error)})
                return
            self._send(200, result)

        def _journaled(self, method: str, handle: Any) -> None:
            path = urlsplit(self.path).path
            self._answered = self._session = None
            self._refusal = ""
            try:
                handle()
            finally:
                if journal is not None and path.startswith("/api/") \
                        and self._answered is not None:
                    status, value = self._answered
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        journal.record(method=method, path=path, status=status,
                                       session=self._session,
                                       refusal=(self._refusal or None)
                                       if status == 401 else None,
                                       object_id=(query.get("id") or [None])[0]
                                       if method == "GET" else None, value=value)
                    except (OSError, sqlite3.Error) as error:
                        print(f"console journal: write failed ({type(error).__name__})",
                              file=sys.stderr, flush=True)

        def do_GET(self) -> None:
            self._journaled("GET", self._get)

        def do_POST(self) -> None:
            self._journaled("POST", self._post)

        def _get(self) -> None:
            if not self._allowed_host():
                self._send(403, {"error": "invalid_host"})
                return
            path = urlsplit(self.path).path
            if path in STATIC:
                self._file(*STATIC[path])
                return
            if not path.startswith("/api/"):
                self._send(404, {"error": "route_not_found"})
                return
            auth = self._authenticate()
            if auth is None:
                self._send(401, {"error": "access_token_required",
                                 "detail": getattr(self, "_refusal", "")})
                return
            request_session, workspace = auth
            if path == "/api/session":
                self._send(200, {"csrf_token": csrf_token, "mode": "local-isolated",
                                 "case_workspace": workspace is not None,
                                 "identity": None if request_session is None
                                 else request_session.describe(),
                                 # The audience this session reads as, for the top bar's role.
                                 "principals": None if request_session is None
                                 else sorted(request_session.principals)})
                return
            try:
                if path == "/api/home":
                    if urlsplit(self.path).query:
                        raise ValueError("invalid_home_query")
                    self._send(200, home(workspace, self._objects(request_session)))
                    return
                if path == "/api/cases" or path.startswith(("/api/case/", "/api/case-citation",
                                                            "/api/case-original-pdf",
                                                            "/api/case-document-metadata")):
                    self._case_get(path, workspace)
                    return
                if path in OBJECT_ROUTES:
                    self._object_get(path, self._objects(request_session))
                    return
                self._send(404, {"error": "route_not_found"})
            except Exception as error:
                self._error(error)

        def _post(self) -> None:
            if not self._allowed_host():
                self._send(403, {"error": "invalid_host"})
                return
            path = urlsplit(self.path).path
            auth = self._authenticate() if path.startswith("/api/") else None
            if path.startswith("/api/") and auth is None:
                self._send(401, {"error": "access_token_required",
                                 "detail": getattr(self, "_refusal", "")})
                return
            if path not in OBJECT_POST_ROUTES:
                self._send(404, {"error": "route_not_found"})
                return
            if self.headers.get("X-Operator-Token") != csrf_token:
                self._send(403, {"error": "csrf_token_required"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send(415, {"error": "json_required"})
                return
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"error": "content_length_required"})
                return
            if size < 0 or size > MAX_BODY:
                self._send(413, {"error": "body_too_large"})
                return
            try:
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("json_object_required")
                # The author is the request's session, never a body field.
                self._object_post(path, self._objects(auth[0]), payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": "invalid_json"})
            except Exception as error:
                self._error(error)

    # The local ontology owns one SQLite connection. A single request thread
    # preserves its thread affinity until a production connection pool exists.
    server = HTTPServer((host, port), Handler)
    server.operator_access_token = access_token
    return server


def serve(host: str = "127.0.0.1", port: int = 8765, *,
          case_workspace: Any = None, session: Any = None,
          ticket_sessions: TicketSessions | None = None, object_workspace: Any = None,
          journal: Any = None) -> None:
    """Serve until interrupted.

    A live console prints no credential: the employee's browser presents its own
    signed session ticket (``#access=<ticket>`` in the URL it opens)."""
    server = make_server(host, port, case_workspace=case_workspace, session=session,
                         ticket_sessions=ticket_sessions, object_workspace=object_workspace,
                         journal=journal)
    try:
        url = f"http://{host}:{server.server_address[1]}/"
        if server.operator_access_token is not None:
            url += f"#access={server.operator_access_token}"
        elif ticket_sessions is not None:
            url += "#access=<your session ticket>"
        print(f"Operator console: {url}", flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
