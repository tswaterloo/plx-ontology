"""Tests for the read-only Genie MCP endpoint (POST /api/mcp)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend
from operator_api import view_as as va


def _client() -> TestClient:
    app = create_app(
        MockBackend(),
        DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
        secure_cookie=False,
    )
    return TestClient(app, base_url="http://testserver")


def _signed_in() -> TestClient:
    client = _client()
    resp = client.post("/api/auth/dev-login", json={})
    assert resp.status_code == 200, resp.text
    return client


def _rpc(client: TestClient, method: str, params: dict | None = None, req_id: int = 1,
         headers: dict | None = None):
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post("/api/mcp", json=payload, headers=headers or {})


class GenieMcpTests(unittest.TestCase):
    def test_unauthenticated_rejected_401(self) -> None:
        client = _client()
        resp = _rpc(client, "initialize")
        self.assertEqual(resp.status_code, 401)
        # Unauthenticated caller with X-PLX-View-As still gets 401 first
        resp_va = _rpc(client, "ping", headers={va.HEADER: "someone@example.com"})
        self.assertEqual(resp_va.status_code, 401)

    def test_view_as_rejected_403(self) -> None:
        client = _signed_in()
        resp = _rpc(client, "ping", headers={va.HEADER: "someone@example.com"})
        self.assertEqual(resp.status_code, 403)

    def test_initialize_and_ping(self) -> None:
        client = _signed_in()
        init_resp = _rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(init_resp.status_code, 200)
        body = init_resp.json()
        self.assertEqual(body["id"], 1)
        self.assertIn("protocolVersion", body["result"])
        self.assertIn("tools", body["result"]["capabilities"])
        self.assertIn("resources", body["result"]["capabilities"])

        ping_resp = _rpc(client, "ping", req_id=2)
        self.assertEqual(ping_resp.status_code, 200)
        self.assertEqual(ping_resp.json()["result"], {})

    def test_tools_list_and_resources(self) -> None:
        client = _signed_in()
        tools_resp = _rpc(client, "tools/list")
        self.assertEqual(tools_resp.status_code, 200)
        names = [t["name"] for t in tools_resp.json()["result"]["tools"]]
        self.assertEqual(names, ["genie_list_measures", "genie_run_measure", "genie_list_views"])

        res_list = _rpc(client, "resources/list")
        self.assertEqual(res_list.status_code, 200)
        uris = [r["uri"] for r in res_list.json()["result"]["resources"]]
        self.assertIn("genie://measures", uris)

        res_read = _rpc(client, "resources/read", {"uri": "genie://measures"})
        self.assertEqual(res_read.status_code, 200)
        contents = res_read.json()["result"]["contents"]
        self.assertEqual(len(contents), 1)
        parsed = json.loads(contents[0]["text"])
        self.assertIn("measures", parsed)

        bad_res = _rpc(client, "resources/read", {"uri": "genie://unknown"})
        self.assertEqual(bad_res.status_code, 200)
        self.assertEqual(bad_res.json()["error"]["code"], -32602)

    def test_genie_list_measures_and_views(self) -> None:
        client = _signed_in()
        m_resp = _rpc(client, "tools/call", {"name": "genie_list_measures", "arguments": {}})
        self.assertEqual(m_resp.status_code, 200)
        m_data = json.loads(m_resp.json()["result"]["content"][0]["text"])
        self.assertIn("measures", m_data)

        v_resp = _rpc(client, "tools/call", {"name": "genie_list_views", "arguments": {}})
        self.assertEqual(v_resp.status_code, 200)
        v_data = json.loads(v_resp.json()["result"]["content"][0]["text"])
        self.assertIn("views", v_data)

    def test_genie_run_measure_uses_reader_worker(self) -> None:
        backend = MockBackend()
        calls: list[tuple[str, dict]] = []

        class _FakeView:
            def _call(self, tool: str, arguments: dict) -> dict:
                calls.append((tool, dict(arguments)))
                return {
                    "rows": [{"value": 42}],
                    "sql": "SELECT 42 AS value",
                    "citations": ["m1"],
                }

        class _FakeWorker:
            def __init__(self) -> None:
                self.readers: list[object] = []

            def run(self, reader, fn, *, timeout=None):
                self.readers.append(reader)
                return fn(_FakeView())

        worker = _FakeWorker()
        backend.reads = worker  # type: ignore[attr-defined]
        app = create_app(
            backend,
            DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
            secure_cookie=False,
        )
        client = TestClient(app, base_url="http://testserver")
        assert client.post("/api/auth/dev-login", json={}).status_code == 200

        resp = _rpc(
            client,
            "tools/call",
            {
                "name": "genie_run_measure",
                "arguments": {"id": "revenue", "period": "2026-10", "by": "region", "where": {"tier": "A"}},
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(worker.readers), 1)
        self.assertEqual(
            calls,
            [("plx.sql", {"measure": "revenue", "period": "2026-10", "by": "region", "filter": {"tier": "A"}})],
        )
        result = resp.json()["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["rows"], [{"value": 42}])
        self.assertEqual(payload["measure"], "revenue")
        self.assertIn("as_of", payload)

    def test_genie_run_measure_marks_sql_errors(self) -> None:
        backend = MockBackend()

        class _ErrView:
            def _call(self, tool: str, arguments: dict) -> dict:
                return {"error": {"code": "not_readable", "message": "outside scope"}}

        class _FakeWorker:
            def run(self, reader, fn, *, timeout=None):
                return fn(_ErrView())

        backend.reads = _FakeWorker()  # type: ignore[attr-defined]
        app = create_app(
            backend,
            DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
            secure_cookie=False,
        )
        client = TestClient(app, base_url="http://testserver")
        assert client.post("/api/auth/dev-login", json={}).status_code == 200

        resp = _rpc(client, "tools/call", {"name": "genie_run_measure", "arguments": {"id": "secret"}})
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["error"]["code"], "not_readable")

    def test_unknown_or_write_tool_and_method_rejected(self) -> None:
        client = _signed_in()
        for bad_tool in ("plx.write", "send_email", "raw_sql", "unknown_tool"):
            resp = _rpc(client, "tools/call", {"name": bad_tool, "arguments": {}})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["error"]["code"], -32601)

        bad_method = _rpc(client, "tools/write", {})
        self.assertEqual(bad_method.status_code, 200)
        self.assertEqual(bad_method.json()["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
