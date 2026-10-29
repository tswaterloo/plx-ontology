"""The app API's contract at its HTTP boundary: auth on every route, bind refusals, the
SSE ask payload, plain errors, and the mock payloads against the committed openapi.json."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import unittest

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
            ("GET", "/api/source", {"handle": "bc://demo/purchaseOrders/90412"}, None,
             "SourceView"),
            ("POST", "/api/feedback", None, {"answer_id": answer_id, "rating": -1,
                                             "note": "wrong supplier"}, "FeedbackResponse"),
        ]
        for method, path, params, body, name in checks:
            response = client.request(method, path, params=params, json=body)
            self.assertEqual(response.status_code, 200, (path, response.text))
            self.assertEqual(conforms(response.json(), component(name)), [], path)
            route = path if path in SPEC["paths"] else "/api/object/{object_type}/{object_id}"
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


if __name__ == "__main__":
    unittest.main()
