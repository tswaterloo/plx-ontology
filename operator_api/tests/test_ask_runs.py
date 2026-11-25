"""Decoupled in-flight Ask runs: survive disconnect/reload, support live reattach and Stop,
and keep completed answers safe from post-pass repair failures."""

from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operator_api import ask_runs, models as m, simple_ask, work_log as wl
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.backend_ask import AskMixin
from operator_api.mock import MockBackend


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        parsed = dict(ln.split(": ", 1) for ln in lines if ": " in ln)
        if "event" in parsed and "data" in parsed:
            out.append((parsed["event"], json.loads(parsed["data"])))
    return out


class SlowAskBackend(MockBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.saw_cancel = threading.Event()
        self.finished = threading.Event()

    def ask(self, reader, request: m.AskRequest):
        conv = request.conversation_id or "conv_slow"
        cancel = ask_runs.current_cancel()
        yield "status", m.StatusEvent(step="searching", message="Reading Soapbox shipments", elapsed_ms=10)
        self.started.set()
        deadline = time.monotonic() + 5.0
        while not self.release.is_set() and time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                self.saw_cancel.set()
                return
            time.sleep(0.02)
        if cancel is not None and cancel.is_set():
            self.saw_cancel.set()
            return
        yield "token", m.TokenEvent(text="Soapbox bottles split across air and boat.", whole=True)
        ans = m.AnswerPayload(
            answer_id=f"ans_{conv}",
            question=request.question,
            outcome="answered",
            direct_answer="Soapbox bottles split across air and boat.",
            hidden=m.Hidden(count=0, by_owner=[], message=None),
            freshness=m.Freshness(),
            conversation_id=conv,
            latency_ms=120,
        )
        yield "answer", ans
        self.finished.set()


class AskRunsHttpTests(unittest.TestCase):
    def _client(self, backend: MockBackend, user: str = "staff:demo") -> TestClient:
        if getattr(backend, "work_log", None) is None:
            guard = PeopleExclusion(["11111111-2222-4333-8444-555555555555"])
            guard.attach_directory([])
            backend.work_log = wl.WorkLog(None, guard=lambda: guard)
        app = create_app(backend, DevAuth(fixture_identity("plx", [user]), mode="mock"),
                         secure_cookie=False)
        client = TestClient(app, base_url="http://testserver")
        r = client.post("/api/auth/dev-login", json={})
        self.assertEqual(r.status_code, 200)
        return client

    def test_in_flight_run_survives_disconnect_lists_running_and_reattaches(self):
        backend = SlowAskBackend()
        client = self._client(backend)
        cid = "soapbox1234"

        # Start Ask on a thread and disconnect once the first event arrives (simulating reload).
        first_chunk = threading.Event()
        got_header: list[str] = []

        def start_and_drop() -> None:
            with client.stream(
                "POST",
                "/api/ask",
                json={"question": "Soapbox shipments", "conversation_id": cid},
            ) as resp:
                self.assertEqual(resp.status_code, 200)
                got_header.append(resp.headers.get("x-plx-conversation-id") or "")
                for line in resp.iter_lines():
                    if "Reading Soapbox shipments" in line:
                        first_chunk.set()
                        break  # close stream early (client reload / tab close)

        t = threading.Thread(target=start_and_drop, daemon=True)
        t.start()
        self.assertTrue(first_chunk.wait(3.0))
        self.assertEqual(got_header, [cid])

        # Backend run is still alive: /api/ask/chats shows it with running=True.
        chats = client.get("/api/ask/chats").json()
        self.assertTrue(chats["available"])
        running = [c for c in chats["chats"] if c["conversation_id"] == cid]
        self.assertEqual(len(running), 1)
        self.assertTrue(running[0]["running"])

        # Another reader cannot stream or stop this reader's run.
        other = self._client(backend, user="staff:other")
        self.assertEqual(other.get(f"/api/ask/runs/{cid}/stream").status_code, 404)
        self.assertEqual(other.post(f"/api/ask/runs/{cid}/stop").json()["stopped"], False)

        # Reopening thread mid-stream returns 200 with active_run (not 404).
        wt = client.get(f"/api/work-log/threads/ask:{cid}")
        self.assertEqual(wt.status_code, 200, wt.text)
        active = wt.json()["resume"]["active_run"]
        self.assertIsNotNone(active)
        self.assertEqual(active["conversation_id"], cid)
        self.assertEqual(active["question"], "Soapbox shipments")
        self.assertIn("Reading Soapbox shipments", active["steps"])

        # Reattach to the live SSE stream, then let the backend finish.
        def release_soon() -> None:
            time.sleep(0.08)
            backend.release.set()

        threading.Thread(target=release_soon, daemon=True).start()
        reattached = client.get(f"/api/ask/runs/{cid}/stream")
        self.assertEqual(reattached.status_code, 200, reattached.text)
        evs = _parse_sse(reattached.text)
        names = [n for n, _ in evs]
        self.assertIn("status", names)
        self.assertIn("answer", names)
        answer_ev = next(d for n, d in evs if n == "answer")
        self.assertEqual(answer_ev["direct_answer"], "Soapbox bottles split across air and boat.")
        t.join(3.0)

        # Work log now holds the completed answer (written by the decoupled runner).
        self.assertTrue(backend.finished.wait(2.0))
        after = client.get(f"/api/work-log/threads/ask:{cid}").json()
        self.assertIsNone(after["resume"]["active_run"])
        self.assertEqual(len(after["resume"]["turns"]), 1)
        self.assertEqual(
            after["resume"]["turns"][0]["answer"]["direct_answer"],
            "Soapbox bottles split across air and boat.",
        )

    def test_explicit_stop_cancels_in_flight_run(self):
        backend = SlowAskBackend()
        client = self._client(backend)
        cid = "stopme9999"

        stream_result: list[str] = []

        def run_stream() -> None:
            resp = client.post(
                "/api/ask",
                json={"question": "Long query", "conversation_id": cid},
            )
            stream_result.append(resp.text)

        t = threading.Thread(target=run_stream, daemon=True)
        t.start()
        self.assertTrue(backend.started.wait(3.0))

        stop_resp = client.post(f"/api/ask/runs/{cid}/stop")
        self.assertEqual(stop_resp.status_code, 200)
        self.assertEqual(stop_resp.json(), {"stopped": True, "conversation_id": cid})
        self.assertTrue(backend.saw_cancel.wait(3.0))
        t.join(3.0)
        self.assertTrue(stream_result)
        evs = _parse_sse(stream_result[0])
        self.assertTrue(any(n == "error" and d.get("code") == "stopped" for n, d in evs))

    def test_keep_alives_sent_during_quiet_run(self):
        run = ask_runs.AskRun("reader1", "c_ka", "Slow query")

        def finish_later() -> None:
            time.sleep(0.28)
            run.publish("answer", SimpleNamespace(), "event: answer\ndata: {}\n\n")
            run.finish()

        threading.Thread(target=finish_later, daemon=True).start()
        chunks = list(run.subscribe(every=0.08))
        self.assertGreaterEqual(chunks.count(ask_runs.KEEP_ALIVE), 2)
        self.assertEqual(chunks[-1], "event: answer\ndata: {}\n\n")

    def test_drain_responds_promptly_to_external_cancel(self):
        class Dummy(AskMixin):
            pass

        events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        fut: Future = Future()
        cancel = threading.Event()
        threading.Timer(0.15, cancel.set).start()
        started = time.monotonic()
        list(Dummy()._drain(events, fut, 60.0, cancel))
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertTrue(fut.cancelled())


class SimpleAskRepairResilienceTests(unittest.TestCase):
    def test_redo_exception_preserves_completed_answer(self):
        """If a post-answer repair turn throws after the main turn wrote a full answer,
        WarmSessions.turn keeps the completed answer rather than dropping it."""

        class FakeSession:
            alive = True

            def __init__(self) -> None:
                self.calls = 0

            def turn(self, prompt, on_line, cancel, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        final_text="Soapbox bottles: 18,000 by air and 23,000 by boat.",
                        events=[],
                        usage={"subtype": "success", "num_turns": 5},
                    )
                raise RuntimeError("session dropped during redo")

            def close(self) -> None:
                self.alive = False

        class FakeHost:
            def close(self) -> None:
                pass

        sess = FakeSession()
        asker = SimpleNamespace(
            _reader_key=lambda _s: "r1",
            _scope_digest=lambda _s: "d1",
            _start_digest=lambda _s: "d1",
            _host_for=lambda _s: FakeHost(),
            _agent_for=lambda _on: SimpleNamespace(open_session=lambda _h, **_k: sess),
        )
        warm = simple_ask.WarmSessions()
        ran = warm.turn(
            asker,
            SimpleNamespace(),
            "conv_resilient",
            "Soapbox shipments",
            [],
            lambda _line: None,
            threading.Event(),
            redo=lambda _r: ("Please fix figure", simple_ask.replaced),
        )
        self.assertEqual(ran.final_text, "Soapbox bottles: 18,000 by air and 23,000 by boat.")
        self.assertEqual(ran.usage.get("figure_repair_error"), "RuntimeError")
