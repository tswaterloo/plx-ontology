"""Tests for Ask P0 freshness validation, /api/ask error mapping, OAuth 401 detection,
and warm subprocess auto-recycle."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import get_args
import unittest
from unittest import mock

from pydantic import ValidationError

from operator_api import agent_ask as aa
from operator_api import models as m
from operator_api import simple_ask as sa


@dataclass
class _FakeRun:
    final_text: str
    events: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class _FakeSession:
    def __init__(self, replies: list[_FakeRun]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.alive = True
        self.closed = 0

    def turn(self, prompt: str, on_line, cancel):
        self.prompts.append(prompt)
        run = self.replies.pop(0)
        if run.usage.get("is_error"):
            self.alive = False
        return run

    def close(self) -> None:
        self.alive = False
        self.closed += 1


class FreshnessMappingTests(unittest.TestCase):
    def test_alarm_and_view_systems_all_match_models_system_literal(self) -> None:
        valid = set(get_args(m.System))
        self.assertEqual(sa._ALARM_SYSTEMS["capture:mc"], "MC")
        self.assertEqual(sa._ALARM_SYSTEMS["capture:sharepoint"], "File")
        self.assertEqual(sa._ALARM_SYSTEMS["capture:sharepoint_lists"], "File")
        self.assertEqual(sa._VIEW_SYSTEMS["sharepoint_files"], "File")
        for mapping in (sa._ALARM_SYSTEMS, sa._VIEW_SYSTEMS):
            for key, label in mapping.items():
                self.assertIn(label, valid, f"{key} -> {label!r} not in models.System")

    def test_ask_freshness_maps_mc_and_sharepoint_and_skips_invalid_system(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            alarm_dir = Path(td) / "ops-alarms"
            alarm_dir.mkdir(parents=True)
            (alarm_dir / "status.json").write_text(json.dumps({
                "checked_at": "2026-11-26T01:00:00Z",
                "checks": [
                    {"name": "capture:mc", "state": "ok",
                     "newest_record_at": "2026-11-26T00:55:00Z"},
                    {"name": "capture:sharepoint", "state": "ok",
                     "newest_record_at": "2026-11-26T00:50:00Z"},
                    {"name": "capture:sharepoint_lists", "state": "ok",
                     "newest_record_at": "2026-11-26T00:52:00Z"},
                ],
            }), encoding="utf-8")
            sa._FRESHNESS_CACHE.clear()
            backend = SimpleNamespace(state_dir=Path(td), store=None)
            ran = sa.SimpleRun(text="ok", agent_text="ok", unverified=[], events=[],
                               seconds=0.5, stopped=False, failure=None)
            fresh = sa._ask_freshness(backend, ran)
            by_sys = {s.system: s for s in fresh.sources}
            self.assertEqual(set(by_sys), {"MC", "File"})
            self.assertEqual(by_sys["MC"].as_of, "2026-11-26T00:55:00Z")
            self.assertEqual(by_sys["File"].as_of, "2026-11-26T00:52:00Z")

            # Unexpected system label logs a warning and is skipped instead of crashing Ask
            bad_sources = [
                {"system": "UnknownSource", "as_of": "2026-11-26T00:55:00Z", "stale": False},
                {"system": "MC", "as_of": "2026-11-26T00:55:00Z", "stale": False},
            ]
            with mock.patch.object(sa, "_live_freshness_sources", return_value=bad_sources), \
                    self.assertLogs("operator_api.ask", "WARNING") as logs:
                fresh2 = sa._ask_freshness(backend, ran)
            self.assertEqual([s.system for s in fresh2.sources], ["MC"])
            self.assertIn("ask_freshness_skipped_system", "".join(logs.output))


class AskErrorMappingTests(unittest.TestCase):
    def test_validation_error_and_internal_value_error_map_to_internal_incident(self) -> None:
        from fastapi.testclient import TestClient
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        from operator_api.mock import MockBackend

        for exc_kind in ("validation", "value"):
            with self.subTest(exc_kind=exc_kind):
                backend = MockBackend()

                def boom(who, body, *, _kind=exc_kind):
                    if _kind == "validation":
                        m.FreshnessSource(system="Mission Control")  # raises ValidationError
                    raise ValueError("internal parser mismatch")
                    yield  # pragma: no cover

                backend.ask = boom
                app = create_app(
                    backend,
                    DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                    secure_cookie=False,
                )
                client = TestClient(app, base_url="http://testserver")
                self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
                with client.stream("POST", "/api/ask", json={"question": "test?"}) as resp:
                    self.assertEqual(resp.status_code, 200)
                    body = "".join(resp.iter_text())
                self.assertIn('"code": "internal"', body)
                self.assertNotIn('"code": "invalid_request"', body)
                self.assertNotIn("wasn't understood", body)


class CredentialsDigestAndWarmRecycleTests(unittest.TestCase):
    def test_start_and_scope_digest_rotate_when_credentials_file_changes(self) -> None:
        import contextlib
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "host.json"
            cfg.write_text("{}")
            claude_dir = Path(td) / ".claude"
            claude_dir.mkdir()
            cred = claude_dir / ".credentials.json"
            cred.write_text('{"access_token": "tok1"}')
            asker = SimpleNamespace(host_config=str(cfg))

            class _PG:
                def execute(self, sql, params=None):
                    if "to_regclass" in sql:
                        return SimpleNamespace(fetchone=lambda: (None,))
                    raise AssertionError(sql)

            @contextlib.contextmanager
            def _connect(**_):
                yield _PG()

            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir)}), \
                    mock.patch("operator_api.agent_ask.ticket_claims", lambda s: {"sub": "ann"}), \
                    mock.patch("operating.ontology_tools_sql.served_views",
                               lambda names: frozenset(names)), \
                    mock.patch.object(sa, "system_prompt", lambda: "prompt"), \
                    mock.patch("operating.pg.pool.connect", _connect):
                s1 = sa.start_digest(asker, object())
                sc1 = sa.scope_digest(asker, object())
                # Atomic replace (new inode) as plx-claude-cred-sync does on rotation
                tmp_cred = claude_dir / ".credentials.json.new"
                tmp_cred.write_text('{"access_token": "tok2"}')
                os.replace(tmp_cred, cred)
                s2 = sa.start_digest(asker, object())
                sc2 = sa.scope_digest(asker, object())
            self.assertNotEqual(s1, s2)
            self.assertNotEqual(sc1, sc2)

    def test_errored_turn_never_runs_redo_or_check_and_is_not_kept(self) -> None:
        warm = sa.WarmSessions()
        sess = _FakeSession([
            _FakeRun("Something broke.", usage={"is_error": True, "exit_code": 1}),
        ])
        asker = SimpleNamespace(
            _reader_key=lambda s: "r1",
            _scope_digest=lambda s: "d1",
            _start_digest=lambda s: "s1",
            _host_for=lambda s: SimpleNamespace(close=lambda: None),
            _agent_for=lambda on: SimpleNamespace(open_session=lambda h: sess),
        )
        redo_called: list[int] = []
        check_called: list[int] = []
        with mock.patch.object(sa, "spares_per_reader", return_value=0):
            ran = warm.turn(
                asker, object(), "c1", "q?", (), lambda line: None, None,
                redo=lambda r: redo_called.append(1) or "repair",
                check=lambda r, h: check_called.append(1) or ("check", sa.covered),
            )
        self.assertEqual(redo_called, [])
        self.assertEqual(check_called, [])
        self.assertEqual(len(warm), 0)
        self.assertEqual(sess.closed, 1)
        self.assertTrue(ran.usage["is_error"])

    def test_ready_session_sign_in_401_auto_recycles_evicts_spares_and_retries_once(self) -> None:
        warm = sa.WarmSessions()
        stale_ready = _FakeSession([
            _FakeRun("API Error: 401 OAuth access token has expired. Please re-authenticate.",
                     usage={"is_error": True, "exit_code": 1}),
        ])
        stale_extra_spare = _FakeSession([])
        fresh_cold = _FakeSession([
            _FakeRun("Here are the 3 open orders.", usage={"subtype": "success"}),
        ])
        opened_sessions = [stale_ready, stale_extra_spare, fresh_cold]
        asker = SimpleNamespace(
            _reader_key=lambda s: "r1",
            _scope_digest=lambda s: "d1",
            _start_digest=lambda s: "s1",
            _host_for=lambda s: SimpleNamespace(close=lambda: None),
            _agent_for=lambda on: SimpleNamespace(
                open_session=lambda h, prestart=False: opened_sessions.pop(0)
            ),
        )
        # Seed two prewarmed ready spares with stale tokens
        with mock.patch.object(sa, "spares_per_reader", return_value=2):
            self.assertTrue(warm.prepare(asker, object(), wait=True))
            self.assertTrue(warm.prepare(asker, object(), wait=True))
        self.assertEqual(warm.ready_count("r1"), 2)

        with mock.patch.object(sa, "spares_per_reader", return_value=0), \
                self.assertLogs("operator_api.ask", "WARNING") as logs:
            ran = warm.turn(asker, object(), "c1", "open orders?", (), lambda line: None, None)

        self.assertEqual(ran.final_text, "Here are the 3 open orders.")
        self.assertEqual(ran.usage["session_start"], "new")
        self.assertEqual(ran.usage["spare_miss"], "sign_in_retry")
        self.assertEqual(stale_ready.closed, 1)
        self.assertEqual(stale_extra_spare.closed, 1)
        self.assertEqual(warm.ready_count("r1"), 0)
        self.assertEqual(len(fresh_cold.prompts), 1)
        self.assertIn("ask_warm_sign_in_retry", "".join(logs.output))
        warm.close_all()

    def test_kept_session_sign_in_401_retries_once_and_does_not_loop_if_still_failing(self) -> None:
        warm = sa.WarmSessions()
        kept_sess = _FakeSession([
            _FakeRun("First answer.", usage={"subtype": "success"}),
            _FakeRun("OAuth access token has expired", usage={"is_error": True, "exit_code": 1}),
        ])
        cold_also_401 = _FakeSession([
            _FakeRun("OAuth access token has expired", usage={"is_error": True, "exit_code": 1}),
        ])
        opened_sessions = [kept_sess, cold_also_401]
        asker = SimpleNamespace(
            _reader_key=lambda s: "r1",
            _scope_digest=lambda s: "d1",
            _start_digest=lambda s: "s1",
            _host_for=lambda s: SimpleNamespace(close=lambda: None),
            _agent_for=lambda on: SimpleNamespace(
                open_session=lambda h, prestart=False: opened_sessions.pop(0)
            ),
        )
        with mock.patch.object(sa, "spares_per_reader", return_value=0):
            r1 = warm.turn(asker, object(), "c1", "first?", (), lambda line: None, None)
            self.assertEqual(r1.final_text, "First answer.")
            hist = [SimpleNamespace(role="user", text="first?"),
                    SimpleNamespace(role="assistant", text="First answer.")]
            r2 = warm.turn(asker, object(), "c1", "second?", hist, lambda line: None, None)

        self.assertEqual(len(cold_also_401.prompts), 1)
        self.assertIn("first?", cold_also_401.prompts[0])
        self.assertEqual(aa.session_failure(r2.final_text, r2.usage, r2.events, None), "sign_in")
        self.assertEqual(kept_sess.closed, 1)
        self.assertEqual(cold_also_401.closed, 1)
        self.assertEqual(len(warm), 0)
        # User-facing payload stays honest and clear
        payload = sa.answer_payload(
            sa.SimpleRun(text=r2.final_text, agent_text=r2.final_text, unverified=[],
                         events=[], seconds=1.0, stopped=False, failure="sign_in"),
            "second?", "c1", 100, "a" * 24,
        )
        self.assertIn("signed in again", payload.direct_answer)
        self.assertNotIn("wasn't understood", payload.direct_answer)
        self.assertNotIn("\u2014", payload.direct_answer)


if __name__ == "__main__":
    unittest.main()
