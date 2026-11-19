"""The app API's contract at its HTTP boundary: auth on every route, bind refusals, the
SSE ask payload, plain errors, and the mock payloads against the committed openapi.json."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import time
import unittest
from unittest import mock
from datetime import date

from fastapi.testclient import TestClient

from operator_api import models as m
from operator_api.__main__ import OPENAPI_FILE, openapi_text
from operator_api.app import PUBLIC_ROUTES, create_app
from operator_api.auth import SESSION_COOKIE, DevAuth, check_bind, fixture_identity
from operator_api.mock import MockBackend

SPEC = json.loads(Path(OPENAPI_FILE).read_text("utf-8"))


def mock_client(**kwargs) -> TestClient:
    app = create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:demo"]),
                                            mode="mock"), secure_cookie=False, **kwargs)
    return TestClient(app, base_url="http://testserver")


def signed_in(**kwargs) -> TestClient:
    client = mock_client(**kwargs)
    response = client.post("/api/auth/dev-login", json={})
    assert response.status_code == 200, response.text
    return client


def events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def conforms(value, schema, path="$"):
    """A small JSON-schema check over the committed document: $ref, anyOf, object
    (required, no unknown property), array, string/integer/number/boolean/null, enum."""
    if "$ref" in schema:
        return conforms(value, SPEC["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]],
                        path)
    if "anyOf" in schema:
        errors = [conforms(value, option, path) for option in schema["anyOf"]]
        return [] if any(not e for e in errors) else errors[0]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not in enum"]
    if "const" in schema and value != schema["const"]:
        return [f"{path}: {value!r} != const"]
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{path}: not an object"]
        errors = [f"{path}: missing {key}" for key in schema.get("required", [])
                  if key not in value]
        props = schema.get("properties")
        extra = schema.get("additionalProperties")
        for key, item in value.items():
            if props is not None and key in props:
                errors += conforms(item, props[key], f"{path}.{key}")
            elif isinstance(extra, dict):
                errors += conforms(item, extra, f"{path}.{key}")
            elif props is not None:
                errors.append(f"{path}: unknown property {key}")
        return errors
    if kind == "array":
        if not isinstance(value, list):
            return [f"{path}: not an array"]
        return [e for i, item in enumerate(value)
                for e in conforms(item, schema.get("items", {}), f"{path}[{i}]")]
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
             "null": type(None)}
    if kind in types and (not isinstance(value, types[kind])
                          or (kind in ("integer", "number") and isinstance(value, bool))):
        return [f"{path}: not {kind}"]
    return []


def component(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


class AuthEveryRoute(unittest.TestCase):
    def test_every_route_but_the_public_ones_needs_a_reader(self):
        client = mock_client()
        checked = 0
        for route in client.app.routes:
            for method in sorted(getattr(route, "methods", None) or ()):
                if method == "HEAD" or not route.path.startswith("/api/"):
                    continue
                if (method, route.path) in PUBLIC_ROUTES:
                    continue
                path = route.path.replace("{object_type}", "PurchaseOrder") \
                    .replace("{object_id:path}", "90412")
                response = client.request(method, path, params={"q": "po", "handle": "x://y"},
                                          json={"question": "q", "answer_id": "a",
                                                "rating": 1})
                self.assertEqual(response.status_code, 401, (method, path, response.text))
                self.assertEqual(response.json()["error"]["code"], "sign_in_required")
                checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_a_forged_or_ended_session_is_refused(self):
        client = signed_in()
        self.assertEqual(client.get("/api/me").status_code, 200)
        client.cookies.set(SESSION_COOKIE, "forged-value")
        self.assertEqual(client.get("/api/me").status_code, 401)
        client = signed_in()
        client.post("/api/auth/logout")
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_dev_login_is_not_served_by_an_entra_server(self):
        class Entra:
            mode = "entra"

            def authenticate(self, headers, cookies):
                from operator_api.auth import AuthRequired
                raise AuthRequired("sign_in_required")

            def config(self):
                return {"mode": "entra", "dev_login": False}

        client = TestClient(create_app(MockBackend(), Entra()))
        response = client.post("/api/auth/dev-login", json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(client.get("/api/me").headers.get("www-authenticate"),
                         'Bearer realm="plx-workbench"')

    def test_a_foreign_origin_cannot_post(self):
        client = signed_in()
        response = client.post("/api/ask", json={"question": "late?"},
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "origin_refused")
        same = client.post("/api/ask", json={"question": "late?"},
                           headers={"Origin": "http://testserver"})
        self.assertEqual(same.status_code, 200)

    def test_the_apps_origin_posts_through_the_proxy_in_front(self):
        # next start's /api rewrite: Host is the API's loopback address, the browser's host
        # arrives as X-Forwarded-Host (Caddy keeps Host and sets the same header).
        client = signed_in()
        proxied = {"Host": "127.0.0.1:8796", "X-Forwarded-Host": "127.0.0.1:3097"}
        ok = client.post("/api/ask", json={"question": "late?"},
                         headers={**proxied, "Origin": "http://127.0.0.1:3097"})
        self.assertEqual(ok.status_code, 200)
        caddy = {"Host": "plx.example.ts.net", "X-Forwarded-Host": "plx.example.ts.net"}
        self.assertEqual(client.post("/api/ask", json={"question": "late?"}, headers={
            **caddy, "Origin": "https://plx.example.ts.net"}).status_code, 200)
        for origin, headers in (
                ("https://evil.example", proxied),                       # foreign page
                ("http://127.0.0.1:8796", proxied),                      # Host, not the app
                ("http://127.0.0.1:3097", {"Host": "127.0.0.1:8796"}),   # no proxy header
                ("http://127.0.0.1:3097", {"Host": "127.0.0.1:8796",     # several hosts
                                           "X-Forwarded-Host": "127.0.0.1:3097, evil"}),
                ("null", proxied)):
            refused = client.post("/api/ask", json={"question": "late?"},
                                  headers={**headers, "Origin": origin})
            self.assertEqual(refused.status_code, 403, (origin, headers))
            self.assertEqual(refused.json()["error"]["code"], "origin_refused")


class AccessLink(unittest.TestCase):
    """The access-link sign-in (#ticket=): a refused link says so, an ended one signs out."""

    def test_a_link_signs_in_and_a_refused_one_says_why(self):
        from operating.session_identity import SessionIdentityRefused
        from operator_api.server import mock_ticket
        fixture = fixture_identity("plx", ["staff:demo"])
        check = mock_ticket("good-ticket-0123456789", fixture)
        state = {"expired": False, "denied": False}

        def identity(ticket):
            if state["expired"]:
                raise SessionIdentityRefused("session_ticket_expired")
            if state["denied"]:
                raise SessionIdentityRefused("employee_not_active")
            return check(ticket)

        client = TestClient(create_app(MockBackend(), DevAuth(ticket_identity=identity, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        proxied = {"Host": "127.0.0.1:8796", "X-Forwarded-Host": "127.0.0.1:3097",
                   "Origin": "http://127.0.0.1:3097"}
        wrong = client.post("/api/auth/dev-login", json={"ticket": "not-the-ticket"}, headers=proxied)
        self.assertEqual((wrong.status_code, wrong.json()["error"]["code"]), (403, "access_link_refused"))
        self.assertEqual(client.post("/api/auth/dev-login", json={}, headers=proxied).json()[
            "error"]["code"], "dev_ticket_required")
        ok = client.post("/api/auth/dev-login", json={"ticket": "good-ticket-0123456789"},
                         headers=proxied)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertIn("HttpOnly", ok.headers["set-cookie"])
        self.assertIn("samesite=strict", ok.headers["set-cookie"].lower())
        self.assertEqual(client.get("/api/me").status_code, 200)
        # the ticket ends: the session has ended (401, sign in again), not "no access"
        state["expired"] = True
        ended = client.get("/api/me")
        self.assertEqual((ended.status_code, ended.json()["error"]["code"]), (401, "sign_in_required"))
        # a refusal that is the person's access stays "no Workbench access"
        state.update(expired=False, denied=True)
        denied = client.post("/api/auth/dev-login", json={"ticket": "good-ticket-0123456789"},
                             headers=proxied)
        self.assertEqual((denied.status_code, denied.json()["error"]["code"]), (403, "no_workbench_access"))


    def test_the_sign_in_kind_says_access_link_only_for_a_live_ticket(self):
        """AUDIT4-03: the app says "Demo sign-in" only for mock/fixture or a configured test
        identity; a console session ticket over live state is the access link."""
        import dataclasses
        from operating.session_identity import PROOF_TICKET
        from operator_api.auth import sign_in_kind
        from operator_api.server import mock_ticket
        fixture = fixture_identity("plx", ["staff:demo"])
        proxied = {"Host": "127.0.0.1:8796", "X-Forwarded-Host": "127.0.0.1:3097",
                   "Origin": "http://127.0.0.1:3097"}
        from operating.employee_permissions import EmployeePrincipal
        person = EmployeePrincipal("00000000-0000-4000-8000-000000000001",
                                   "00000000-0000-4000-8000-000000000002")
        signed = lambda: dataclasses.replace(fixture(), proof=PROOF_TICKET, employee=person,
                                             principals=frozenset({person.principal}),
                                             expires_at=4102444800,
                                             identities=frozenset({person.object_id}))
        for mode, identity, kind in (("dev", mock_ticket("good-ticket-0123456789", signed), "access_link"),
                                     ("mock", mock_ticket("good-ticket-0123456789", fixture), "demo")):
            client = TestClient(create_app(MockBackend(), DevAuth(ticket_identity=identity, mode=mode),
                                           secure_cookie=False), base_url="http://testserver")
            self.assertEqual(client.get("/api/auth/config").json()["sign_in"],
                             "access_link" if mode == "dev" else "demo")
            ok = client.post("/api/auth/dev-login", json={"ticket": "good-ticket-0123456789"},
                             headers=proxied)
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(client.get("/api/me").json()["sign_in"], kind, mode)
        # a configured test identity (no ticket) stays demo; an unknown proof is never a sign-in
        client = TestClient(create_app(MockBackend(), DevAuth(identity=fixture, mode="dev"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.get("/api/auth/config").json()["sign_in"], "demo")
        self.assertEqual(sign_in_kind("dev", "fixture"), "demo")
        self.assertEqual(sign_in_kind("mock", PROOF_TICKET), "demo")
        self.assertEqual(sign_in_kind("entra", "something_new"), "demo")
        self.assertEqual(sign_in_kind("entra", "entra_access_token"), "microsoft")


class SessionLength(unittest.TestCase):
    """A demo session lasts a working day of use: 8 h idle, sliding, never past 24 h."""

    def test_slides_on_activity_and_ends_when_idle_or_at_the_cap(self):
        from operator_api import auth as a
        now = [1000.0]
        dev = DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock", clock=lambda: now[0])
        token, _ = dev.login()
        hour = 3600.0
        for _ in range(5):               # active every 7 h: well past the first 8 h window
            now[0] += 7 * hour
            if now[0] - 1000.0 >= a.DEV_SESSION_MAX_SECONDS:
                break
            self.assertTrue(dev._held(token)[0])
        now[0] = 1000.0 + a.DEV_SESSION_MAX_SECONDS + 1
        self.assertFalse(dev._held(token)[0], "never past a day from sign-in")
        token, _ = dev.login()
        now[0] += 8 * hour + 1
        self.assertFalse(dev._held(token)[0], "idle for 8 h ends the session")
        self.assertFalse(dev._held("not-a-token")[0])


class BindRefusals(unittest.TestCase):
    def test_dev_and_mock_bind_loopback_or_tailscale_only(self):
        for mode in ("dev", "mock"):
            for ok in ("127.0.0.1", "::1", "localhost", "100.101.102.103",
                       "fd7a:115c:a1e0::1"):
                check_bind(ok, mode)
            for bad in ("0.0.0.0", "::", "8.8.8.8", "192.168.1.10", "10.0.0.5",
                        "example.com", ""):
                with self.assertRaises(ValueError, msg=(mode, bad)):
                    check_bind(bad, mode)

    def test_entra_binds_loopback_only(self):
        check_bind("127.0.0.1", "entra")
        for bad in ("100.101.102.103", "0.0.0.0"):
            with self.assertRaises(ValueError):
                check_bind(bad, "entra")

    def test_the_server_refuses_a_public_bind_at_start(self):
        from operator_api.server import main
        for argv in (["--mock", "--bind", "0.0.0.0"], ["--dev", "--bind", "203.0.113.9"]):
            with self.assertRaises(SystemExit) as stopped, \
                    contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(stopped.exception.code, 2)

    def test_no_mode_is_no_server(self):
        from operator_api.server import main
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["--bind", "127.0.0.1"])


class AskStream(unittest.TestCase):
    def test_ask_streams_status_then_tokens_then_one_answer(self):
        client = signed_in()
        response = client.post("/api/ask", json={
            "question": "why is the soap order from that canadian guy late"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        seen = events(response.text)
        names = [name for name, _ in seen]
        self.assertEqual(names[0], "status")
        self.assertEqual(names[-1], "answer")
        self.assertEqual(names.count("answer"), 1)
        self.assertIn("token", names)
        for name, data in seen:
            m.ASK_EVENTS[name].model_validate(data)
            self.assertEqual(conforms(data, component(m.ASK_EVENTS[name].__name__)), [], name)
        answer = seen[-1][1]
        tokens = "".join(data["text"] for name, data in seen if name == "token")
        self.assertEqual(tokens, answer["direct_answer"])
        self.assertTrue(answer["facts"] and all(f["sources"] for f in answer["facts"]))
        self.assertTrue(answer["conflicts"][0]["values"][0]["trusted"])
        self.assertEqual(sum(1 for v in answer["conflicts"][0]["values"] if v["trusted"]), 1)
        # what to ask next: a few questions about the answer's own record, never the question
        self.assertTrue(1 <= len(answer["follow_ups"]) <= 3, answer["follow_ups"])
        self.assertNotIn(answer["question"], answer["follow_ups"])
        self.assertTrue(all(q.endswith("?") for q in answer["follow_ups"]))

    def test_hidden_items_are_counts_by_owner_only(self):
        client = signed_in()
        answer = events(client.post("/api/ask", json={"question": "is PO 90412 late"}).text)[-1][1]
        hidden = answer["hidden"]
        self.assertGreater(hidden["count"], 0)
        self.assertEqual(set(hidden), {"count", "by_owner", "message"})
        for owner in hidden["by_owner"]:
            self.assertEqual(set(owner), {"owner_or_team", "count", "named"})
        self.assertEqual(sum(o["count"] for o in hidden["by_owner"]), hidden["count"])
        self.assertIn("don't have access", hidden["message"])

    def test_a_bad_question_is_a_plain_422(self):
        client = signed_in()
        for body in ({"question": ""}, {"question": "x" * 1001}, {"question": "q", "x": 1}):
            response = client.post("/api/ask", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(set(response.json()), {"error"})
            self.assertNotIn("loc", response.text)

    def test_a_backend_failure_is_plain_words_never_a_trace(self):
        class Broken(MockBackend):
            def ask(self, reader, request):
                yield "status", m.StatusEvent(step="understanding", message="Reading",
                                              elapsed_ms=0)
                raise RuntimeError("secret path /data/plx-state/x.sqlite exploded")

            def home(self, reader):
                raise RuntimeError("secret path /data/plx-state/x.sqlite exploded")

        client = TestClient(create_app(Broken(), DevAuth(
            fixture_identity("plx", ["staff:demo"]), mode="mock"), secure_cookie=False),
            raise_server_exceptions=False)
        client.post("/api/auth/dev-login", json={})
        streamed = events(client.post("/api/ask", json={"question": "late?"}).text)
        self.assertEqual(streamed[-1][0], "error")
        home = client.get("/api/home")
        self.assertEqual(home.status_code, 500)
        for text in (json.dumps(streamed), home.text):
            self.assertNotIn("plx-state", text)
            self.assertNotIn("Traceback", text)
            self.assertNotIn("RuntimeError", text)
        self.assertIn("incident", home.json()["error"]["message"])

    def test_a_keyerror_in_a_record_read_is_an_incident_not_a_missing_record(self):
        """serve-1115d V-19: every record route answers 404 only for ``RecordNotFound``; a
        plain KeyError (a bug) out of the read is a 500 incident, nothing of it shown."""
        from operator_api.errors import RecordNotFound

        class Broken(MockBackend):
            error: Exception = KeyError("secret_key_name")

        for name in ("object_page", "explore", "record_history", "record_as_of",
                     "object_section", "object_impact"):
            setattr(Broken, name, lambda self, *a, **k: (_ for _ in ()).throw(self.error))
        backend = Broken()
        client = TestClient(create_app(backend, DevAuth(
            fixture_identity("plx", ["staff:demo"]), mode="mock"), secure_cookie=False),
            raise_server_exceptions=False)
        client.post("/api/auth/dev-login", json={})
        paths = ("/api/object/PurchaseOrder/90412", "/api/explore/PurchaseOrder/90412",
                 "/api/history/PurchaseOrder/90412",
                 "/api/as-of/PurchaseOrder/90412?at=2026-10-01",
                 "/api/object-section/late/PurchaseOrder/90412",
                 "/api/object-impact/why/PurchaseOrder/90412")
        for error, status in ((KeyError("secret_key_name"), 500),
                              (LookupError("secret_key_name"), 500),
                              (RecordNotFound(), 404)):
            backend.error = error
            for path in paths:
                with contextlib.redirect_stderr(io.StringIO()):
                    got = client.get(path)
                self.assertEqual(got.status_code, status, (path, error))
                self.assertEqual(got.json()["error"]["code"],
                                 "internal" if status == 500 else "not_found", path)
                self.assertNotIn("secret_key_name", got.text)
                self.assertNotIn("KeyError", got.text)

    def test_source_and_feedback_routes_answer_404_only_for_a_not_found(self):
        """followups-1115: /api/source, /api/source/records, /api/feedback and the feedback
        status route caught any KeyError (or LookupError) as "not found", so a bug inside
        the read was a missing source or answer. Only ``RecordNotFound`` (and, on the
        source routes, a refusal that names no source: a malformed handle, a family no
        host serves here) is a 404 now; anything else is a logged 500, nothing of it
        shown."""
        from unittest import mock

        from operator_api.errors import RecordNotFound

        class Store:
            def set_status(self, fid, status, by):
                raise Broken.error

        class Broken(MockBackend):
            error: Exception = KeyError("secret_key_name")
            feedback_store = Store()

        def fail(self, *args, **kwargs):
            raise self.error

        for name in ("source", "source_records", "answer_context"):
            setattr(Broken, name, fail)
        backend = Broken()
        client = TestClient(create_app(backend, DevAuth(
            fixture_identity("plx", ["staff:demo"]), mode="mock"), secure_cookie=False),
            raise_server_exceptions=False)
        client.post("/api/auth/dev-login", json={})
        calls = (("GET", "/api/source", {"params": {"handle": "bc://demo/x/1"}}),
                 ("GET", "/api/source/records", {"params": {"handle": "bc://demo/x/1"}}),
                 ("POST", "/api/feedback", {"json": {"answer_id": "a1", "rating": 1}}),
                 ("POST", "/api/feedback/f1/status", {"json": {"status": "fixed"}}))
        sources = calls[:2]
        cases = [(KeyError("secret_key_name"), 500, calls),
                 (LookupError("secret_key_name"), 500, calls),
                 (ValueError("secret_key_name"), 500, sources),
                 (RuntimeError("secret_key_name"), 500, calls),
                 (RecordNotFound("answer_not_found"), 404, calls),
                 (ValueError("l1_citation_handle_required"), 404, sources),
                 (ValueError("citation_scheme_not_granted: no host grant"), 404, sources),
                 (LookupError("source_reader_not_configured"), 404, sources),
                 # a plain KeyError named like a refusal is still a bug's
                 (KeyError("citation_unresolvable_for_actor"), 500, sources)]
        with mock.patch("operator_api.feedback.is_admin", return_value=True):
            for error, status, which in cases:
                Broken.error = error
                for method, path, kw in which:
                    with contextlib.redirect_stderr(io.StringIO()):
                        got = client.request(method, path, **kw)
                    self.assertEqual(got.status_code, status, (path, repr(error)))
                    self.assertEqual(got.json()["error"]["code"],
                                     "internal" if status == 500 else "not_found", path)
                    self.assertNotIn("secret_key_name", got.text)
                    self.assertNotIn("KeyError", got.text)

    def test_a_malformed_record_id_is_not_found_and_a_cut_read_timed_out(self):
        """harden-1114: a record id malformed for its type raised ValueError
        ("object_id_invalid") out of the page read: a 500 incident in the 11-13 log. It
        reads as not found (as an unseen record does); a store read stopped at its time
        limit ("interrupted") as a timeout (504), not "the server failed"."""
        import sqlite3

        class Odd(MockBackend):
            def object_page(self, reader, object_type, object_id, *, quick=False):
                raise ValueError("object_id_invalid")

            def home(self, reader):
                raise sqlite3.OperationalError("interrupted")

        client = TestClient(create_app(Odd(), DevAuth(
            fixture_identity("plx", ["staff:demo"]), mode="mock"), secure_cookie=False),
            raise_server_exceptions=False)
        client.post("/api/auth/dev-login", json={})
        page = client.get("/api/object/RelSageArStcuso/RelSageArStcuso:x%7CTAKE%20%20%20")
        self.assertEqual((page.status_code, page.json()["error"]["code"]), (404, "not_found"))
        home = client.get("/api/home")
        self.assertEqual((home.status_code, home.json()["error"]["code"]), (504, "timeout"))
        self.assertNotIn("incident", home.text)
        # a read that waited past the API role's lock_timeout (behind a schema change)
        Odd.home = lambda self, reader: (_ for _ in ()).throw(sqlite3.OperationalError(
            "postgres: LockNotAvailable: canceling statement due to lock timeout"))
        home = client.get("/api/home")
        self.assertEqual((home.status_code, home.json()["error"]["code"]), (504, "timeout"))

    def test_postgres_refusing_connections_is_busy_not_a_server_failure(self):
        """11-18 3:46-4:12 PM ET Postgres refused connections at its 200-client limit: a
        page then reads "busy, try again" (503, Retry-After), never a 500 incident; any
        other database error is still an incident (not hidden as busy)."""
        import psycopg
        import sqlite3
        refused = psycopg.OperationalError(
            'connection failed: connection to server on socket "/var/run/postgresql/'
            '.s.PGSQL.5432" failed: FATAL:  sorry, too many clients already')
        wrapped = sqlite3.OperationalError("postgres: reconnect failed: OperationalError")
        wrapped.__cause__ = refused
        cases = [refused, wrapped,
                 sqlite3.OperationalError("postgres: OperationalError: connection failed: "
                                          "FATAL:  sorry, too many clients already")]
        for error in cases:
            class Odd(MockBackend):
                def home(self, reader, error=error):
                    raise error
            client = TestClient(create_app(Odd(), DevAuth(
                fixture_identity("plx", ["staff:demo"]), mode="mock"), secure_cookie=False),
                raise_server_exceptions=False)
            client.post("/api/auth/dev-login", json={})
            with contextlib.redirect_stderr(io.StringIO()):
                home = client.get("/api/home")
            self.assertEqual((home.status_code, home.json()["error"]["code"]), (503, "busy"),
                             repr(error))
            self.assertEqual(home.headers.get("Retry-After"), "5")
            self.assertNotIn("incident", home.text)
            self.assertNotIn("too many clients", home.text)

        # any other failure, a connection one too (wrong password, server down, the
        # store's reconnect refused for it): an incident, logged, never hidden as busy
        password = psycopg.OperationalError(
            'connection failed: connection to server on socket "/var/run/postgresql/'
            '.s.PGSQL.5432" failed: FATAL:  password authentication failed for user "x"')
        down = sqlite3.OperationalError("postgres: reconnect failed: OperationalError")
        down.__cause__ = psycopg.OperationalError(
            "connection failed: connection to server at \"10.0.0.9\", port 5432 failed: "
            "Connection refused")
        for error in (psycopg.OperationalError("division by zero"), password, down):
            class Broken(MockBackend):
                def home(self, reader, error=error):
                    raise error
            client = TestClient(create_app(Broken(), DevAuth(
                fixture_identity("plx", ["staff:demo"]), mode="mock"), secure_cookie=False),
                raise_server_exceptions=False)
            client.post("/api/auth/dev-login", json={})
            with self.assertLogs(level="ERROR"), contextlib.redirect_stderr(io.StringIO()):
                home = client.get("/api/home")
            self.assertEqual((home.status_code, home.json()["error"]["code"]),
                             (500, "internal"), repr(error))
            self.assertIn("incident", home.text)


class AskBodySize(unittest.TestCase):
    """Up to ten attachments of 11.2 MB each were parsed into memory
    before the 5-file / 10 MB limit applied. Over the limit is refused at the HTTP layer,
    before parsing; more than five files, or more than 10 MB together, is refused too."""

    def setUp(self):
        self.client = signed_in()
        self.asked = []
        backend = self.client.app.state.backend
        original = backend.ask
        backend.ask = lambda who, body: self.asked.append(body) or original(who, body)

    def post(self, files):
        return self.client.post("/api/ask", json={"question": "what is in this file?",
                                                  "attachments": files})

    def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        big = [{"name": f"f{i}.pdf", "data": "A" * 3_000_000} for i in range(5)]
        got = self.post(big)
        self.assertEqual(got.status_code, 413, got.text)
        self.assertEqual(got.json()["error"]["code"], "request_too_large")
        # streamed with no stated length: refused once it passes the limit
        import asyncio
        from operator_api.app import BodyLimit
        reached, sent = [], []

        async def app(scope, receive, send):
            reached.append(scope)

        async def receive():
            return {"type": "http.request", "body": b"x" * 1000, "more_body": True}

        async def send(message):
            sent.append(message)
        asyncio.run(BodyLimit(app, limit=1, limits={"/api/ask": 5000})(
            {"type": "http", "path": "/api/ask", "method": "POST", "headers": []},
            receive, send))
        self.assertEqual((reached, sent[0]["status"]), ([], 413))
        self.assertEqual(self.asked, [])

    def test_every_other_body_is_held_to_a_megabyte_before_parsing_signed_in_or_not(self):
        """SEC-007: the unauthenticated dev sign-in and every other route refuse a body
        over ``BODY_MAX`` (413) before parsing it; a question keeps its larger limit."""
        from fastapi.testclient import TestClient
        from operator_api.app import BODY_MAX
        anonymous = TestClient(self.client.app, base_url="http://testserver")
        over = "x" * (BODY_MAX + 1)
        for client, path in ((anonymous, "/api/auth/dev-login"),
                             (self.client, "/api/feedback/note")):
            with self.subTest(path=path):
                got = client.post(path, content=over,
                                  headers={"Content-Type": "application/json"})
                self.assertEqual(got.status_code, 413, got.text)
                self.assertEqual(got.json()["error"]["code"], "request_too_large")
        # under the limit the route answers as before; a question above a megabyte passes
        self.assertNotEqual(self.client.post("/api/feedback/note",
                                             json={"note": "fine"}).status_code, 413)
        files = [{"name": "f.pdf", "data": "A" * (BODY_MAX + 10)}]
        self.assertNotEqual(self.post(files).status_code, 413)

    def test_more_than_five_files_or_ten_megabytes_is_refused(self):
        six = [{"name": f"f{i}.txt", "data": "aGVsbG8="} for i in range(6)]
        self.assertEqual(self.post(six).status_code, 422)
        heavy = [{"name": f"f{i}.pdf", "data": "A" * 2_850_000} for i in range(5)]
        self.assertEqual(self.post(heavy).status_code, 422)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.post(six[:5]).status_code, 200)
        self.assertEqual(len(self.asked), 1)


class MockMatchesOpenapi(unittest.TestCase):
    def test_committed_openapi_is_the_generated_one(self):
        self.assertEqual(Path(OPENAPI_FILE).read_text("utf-8"), openapi_text(),
                         "run: python3 -m operator_api openapi --write")

    def test_every_mock_payload_conforms_to_openapi(self):
        client = signed_in()
        ask = events(client.post("/api/ask", json={"question": "landed cost 90412"}).text)
        answer_id = ask[-1][1]["answer_id"]
        checks = [
            ("GET", "/api/auth/config", None, None, "AuthConfig"),
            ("GET", "/api/me", None, None, "Me"),
            ("GET", "/api/suggest", {"q": "mapel"}, None, "SuggestResponse"),
            ("GET", "/api/home", None, None, "Home"),
            ("GET", "/api/object/PurchaseOrder/PurchaseOrder%3Ademo%3A90412", None, None,
             "ObjectPage"),
            ("GET", "/api/object/PurchaseOrder/90412", None, None, "ObjectPage"),
            ("GET", "/api/explore/PurchaseOrder/90412", None, None, "Explore"),
            ("GET", "/api/history/PurchaseOrder/90412", None, None, "RecordHistory"),
            ("GET", "/api/as-of/PurchaseOrder/90412", {"at": "2026-10-01"}, None,
             "RecordAsOf"),
            *(("GET", f"/api/object-section/{name}/PurchaseOrder/90412", None, None,
               "ObjectSection") for name in m.PAGE_SECTIONS),
            ("GET", "/api/source", {"handle": "bc://demo/purchaseOrders/90412"}, None,
             "SourceView"),
            ("POST", "/api/feedback", None, {"answer_id": answer_id, "rating": -1,
                                             "note": "wrong supplier"}, "FeedbackResponse"),
        ]
        for method, path, params, body, name in checks:
            response = client.request(method, path, params=params, json=body)
            self.assertEqual(response.status_code, 200, (path, response.text))
            self.assertEqual(conforms(response.json(), component(name)), [], path)
            route = path if path in SPEC["paths"] else \
                "/api/explore/{object_type}/{object_id}" if path.startswith("/api/explore/") \
                else "/api/history/{object_type}/{object_id}" \
                if path.startswith("/api/history/") \
                else "/api/as-of/{object_type}/{object_id}" if path.startswith("/api/as-of/") \
                else \
                "/api/object-section/{section}/{object_type}/{object_id}" \
                if path.startswith("/api/object-section/") \
                else "/api/object/{object_type}/{object_id}"
            declared = SPEC["paths"][route][method.lower()]["responses"]["200"]
            self.assertEqual(declared["content"]["application/json"]["schema"],
                             component(name), path)
        self.assertEqual(set(SPEC["paths"]["/api/ask"]["post"]["responses"]["200"]["content"]
                             ["text/event-stream"]["schema"]["x-sse-events"]),
                         set(m.ASK_EVENTS))

    def test_suggest_tolerates_typos(self):
        client = signed_in()
        labels = [s["label"] for s in client.get("/api/suggest",
                                                  params={"q": "maple lef"}).json()["suggestions"]]
        self.assertIn("Maple Leaf Soapworks", labels)

    def test_feedback_on_someone_elses_or_no_answer_is_not_found(self):
        client = signed_in()
        response = client.post("/api/feedback", json={"answer_id": "nope", "rating": 1})
        self.assertEqual(response.status_code, 404)


class CardActionsRoutes(unittest.TestCase):
    """The action routes over the mock's real composer: the card is the reader's own, a
    confirm is a dry run while sending is off, a double click is one row, and the journal
    shows each person only their own actions."""

    def test_preview_confirm_state_and_log_are_the_readers_own(self):
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        backend = MockBackend()
        backend.actions_environ = {"PLX_EXCLUDED_PEOPLE_OIDS":
                                   "22222222-2222-4222-8222-222222222222"}

        def client_for(email: str, oid: str) -> TestClient:
            session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                      subject_email=email, subject_oid=oid)
            client = TestClient(create_app(backend, DevAuth(lambda: session, mode="mock"),
                                           secure_cookie=False), base_url="http://testserver")
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
            return client

        vince = client_for("vince@plx.example", "11111111-1111-4111-8111-111111111111")
        card = next(c for c in vince.get("/api/suggestions").json()["items"]
                    if c["kind"] == "vendor_chase" and c["draft"]["to"]["address"])
        self.assertEqual(card["actions"][0], {"kind": "send_email", "label": "Send email",
                                              "outbound": True})
        ask = {"card_id": card["id"], "kind": "send_email"}
        preview = vince.post("/api/actions/preview", json=ask)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(conforms(preview.json(), component("ActionPreview")), [])
        found = preview.json()
        self.assertEqual((found["payload"]["to"], found["sender"], found["problems"]),
                         ([card["draft"]["to"]["address"]], "vince@plx.example", []))
        self.assertIn("PLX_ACTIONS_LIVE=1 not set", found["missing"][0])
        key = "dialog-0123456789abcdef"
        stale = vince.post("/api/actions/confirm", json={**ask, "confirm": True,
                                                         "idempotency_key": key})
        self.assertEqual(stale.status_code, 409)          # no previewed hash, nothing done
        go = {**ask, "confirm": True, "idempotency_key": key,
              "payload_hash": found["payload_hash"]}
        first, second = (vince.post("/api/actions/confirm", json=go) for _ in range(2))
        self.assertEqual(conforms(first.json(), component("ActionResult")), [])
        self.assertEqual((first.json()["status"], second.json()["replayed"]),
                         ("dry_run", True))
        self.assertEqual(vince.get("/api/actions/state").json()["cards"][card["id"]]["status"],
                         "dry_run")
        log = vince.get("/api/actions/log").json()["rows"]
        self.assertEqual([(r["status"], r["actor_email"]) for r in log],
                         [("dry_run", "vince@plx.example")])
        ana = client_for("ana@plx.example", "33333333-3333-4333-8333-333333333333")
        self.assertEqual(ana.get("/api/actions/log").json()["rows"], [])
        self.assertEqual(ana.get("/api/actions/state").json()["cards"], {})
        missing = vince.post("/api/actions/preview", json={**ask, "card_id": "not-mine"})
        self.assertEqual(missing.status_code, 404)


class CredentialsMasked(unittest.TestCase):
    """SEC-S-09: a credential a source quoted (an API key mailed to a colleague) is never
    served: not in a page, not in an answer (even split over its streamed words), and not
    in a drafted message, whose preview hash and send carry the masked text."""

    KEY = "Abc8Q~bcDEFghiJKLmnoPQRstuVWxyz0123456789"      # Azure client-secret shape
    PASSWORD = "Welcome123!"

    def test_pages_answers_and_drafts_never_carry_a_credential(self):
        from integrations.graph_send import ActionSender
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        key, password = self.KEY, self.PASSWORD
        leak = f"the MC key is MC_MCP_API_KEY={key} and the portal password: {password}"
        sent: list[dict] = []

        class Leaky(MockBackend):
            def home(self, reader):
                return super().home(reader).model_copy(update={"greeting_name": leak})

            def ask(self, reader, request):
                for event, payload in super().ask(reader, request):
                    if event == "token":
                        continue
                    if event == "answer":
                        payload = payload.model_copy(update={"direct_answer": leak})
                        for word in leak.split(" "):
                            yield "token", m.TokenEvent(text=word + " ")
                    yield event, payload

            def suggestions(self, reader):
                found = super().suggestions(reader)
                for card in found.items:
                    if card.kind == "vendor_chase" and card.draft and card.draft.to:
                        card.draft.body = f"{card.draft.body}\n{leak}"
                return found

        class Recording(ActionSender):
            def dispatch(self, kind, sender, payload):
                sent.append(dict(payload))
                return super().dispatch(kind, sender, payload)

        backend = Leaky()
        backend.actions_environ = {"PLX_EXCLUDED_PEOPLE_OIDS":
                                   "22222222-2222-4222-8222-222222222222"}
        backend.action_sender = Recording(backend.actions_environ)
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email="vince@plx.example",
                                  subject_oid="11111111-1111-4111-8111-111111111111")
        client = TestClient(create_app(backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        home = client.get("/api/home")
        streamed = client.post("/api/ask", json={"question": "is PO 90412 late"}).text
        card = next(c for c in client.get("/api/suggestions").json()["items"]
                    if c["kind"] == "vendor_chase" and c["draft"]["to"]["address"])
        ask = {"card_id": card["id"], "kind": "send_email"}
        preview = client.post("/api/actions/preview", json=ask).json()
        result = client.post("/api/actions/confirm", json={
            **ask, "confirm": True, "idempotency_key": "dialog-secret-0123456789",
            "payload_hash": preview["payload_hash"]})
        self.assertEqual(result.json()["status"], "dry_run", result.text)
        for text in (home.text, streamed, json.dumps(card), json.dumps(preview), result.text,
                     json.dumps(sent)):
            self.assertNotIn(key, text)
            self.assertNotIn(password, text)
        self.assertIn("MC_MCP_API_KEY=[secret]", home.json()["greeting_name"])
        answer = events(streamed)
        tokens = "".join(data["text"] for name, data in answer if name == "token")
        self.assertEqual(tokens.strip(), answer[-1][1]["direct_answer"])
        self.assertIn("password: [secret]", tokens)
        # what the person previewed is what goes: the drafted text was masked before the hash
        self.assertEqual(len(sent), 1)
        self.assertIn("password: [secret]", sent[0]["body"])
        self.assertEqual(sent[0]["body"], preview["payload"]["body"])

    def test_a_whole_token_stream_is_sent_as_written_and_masked(self):
        """The simple Ask streams the answer so far in full (whole tokens): each goes out
        at once as one whole token (live, before: they were joined and split into words,
        'ByBy supplier ...', and held until the answer came), never a credential's first
        half, and the last one sent in full before the next event."""
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        key = self.KEY
        written = ["By", "By supplier", "By supplier spend the MC key is MC_MCP_API_KEY="
                   + key[:12], f"By supplier spend the MC key is MC_MCP_API_KEY={key}",
                   f"By supplier spend the MC key is MC_MCP_API_KEY={key} so far.\n"]
        final = written[-1].strip()

        class Streaming(MockBackend):
            def ask(self, reader, request):
                for event, payload in super().ask(reader, request):
                    if event == "token":
                        continue
                    if event == "answer":
                        for text in written[:2]:
                            yield "token", m.TokenEvent(text=text, whole=True)
                        yield "status", m.StatusEvent(step="reading", message="Reading",
                                                      elapsed_ms=1)
                        for text in written[2:]:
                            yield "token", m.TokenEvent(text=text, whole=True)
                        yield "status", m.StatusEvent(step="answering", message="Writing",
                                                      elapsed_ms=2)
                        yield "token", m.TokenEvent(text=final, whole=True)
                        payload = payload.model_copy(update={"direct_answer": final})
                    yield event, payload

        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email="vince@plx.example",
                                  subject_oid="11111111-1111-4111-8111-111111111111")
        client = TestClient(create_app(Streaming(), DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        streamed = client.post("/api/ask", json={"question": "top suppliers"}).text
        self.assertNotIn(key[:12], streamed)
        got = events(streamed)
        names = [name for name, _ in got]
        tokens = [data for name, data in got if name == "token"]
        self.assertTrue(all(t.get("whole") for t in tokens), tokens)
        # sent as written: the first words before the next step, not held for the answer
        self.assertLess(names.index("token"), names.index("status", names.index("token")))
        before = [d["text"] for n, d in got[:names.index("status", names.index("token"))]
                  if n == "token"]
        self.assertEqual(before[-1], "By supplier")
        self.assertEqual([t["text"] for t in tokens][-1].strip(),
                         final.replace(key, "[secret]"))
        self.assertEqual(got[-1][0], "answer")
        for t in tokens:
            self.assertNotIn("ByBy", t["text"])
        # each whole text once, and never shorter than one already sent in full
        texts = [t["text"] for t in tokens]
        self.assertEqual(len(texts), len(set(texts)))
        for a, b in zip(texts, texts[1:]):
            self.assertFalse(a.startswith(b) and len(b) < len(a), (a, b))


class CardNumbersNeverServed(unittest.TestCase):
    """A payment card number in captured text (a filled authorisation form in a mail or its
    OCR reading) is served as its last four digits, its CVV and expiry masked: in a page,
    in an Ask answer (its four groups streamed as four words), in a drafted message (the
    preview hash and the send carry the masked text) and in an exported CSV. A PO number
    beside it is served as it is. (The networks' published test number, never a real one.)"""

    CARD = "4111 1111 1111 1111"
    FORM = f"Card Number: {CARD} Exp: 09/28 CVV: 737 for PO 4500123456"

    def test_pages_answers_drafts_and_exports_never_carry_a_card(self):
        from integrations.graph_send import ActionSender
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        form, sent = self.FORM, []

        class Leaky(MockBackend):
            def home(self, reader):
                return super().home(reader).model_copy(update={"greeting_name": form})

            def ask(self, reader, request):
                for event, payload in super().ask(reader, request):
                    if event == "token":
                        continue
                    if event == "answer":
                        payload = payload.model_copy(update={"direct_answer": form})
                        for word in form.split(" "):
                            yield "token", m.TokenEvent(text=word + " ")
                    yield event, payload

            def suggestions(self, reader):
                found = super().suggestions(reader)
                for card in found.items:
                    if card.kind == "vendor_chase" and card.draft and card.draft.to:
                        card.draft.body = f"{card.draft.body}\n{form}"
                return found

            def my_work_order_import(self, reader, item_id):
                return "order.csv", f"line,note\n1,{form}\n".encode("utf-8")

        class Recording(ActionSender):
            def dispatch(self, kind, sender, payload):
                sent.append(dict(payload))
                return super().dispatch(kind, sender, payload)

        backend = Leaky()
        backend.actions_environ = {"PLX_EXCLUDED_PEOPLE_OIDS":
                                   "22222222-2222-4222-8222-222222222222"}
        backend.action_sender = Recording(backend.actions_environ)
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email="vince@plx.example",
                                  subject_oid="11111111-1111-4111-8111-111111111111")
        client = TestClient(create_app(backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        home = client.get("/api/home")
        streamed = client.post("/api/ask", json={"question": "is PO 90412 late"}).text
        card = next(c for c in client.get("/api/suggestions").json()["items"]
                    if c["kind"] == "vendor_chase" and c["draft"]["to"]["address"])
        ask = {"card_id": card["id"], "kind": "send_email"}
        preview = client.post("/api/actions/preview", json=ask).json()
        result = client.post("/api/actions/confirm", json={
            **ask, "confirm": True, "idempotency_key": "dialog-card-0123456789",
            "payload_hash": preview["payload_hash"]})
        self.assertEqual(result.json()["status"], "dry_run", result.text)
        exported = client.get("/api/my-work/x/order-import")
        self.assertEqual(exported.status_code, 200, exported.text)
        shown = "\u2022\u2022\u2022\u2022 " * 3 + "1111"
        for text in (home.text, streamed, json.dumps(card), json.dumps(preview), result.text,
                     json.dumps(sent), exported.text):
            self.assertNotIn(self.CARD, text)
            self.assertNotIn(self.CARD.replace(" ", ""), text)
        answer = events(streamed)
        tokens = "".join(data["text"] for name, data in answer if name == "token")
        self.assertEqual(len(sent), 1)
        masked = f"Card Number: {shown} Exp: \u2022\u2022/\u2022\u2022 CVV: \u2022\u2022\u2022 " \
                 "for PO 4500123456"
        for text in (home.json()["greeting_name"], tokens, sent[0]["body"], exported.text):
            self.assertIn(masked, text)
        self.assertIn(shown, sent[0]["body"])
        self.assertEqual(sent[0]["body"], preview["payload"]["body"])


if __name__ == "__main__":
    unittest.main()


class WatcherItemStateRoute(unittest.TestCase):
    """POST /api/briefing/watcher/{item_id} over the real backend method: the reader's own
    view decides it; an item they may not see is 404, a bad snooze 400, a bad state 422."""

    def test_dismiss_snooze_and_refusals(self):
        from operator_api.backend import WorkspaceBackend
        calls = []

        class View:
            def watcher_item_state(self, item_id, state, *, until=None, reason=None):
                calls.append((item_id, state, until))
                if item_id != "aw-0000000000000001":
                    raise KeyError("item_not_found")
                if state == "snoozed" and until == "2000-01-01":
                    raise ValueError("snooze_until_invalid")
                return {"item_id": item_id, "state": state,
                        "until": "2099-01-08" if state == "snoozed" else None,
                        "at": "2099-01-01T00:00:00Z"}

        class Reads:
            def run(self, reader, job, timeout=None):
                return job(View())

        class Backend(MockBackend):
            reads = Reads()
            watcher_item_state = WorkspaceBackend.watcher_item_state

        client = TestClient(create_app(Backend(), DevAuth(
            fixture_identity("plx", ["staff:demo"]), mode="mock"), secure_cookie=False),
            base_url="http://testserver")
        url = "/api/briefing/watcher/aw-0000000000000001"
        self.assertEqual(client.post(url, json={"state": "dismissed"}).status_code, 401)
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        done = client.post(url, json={"state": "dismissed", "reason": "handled"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(conforms(done.json(), component("WatcherItemState")), [])
        self.assertEqual(done.json()["state"], "dismissed")
        snoozed = client.post(url, json={"state": "snoozed"}).json()
        self.assertEqual((snoozed["state"], snoozed["until"]), ("snoozed", "2099-01-08"))
        hidden = client.post("/api/briefing/watcher/aw-ffffffffffffffff",
                             json={"state": "dismissed"})
        self.assertEqual((hidden.status_code, hidden.json()["error"]["code"]), (404, "item_not_found"))
        bad = client.post(url, json={"state": "snoozed", "until": "2000-01-01"})
        self.assertEqual((bad.status_code, bad.json()["error"]["code"]), (400, "snooze_invalid"))
        self.assertEqual(client.post(url, json={"state": "deleted"}).status_code, 422)
        self.assertEqual(calls[0], ("aw-0000000000000001", "dismissed", None))


class BriefRoutes(unittest.TestCase):
    """The three brief routes for a signed-in reader (the mock world: a synthetic calendar
    and week, no finance marking); the auth walk above covers them signed out."""

    def test_weekly_review_meetings_and_a_brief_that_fails_closed(self):
        from operating import briefs as br
        client = signed_in()
        weekly = client.get("/api/brief/weekly", params={"week": "2026-11-02"})
        self.assertEqual(weekly.status_code, 200, weekly.text)
        body = weekly.json()
        self.assertEqual((body["kind"], body["title"]),
                         ("weekly", "Weekly review, week of 2026-11-02"))
        states = {s["key"]: s["state"] for s in body["sections"]}
        self.assertEqual(states["cash"], "finance_marked")
        self.assertEqual(states["margin"], "finance_marked")
        self.assertFalse([f for s in body["sections"] for f in s["figures"]
                          if f["unit"] == "money"])
        # the demo week has the real builder's sections, no more and no fewer
        real = br.weekly_review(lambda tool, args: {"error": {"code": "x", "message": "x"}},
                                frozenset(), {"status": "not_configured"},
                                week=date(2026, 11, 2), today=date(2026, 11, 10))
        self.assertEqual(set(states), {s["key"] for s in real["sections"]})
        self.assertEqual(client.get("/api/brief/weekly", params={"week": "nope"}).status_code,
                         422)
        meetings = client.get("/api/brief/meetings").json()["meetings"]
        self.assertTrue(meetings)
        for e in meetings:
            if e["private"]:
                self.assertEqual((e["attendees"], e["agenda"], e["location"]), ([], None, None))
                continue
            self.assertTrue(e["organizer"] and e["attendees"], e["object"]["label"])
            brief = client.get(e["brief_url"].replace("/meetings/brief/", "/api/brief/"))
            self.assertEqual(brief.status_code, 200, e["brief_url"])
            # every source the brief cites opens, named as the brief names it
            cited = {}

            def walk(node):
                if isinstance(node, dict):
                    if isinstance(node.get("handle"), str) and node.get("label"):
                        cited[node["handle"]] = node["label"]
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            walk(brief.json())
            self.assertTrue(cited, e["brief_url"])
            for handle, label in cited.items():
                opened = client.get("/api/source", params={"handle": handle})
                self.assertEqual(opened.status_code, 200, handle)
                self.assertEqual(opened.json()["label"], label, handle)
        self.assertEqual(client.get("/api/source", params={"handle": "demo2://x/y"}).status_code,
                         404)
        for path in ("/api/brief/PurchaseOrder/90412", "/api/brief/CalendarEvent/x",
                     "/api/brief/Customer/" + "x" * 701):
            got = client.get(path)
            self.assertIn(got.status_code, (404, 422), path)
            self.assertIn("error", got.json(), path)


class ReaderWrites(unittest.TestCase):
    """What counts as the reader's own write (their kept screens stop being current)."""

    def test_a_write_counts_and_an_ask_under_way_does_not(self):
        wrote: list = []

        class Recording(MockBackend):
            def reader_wrote(self, reader, screens=None):
                wrote.append((reader.key, screens))

        app = create_app(Recording(), DevAuth(fixture_identity("plx", ["staff:demo"]),
                                              mode="mock"), secure_cookie=False)
        client = TestClient(app, base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        wrote.clear()
        self.assertEqual(client.get("/api/home").status_code, 200)
        self.assertEqual(wrote, [])                    # a read is no write
        asked = client.post("/api/ask", json={"question": "is 90412 late?"})
        self.assertEqual(asked.status_code, 200, asked.text)
        self.assertEqual(wrote, [])                    # the Ask counts its own answer
        self.assertEqual(client.post("/api/briefing/seen").status_code, 200)
        self.assertEqual(len(wrote), 2)                # before it ran and after
        self.assertEqual(len(set(wrote)), 1)
        # marking the briefing seen changes the briefing and Home only, not My work
        self.assertEqual(wrote[0][1], frozenset({"briefing", "briefing_items", "home", "suggestions"}))
        wrote.clear()
        client.post("/api/feedback/note", json={"note": "x"})
        self.assertEqual({w[1] for w in wrote}, {None})   # any other write: every screen


class BriefingPagesAtOnce(unittest.TestCase):
    """Home's groups' first pages in one request (they were one request per group)."""

    def setUp(self) -> None:
        self.backend = MockBackend()
        app = create_app(self.backend, DevAuth(fixture_identity("plx", ["staff:demo"]),
                                               mode="mock"), secure_cookie=False)
        self.client = TestClient(app, base_url="http://testserver")
        self.assertEqual(self.client.post("/api/auth/dev-login", json={}).status_code, 200)

    def test_each_type_is_what_the_items_route_answers_in_the_order_asked(self):
        types = ["price_below_cost", "po_due", "price_below_cost", "po_late"]
        got = self.client.get("/api/briefing/pages", params=[("event_type", t) for t in types])
        self.assertEqual(got.status_code, 200, got.text)
        pages = got.json()["pages"]
        self.assertEqual([p["event_type"] for p in pages],
                         ["price_below_cost", "po_due", "po_late"])   # a repeat once
        for page in pages:
            alone = self.client.get("/api/briefing/items", params={
                "event_type": page["event_type"], "offset": 0, "limit": 25}).json()
            self.assertEqual(page, alone)

    def test_a_type_that_fails_is_left_out_and_all_failing_is_that_failure(self):
        real = self.backend.briefing_items

        def items(reader, event_type, **kw):
            if event_type == "po_due":
                raise TimeoutError("workbench_busy")
            return real(reader, event_type, **kw)

        with mock.patch.object(self.backend, "briefing_items", items):
            got = self.client.get("/api/briefing/pages", params=[
                ("event_type", "po_due"), ("event_type", "po_late")])
            self.assertEqual([p["event_type"] for p in got.json()["pages"]], ["po_late"])
            every = self.client.get("/api/briefing/pages", params=[("event_type", "po_due")])
            self.assertEqual((every.status_code, every.json()["error"]["code"]), (503, "busy"))

    def test_the_briefing_carries_the_pages_home_can_show_when_asked(self):
        plain = self.client.get("/api/briefing").json()
        self.assertEqual(plain.get("pages", []), [])
        got = self.client.get("/api/briefing", params={"pages": "true"}).json()
        top = got["items"]
        wanted = [g["event_type"] for g in got["groups"]
                  if g["event_type"] != "po_late" and g["count"] > g["in_top"]
                  and sum(1 for i in top if i["event_type"] == g["event_type"] and i.get("home")) < 3]
        if (got.get("home_count") or 0) <= sum(1 for i in top if i.get("home")):
            wanted = []
        self.assertEqual([p["event_type"] for p in got.get("pages", [])], wanted)
        for page in got.get("pages", []):
            alone = self.client.get("/api/briefing/items", params={
                "event_type": page["event_type"], "offset": 0, "limit": 25}).json()
            self.assertEqual(page, alone)

    def test_a_group_needs_a_page_only_when_home_can_show_more_of_it(self):
        from types import SimpleNamespace as N
        from operator_api.app import _home_group_pages
        item = lambda kind, home=True: N(event_type=kind, home=home)
        group = lambda kind, count, in_top: N(event_type=kind, count=count, in_top=in_top)
        got = N(home_count=9, items=[item("po_due"), item("po_due"), item("price_below_cost", False)],
                groups=[group("po_due", 5, 2), group("po_late", 40, 0), group("price_below_cost", 7, 1),
                        group("po_unconfirmed", 4, 4), group("no_owner", 3, 0)])
        # po_due shows 2 of 3 rows; po_late has its own list; the price group's one top item
        # is not sent home (shown 0); a group wholly in the top needs none
        self.assertEqual(_home_group_pages(got), ["po_due", "price_below_cost", "no_owner"])
        got.home_count = 2   # the top items already fill what Home lists
        self.assertEqual(_home_group_pages(got), [])

    def test_no_type_or_an_unknown_one_is_refused(self):
        self.assertEqual(self.client.get("/api/briefing/pages").status_code, 422)
        self.assertEqual(self.client.get("/api/briefing/pages", params={
            "event_type": "nonsense"}).status_code, 422)
        self.assertEqual(self.client.get("/api/briefing/pages", params=[
            ("event_type", "po_late")] * 13).status_code, 422)   # at most 12 asked


class HealthShowsThePolicyInForce(unittest.TestCase):
    def test_health_names_the_policy_digest_and_an_edit_not_applied(self) -> None:
        """SEC-005: with an employee resolver the public health check carries the policy
        digest in force and why its file's latest edit was not applied (a code, never a
        record); without one it says nothing of a policy; a failing status never fails
        liveness."""
        import argparse
        import os
        import tempfile
        from unittest import mock
        from operating.tests.test_employee_permissions import POLICY
        from operator_api.cli import make_auth
        from operator_api.workspace.case_console import employee_resolver
        auth = DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock")
        self.assertNotIn("employee_policy",
                         TestClient(create_app(MockBackend(), auth, secure_cookie=False))
                         .get("/api/health").json())
        # the live shape: the ticket sign-in ``make_auth`` builds over the console resolver
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLX_OPS_DIR": tmp}):
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(POLICY))
            resolver = employee_resolver(path, None)
            with mock.patch.dict(os.environ, {"PLX_WORKBENCH_SESSION_SECRET": "t" * 64}):
                auth, _ = make_auth(argparse.ArgumentParser(), argparse.Namespace(
                    dev_fixture_principal=[], dev_employee_oid=None), "dev",
                    {"resolver": resolver, "replay_guard": None})
            client = TestClient(create_app(MockBackend(), auth, secure_cookie=False))
            got = client.get("/api/health")
            self.assertEqual(got.status_code, 200)
            self.assertEqual(got.json()["employee_policy"], {
                "digest": resolver.policy.digest, "reload_error": None, "reloads": 0,
                "state": "in_force", "refused_since": None, "alarm_error": None})
            digest = resolver.policy.digest
            path.write_text("{")              # an edit that does not load
            with self.assertLogs("operating.employee_permissions.policy", "ERROR"):
                shown = client.get("/api/health").json()["employee_policy"]
            # the last good policy stays in force; health says so and why
            self.assertEqual((shown["state"], shown["digest"], shown["alarm_error"]),
                             ("kept_last_good", digest, None))
            # public health names the refusal by a code, never the parser's text
            # (AUDIT4-13): the file's own words stay off the unauthenticated route
            self.assertEqual(shown["reload_error"], "employee_policy_unreadable")
            self.assertTrue(shown["refused_since"])
            self.assertEqual(set(shown), {"digest", "reload_error", "reloads", "state",
                                          "refused_since", "alarm_error"})
            self.assertTrue(resolver.policy_status()["reload_error"])
            self.assertNotEqual(resolver.policy_status()["reload_error"],
                                "employee_policy_unreadable")
            path.unlink()                     # a missing file: its own code stays shown
            with self.assertLogs("operating.employee_permissions.policy", "ERROR"):
                shown = client.get("/api/health").json()["employee_policy"]
            self.assertEqual(shown["reload_error"], "employee_policy_missing")
        from operator_api.app import public_policy_status
        for bad in ({"reload_error": "sam@x.com in line 3", "alarm_error": "a b",
                     "state": "weird", "extra": "x"}, {"reload_error": 7}):
            with self.subTest(bad=bad):
                got = public_policy_status(bad)
                self.assertEqual(got["reload_error"], "employee_policy_unreadable")
                self.assertNotIn("extra", got)
                self.assertNotIn("sam@", json.dumps(got))
        self.assertIsNone(public_policy_status("x"))

        def broken(self):
            raise RuntimeError("x")
        auth.resolver = type("R", (), {"policy_status": broken})()
        got = TestClient(create_app(MockBackend(), auth, secure_cookie=False)).get(
            "/api/health").json()
        self.assertEqual((got["status"], got["employee_policy"]), ("ok", None))


class StreamKeepAlive(unittest.TestCase):
    """harden-1114: an Ask whose server froze or restarted mid-answer left the app
    waiting for ever (no byte ever came). While nothing new is ready the stream says it is
    alive every KEEP_ALIVE_SECONDS (an SSE comment), so the app can tell a long answer from
    a dead server; closing the stream closes its source, and a failure still surfaces."""

    def test_silence_is_filled_with_keep_alives_and_events_pass_through(self):
        from operator_api import app as app_module

        def slow():
            yield "event: status\ndata: {}\n\n"
            time.sleep(0.35)
            yield "event: answer\ndata: {}\n\n"

        got = list(app_module.kept_alive(slow(), every=0.1))
        self.assertEqual(got[0], "event: status\ndata: {}\n\n")
        self.assertEqual(got[-1], "event: answer\ndata: {}\n\n")
        self.assertGreaterEqual(got.count(app_module.KEEP_ALIVE), 2)
        self.assertTrue(app_module.KEEP_ALIVE.startswith(":"))   # a comment parsers skip

    def test_an_early_close_closes_the_source_and_a_failure_surfaces(self):
        from operator_api import app as app_module
        closed = []

        def source():
            try:
                for i in range(1000):
                    yield f"event: token\ndata: {i}\n\n"
                    time.sleep(0.01)
            finally:
                closed.append(True)

        stream = app_module.kept_alive(source(), every=0.1)
        next(stream)
        stream.close()
        deadline = time.time() + 5
        while not closed and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(closed, [True])

        def broken():
            yield "event: status\ndata: {}\n\n"
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            list(app_module.kept_alive(broken(), every=0.1))



class MockWorkload(unittest.TestCase):
    """The fixture backend answers Team's workload reads (the app walk read 500 there)."""

    def test_workload_counts_teams_and_the_readers_own_row(self):
        body = signed_in().get("/api/workload").json()
        self.assertTrue(body["teams"])
        self.assertIn("PUR", body["granted_teams"])
        self.assertEqual([p["person"] for p in body["people"] if p["self"]], ["demo-0"])
        # a team of fewer than three gives no reply count (it would name one person)
        small = [t for t in body["teams"] if t["team"] in ("QA", "FIN")]
        self.assertTrue(small and all(t["awaiting_reply"] is None for t in small))

    def test_grant_sheet_is_the_owners_only(self):
        response = signed_in().get("/api/workload/grants")
        self.assertEqual(response.status_code, 403, response.text)
