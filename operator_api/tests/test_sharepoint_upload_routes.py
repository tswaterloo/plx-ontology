"""Unit tests for SharePoint folder discovery and artifact upload routes."""
from __future__ import annotations

import base64
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from operating import autopilot_write_adapters as wa
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._rows = rows or []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, folder_rows: list[tuple[Any, ...]], inserts: list[tuple[str, Any]],
                 forbid_matters: bool = False) -> None:
        self._folder_rows = folder_rows
        self._inserts = inserts
        self._forbid_matters = forbid_matters

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        if "FROM biz.sharepoint_files" in sql:
            return _FakeCursor(self._folder_rows)
        if "INSERT INTO assist.matters" in sql and self._forbid_matters:
            raise PermissionError("permission denied for table matters")
        if "INSERT INTO assist." in sql:
            self._inserts.append((sql, params))
        return _FakeCursor([])

    def commit(self) -> None:
        pass


class _FakePool:
    def __init__(self, folder_rows: list[tuple[Any, ...]] | None = None,
                 forbid_matters: bool = False) -> None:
        self.folder_rows = folder_rows or []
        self.inserts: list[tuple[str, Any]] = []
        self.forbid_matters = forbid_matters

    @contextmanager
    def connection(self):
        yield _FakeConn(self.folder_rows, self.inserts, forbid_matters=self.forbid_matters)


class _FakeExecutor:
    def __init__(self, live: bool, adapter: Any) -> None:
        self._live = live
        self.adapters = {"sharepoint_write": adapter}

    def live_kinds(self, _item: Any) -> set[str]:
        return {"sharepoint_write"} if self._live else set()


class _SharePointBackend(MockBackend):
    """MockBackend with injectable Postgres pool and live SharePoint write executor."""

    def __init__(self, *, pool: Any = None, state_dir: Path | None = None,
                 executor: Any = None) -> None:
        super().__init__()
        self.pool = pool
        self.state_dir = state_dir
        self._test_executor = executor

    def _apa_state(self) -> Path | None:
        return self.state_dir if self._test_executor is not None else None

    def _apa_executor(self, _state: Path) -> Any:
        return self._test_executor

    def _apa_me(self, reader: Any) -> set[str]:
        return {reader.key, "ops@example.com"}

    def _instruct_guard(self) -> Any:
        return None


def _signed_in_client(backend: Any) -> TestClient:
    auth = DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock")
    client = TestClient(create_app(backend, auth, secure_cookie=False), base_url="http://testserver")
    resp = client.post("/api/auth/dev-login", json={})
    assert resp.status_code == 200, resp.text
    return client


