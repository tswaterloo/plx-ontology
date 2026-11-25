"""Tests for Assist speed, batched door checks, full-text message reads, and job checker prompts."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from operating import autopilot as ap
from operating import autopilot_check
from operating import autopilot_reading
from operating import autopilot_records
from operator_api.tests import test_autopilot_area as base


class AssistRealFixesTests(unittest.TestCase):
    def test_visible_batches_decide_calls(self) -> None:
        uid = "11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as td:
            q = ap.Queue(Path(td) / "q.sqlite")
            try:
                for idx in range(5):
                    q.conn.execute(
                        "INSERT INTO items (item_id, event_id, autopilot, owner, title, body, "
                        "audiences, markings, citations, due_at, created_at, state) "
                        "VALUES (?, ?, 'desk', ?, ?, '{}', ?, '[]', ?, "
                        "'2026-11-26T00:00:00Z', '2026-11-25T00:00:00Z', 'open')",
                        (f"ap_{idx}", f"ev_{idx}", uid, f"Item {idx}", json.dumps([f"mail:{uid}"]),
                         json.dumps([f"graph-mail://x/m{idx}@1"])),
                    )
                q.conn.commit()
                calls: list[list[str]] = []

                def decide(handles):
                    calls.append(list(handles))
                    return {h: (None if not h.endswith("m2@1") else "denied") for h in handles}

                got = autopilot_reading.visible(
                    q,
                    reader=f"employee:{uid}",
                    may_read=lambda _a: True,
                    held=frozenset(),
                    decide=decide,
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(calls[0]), 5)
                self.assertEqual([i["item_id"] for i in got], ["ap_0", "ap_1", "ap_3", "ap_4"])

                # visible_one only checks the single item's handle
                calls.clear()
                one = autopilot_reading.visible_one(
                    q,
                    "ap_1",
                    reader=f"employee:{uid}",
                    may_read=lambda _a: True,
                    held=frozenset(),
                    decide=decide,
                )
                self.assertIsNotNone(one)
                self.assertEqual(calls, [["graph-mail://x/m1@1"]])
            finally:
                q.close()

    def test_body_texts_prefers_full_text_over_preview(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT, superseded_at TEXT);"
            "CREATE TABLE text_fields (field_id INTEGER PRIMARY KEY, citation TEXT, field_path TEXT);"
            "CREATE TABLE text_chunks (field_id INTEGER, start INTEGER, body BLOB);"
        )
        full = "Full message text well beyond 255 characters: " + ("detail " * 60)
        conn.execute("INSERT INTO object_versions VALUES ('Email:1', 'graph-mail://m/1@1', NULL)")
        conn.execute("INSERT INTO text_fields VALUES (1, 'graph-mail://m/1@1', 'row.body_text')")
        conn.execute("INSERT INTO text_chunks VALUES (1, 0, ?)", (zlib.compress(full.encode("utf-8")),))
        st = SimpleNamespace(conn=conn)
        bodies = autopilot_records.body_texts(st, ["graph-mail://m/1@1"])
        self.assertEqual(bodies["graph-mail://m/1@1"], full)
        row = {
            "body_preview": {"value": full[:255], "l1_handle": "graph-mail://m/1@1"},
        }
        self.assertEqual(autopilot_records.message_body(row, bodies), full)
        # Fallback to preview only when full text is absent
        self.assertEqual(autopilot_records.message_body(row, {}), full[:255])
        conn.close()

    def test_job_piece_uses_job_moment_in_checker(self) -> None:
        got = SimpleNamespace(
            reply="",
            ask=None,
            todos=[],
            proposals=[{"tool": "plx.prepare_write", "arguments": {"system": "teams", "operation": "chat_message"}}],
            note="",
        )
        text = autopilot_check.prompt(
            got,
            'This is one piece of a whole job Alex handed you: "Reply to Jordan on Teams"',
            ["biz.teams_messages row with chat id 19:abc"],
            "Alex",
            look=False,
            prepared=True,
        )
        self.assertIn("explicitly handed the assistant a job", text)
        self.assertNotIn("A message just arrived for them", text)


class AutopilotDecideNoAreaRebuildTests(base.AutopilotArea):
    def test_decide_does_not_rebuild_entire_area(self) -> None:
        with patch.object(self.backend, "_area_read", side_effect=AssertionError("_area_read should not run")):
            got = self.client(base.OWNER).post("/api/autopilot/items/ap_own/decision", json={"decision": "reject"})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["state"], "rejected")
