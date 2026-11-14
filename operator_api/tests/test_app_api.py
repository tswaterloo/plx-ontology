"""The app API's contract at its HTTP boundary: auth on every route, bind refusals, the
SSE ask payload, plain errors, and the mock payloads against the committed openapi.json."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import unittest
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
        self.assertEqual(wrote[0][1], frozenset({"briefing", "home", "suggestions"}))
        wrote.clear()
        client.post("/api/feedback/note", json={"note": "x"})
        self.assertEqual({w[1] for w in wrote}, {None})   # any other write: every screen


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
            self.assertTrue(shown["reload_error"])
            self.assertTrue(shown["refused_since"])

        def broken(self):
            raise RuntimeError("x")
        auth.resolver = type("R", (), {"policy_status": broken})()
        got = TestClient(create_app(MockBackend(), auth, secure_cookie=False)).get(
            "/api/health").json()
        self.assertEqual((got["status"], got["employee_policy"]), ("ok", None))