class SharePointUploadRoutesTest(unittest.TestCase):
    def test_routes_require_authentication(self) -> None:
        auth = DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock")
        client = TestClient(create_app(MockBackend(), auth, secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.get("/api/sharepoint/folders").status_code, 401)
        self.assertEqual(
            client.post(
                "/api/artifacts/sharepoint-upload",
                json={"title": "Q4 Brief", "format": "docx", "folder_path": "Shared Documents/Quality"},
            ).status_code,
            401,
        )

    def test_get_sharepoint_folders_merges_postgres_and_defaults(self) -> None:
        pool = _FakePool(
            folder_rows=[
                ("CM Operations Files/2026 Reports", 42, "sharepoint://b!drive-ops-123/item-1"),
                ("Customer Documents/Acme Packaging", 17, "sharepoint://b!drive-cust-456/item-2"),
            ],
        )
        backend = _SharePointBackend(pool=pool)
        client = _signed_in_client(backend)
        resp = client.get("/api/sharepoint/folders")
        self.assertEqual(resp.status_code, 200)
        folders = resp.json()["folders"]
        paths = [f["path"] for f in folders]
        self.assertEqual(paths[0], "CM Operations Files/2026 Reports")
        self.assertEqual(folders[0]["drive_id"], "b!drive-ops-123")
        self.assertEqual(folders[0]["file_count"], 42)
        self.assertEqual(folders[0]["source"], "postgres")
        self.assertIn("Customer Documents/Acme Packaging", paths)
        self.assertIn("Shared Documents/Operations", paths)
        self.assertIn("Shared Documents/Purchasing", paths)

    def test_upload_queues_durable_assist_draft_and_generates_downloadable_xlsx(self) -> None:
        with TemporaryDirectory() as tmp:
            pool = _FakePool(
                folder_rows=[("Shared Documents/Purchasing", 11, "sharepoint://b!drive-ops-123/item-9")],
                forbid_matters=True,  # simulates plx_api role appending to assist.job_log
            )
            backend = _SharePointBackend(pool=pool, state_dir=Path(tmp))
            client = _signed_in_client(backend)
            resp = client.post(
                "/api/artifacts/sharepoint-upload",
                json={
                    "title": "Open PO Landed Cost Tracker",
                    "format": "xlsx",
                    "folder_path": "Shared Documents/Purchasing/2026 Q4",
                    "create_folder_if_missing": True,
                    "content_markdown": "| PO | Cost |\n|---|---|\n| PO-90412 | 142800 |",
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(body["status"], "approval_queued")
            self.assertEqual(body["filename"], "Open PO Landed Cost Tracker.xlsx")
            self.assertEqual(body["folder_path"], "Shared Documents/Purchasing/2026 Q4")
            self.assertTrue(body["create_folder_if_missing"])
            self.assertTrue(body["draft_id"].startswith("sp_"))
            self.assertTrue(body["download_url"].startswith("/api/instruct/files/f_"))
            self.assertGreater(body["size_bytes"], 200)

            # Verify durable Postgres assist.job_log entries were recorded
            self.assertEqual(len(pool.inserts), 2)
            self.assertTrue(any("assist.job_log" in sql for sql, _ in pool.inserts))

            # Verify the generated Excel file is immediately downloadable and valid zip/OOXML
            dl = client.get(body["download_url"])
            self.assertEqual(dl.status_code, 200)
            self.assertEqual(dl.content[:2], b"PK")

    def test_upload_executes_live_graph_folder_creation_and_put_when_enabled(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_transport(req: Any, key: str) -> tuple[int, dict[str, Any]]:
            calls.append({"method": req["method"], "url": req["url"], "body": req.get("body"), "key": key})
            if req["method"] == "POST":
                # First folder already exists (409 idempotent), nested folder created (201)
                if req["body"]["name"] == "Shared Documents":
                    return 409, {"error": {"code": "nameAlreadyExists"}}
                return 201, {"id": "folder-new-1", "name": req["body"]["name"]}
            return 201, {
                "id": "item-uploaded-7",
                "eTag": '"etag-7"',
                "webUrl": "https://tenant.sharepoint.com/Shared%20Documents/Finance/Q4/Margin%20Deck.pptx",
            }

        adapter = wa.SharePointAdapter(transport=fake_transport)
        executor = _FakeExecutor(live=True, adapter=adapter)
        with TemporaryDirectory() as tmp:
            pool = _FakePool(
                folder_rows=[("Shared Documents/Finance", 9, "sharepoint://b!drive-fin-999/item-1")],
            )
            backend = _SharePointBackend(pool=pool, state_dir=Path(tmp), executor=executor)
            client = _signed_in_client(backend)
            resp = client.post(
                "/api/artifacts/sharepoint-upload",
                json={
                    "title": "Margin Deck",
                    "format": "pptx",
                    "folder_path": "Shared Documents/Finance/Q4",
                    "create_folder_if_missing": True,
                    "content_markdown": "# Q4 Margin Review\n\n- Margin expanded +240 bps",
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(body["status"], "uploaded")
            self.assertEqual(body["filename"], "Margin Deck.pptx")
            self.assertEqual(
                body["web_url"],
                "https://tenant.sharepoint.com/Shared%20Documents/Finance/Q4/Margin%20Deck.pptx",
            )
            self.assertIn("Shared Documents/Finance/Q4", body["folders_created"])

            # Verify Graph folder creation POSTs + final PUT upload
            methods = [c["method"] for c in calls]
            self.assertEqual(methods, ["POST", "POST", "POST", "PUT"])
            self.assertIn("/drives/b!drive-fin-999/root/children", calls[0]["url"])
            self.assertIn("Margin%20Deck.pptx:/content", calls[-1]["url"])

    def test_upload_rejects_path_traversal(self) -> None:
        backend = _SharePointBackend()
        client = _signed_in_client(backend)
        resp = client.post(
            "/api/artifacts/sharepoint-upload",
            json={
                "title": "Report",
                "format": "docx",
                "folder_path": "Shared Documents/../Secrets",
            },
        )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
