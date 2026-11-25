"""Decoupled in-flight Ask runs: survive client disconnect/reload and support live reattach
and explicit Stop.

Each user Ask runs on a background thread owned by ``AskRunRegistry`` rather than by the
HTTP response generator. Closing the browser tab, reloading the page, or losing a proxy
connection only unsubscribes that HTTP reader; the run keeps going to completion and writes
its finished answer to the work log. Only an explicit Stop request sets ``run.cancel``.

A reconnecting client calls ``GET /api/ask/runs/{conversation_id}/stream`` to receive a
compact snapshot of the progress and streamed text so far, followed by live SSE events until
``answer`` or ``error``. While waiting for new events, every subscriber receives an SSE
keep-alive comment every ``KEEP_ALIVE_SECONDS`` so proxies never drop long multi-source runs.
"""

from __future__ import annotations

import contextvars
from datetime import datetime, timezone
import secrets
import threading
import time
from typing import Any, Callable, Iterator, Optional

KEEP_ALIVE = ": keep-alive\n\n"
KEEP_ALIVE_SECONDS = 15.0
FINISHED_TTL_SECONDS = 300.0
MAX_FINISHED_RUNS = 200
MAX_REPLAY_STEPS = 40

CURRENT_CANCEL: contextvars.ContextVar[Optional[threading.Event]] = contextvars.ContextVar(
    "plx_ask_cancel", default=None
)


def current_cancel() -> Optional[threading.Event]:
    """The explicit Stop cancel event for the Ask run executing on this thread, if any."""
    return CURRENT_CANCEL.get()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class AskRun:
    """One in-flight (or recently finished) Ask run for a single reader and conversation."""

    def __init__(self, reader_key: str, conversation_id: str, question: str) -> None:
        self.run_id = secrets.token_hex(12)
        self.reader_key = reader_key
        self.conversation_id = conversation_id
        self.question = question
        self.started_at = _iso_now()
        self.cancel = threading.Event()
        self.done = False
        self.finished_at: Optional[float] = None

        self._cond = threading.Condition()
        self._frames: list[str] = []
        # Compact state for late-joining reattachers and active_run summaries.
        self._understanding_frame: Optional[str] = None
        self._status_frames: list[str] = []
        self._sources_frame: Optional[str] = None
        self._verdict_frame: Optional[str] = None
        self._charts_frame: Optional[str] = None
        self._token_frame: Optional[str] = None
        self._terminal_frame: Optional[str] = None
        self._steps: list[str] = []
        self._latest_status: Optional[str] = None
        self._streamed_text: str = ""
        self._simple: bool = False

    def publish(self, event: str, payload: Any, sse_chunk: str) -> None:
        """Record one formatted SSE frame and wake all attached subscribers."""
        with self._cond:
            self._frames.append(sse_chunk)
            if event == "understanding":
                self._understanding_frame = sse_chunk
            elif event == "status":
                msg = getattr(payload, "message", None)
                if isinstance(msg, str) and msg:
                    self._latest_status = msg
                    if not self._steps or self._steps[-1] != msg:
                        self._steps.append(msg)
                        if len(self._steps) > MAX_REPLAY_STEPS:
                            self._steps = self._steps[-MAX_REPLAY_STEPS:]
                self._status_frames.append(sse_chunk)
                if len(self._status_frames) > MAX_REPLAY_STEPS:
                    self._status_frames = self._status_frames[-MAX_REPLAY_STEPS:]
            elif event == "sources":
                self._sources_frame = sse_chunk
            elif event == "verdict":
                self._verdict_frame = sse_chunk
            elif event == "charts":
                self._charts_frame = sse_chunk
                self._simple = True
            elif event == "token":
                text = str(getattr(payload, "text", "") or "")
                whole = bool(getattr(payload, "whole", False))
                if whole:
                    self._streamed_text = text
                    self._simple = True
                    self._token_frame = sse_chunk
                else:
                    self._streamed_text += text
                    # Keep latest combined text for snapshot replay.
                    self._token_frame = sse_chunk
            elif event in ("answer", "error"):
                self._terminal_frame = sse_chunk
            self._cond.notify_all()

    def finish(self) -> None:
        with self._cond:
            self.done = True
            self.finished_at = time.monotonic()
            self._cond.notify_all()

    def stop(self) -> bool:
        """Signal explicit cancellation (Stop button). Returns True if still running."""
        with self._cond:
            if self.done:
                return False
            self.cancel.set()
            self._cond.notify_all()
            return True

    def active_summary(self) -> dict[str, Any]:
        """Plain summary of an in-progress run for ``WorkLogResume.active_run``."""
        with self._cond:
            return {
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "question": self.question,
                "started_at": self.started_at,
                "status": self._latest_status,
                "steps": list(self._steps[-8:]),
                "streamed": self._streamed_text,
                "simple": self._simple,
            }

    def subscribe(self, *, snapshot: bool = False,
                  every: float = KEEP_ALIVE_SECONDS,
                  make_token_frame: Optional[Callable[[str, bool], str]] = None
                  ) -> Iterator[str]:
        """Yield SSE frames for this run, plus keep-alives during quiet stretches.

        Closing this iterator (client disconnect) only stops reading; it never cancels
        the underlying run.
        """
        cursor = 0
        if snapshot:
            initial: list[str] = []
            already_done = False
            with self._cond:
                if self._understanding_frame:
                    initial.append(self._understanding_frame)
                initial.extend(self._status_frames)
                if self._sources_frame:
                    initial.append(self._sources_frame)
                if self._verdict_frame:
                    initial.append(self._verdict_frame)
                if self._charts_frame:
                    initial.append(self._charts_frame)
                if self._streamed_text and not self._terminal_frame:
                    if make_token_frame is not None:
                        initial.append(make_token_frame(self._streamed_text, True))
                    elif self._token_frame:
                        initial.append(self._token_frame)
                if self._terminal_frame:
                    initial.append(self._terminal_frame)
                cursor = len(self._frames)
                already_done = self.done
            for chunk in initial:
                yield chunk
            if already_done:
                return

        while True:
            batch: list[str] = []
            finished = False
            with self._cond:
                if cursor < len(self._frames):
                    batch = self._frames[cursor:]
                    cursor = len(self._frames)
                elif self.done:
                    finished = True
                else:
                    self._cond.wait(timeout=every)
                    if cursor < len(self._frames):
                        batch = self._frames[cursor:]
                        cursor = len(self._frames)
                    elif self.done:
                        finished = True
            if batch:
                for chunk in batch:
                    yield chunk
                continue
            if finished:
                return
            yield KEEP_ALIVE


class AskRunRegistry:
    """Tracks in-flight and recently-completed Ask runs per ``(reader_key, conversation_id)``."""

    def __init__(self, *, finished_ttl: float = FINISHED_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._runs: dict[tuple[str, str], AskRun] = {}
        self._finished_ttl = finished_ttl

    def _prune_locked(self, now: float) -> None:
        expired = [
            key for key, run in self._runs.items()
            if run.done and run.finished_at is not None
            and now - run.finished_at > self._finished_ttl
        ]
        for key in expired:
            self._runs.pop(key, None)
        finished = [
            (run.finished_at or 0.0, key)
            for key, run in self._runs.items() if run.done
        ]
        if len(finished) > MAX_FINISHED_RUNS:
            finished.sort()
            for _ts, key in finished[:len(finished) - MAX_FINISHED_RUNS]:
                self._runs.pop(key, None)

    def start(self, reader_key: str, conversation_id: str, question: str,
              runner: Callable[[AskRun], None]) -> AskRun:
        run = AskRun(reader_key, conversation_id, question)
        key = (reader_key, conversation_id)
        with self._lock:
            self._prune_locked(time.monotonic())
            prev = self._runs.get(key)
            if prev is not None and not prev.done:
                prev.stop()
            self._runs[key] = run

        ctx = contextvars.copy_context()

        def entry() -> None:
            token = CURRENT_CANCEL.set(run.cancel)
            try:
                runner(run)
            finally:
                CURRENT_CANCEL.reset(token)
                run.finish()

        threading.Thread(
            target=ctx.run, args=(entry,), daemon=True,
            name=f"plx-ask-{conversation_id[:8]}",
        ).start()
        return run

    def get(self, reader_key: str, conversation_id: str) -> Optional[AskRun]:
        with self._lock:
            self._prune_locked(time.monotonic())
            return self._runs.get((reader_key, conversation_id))

    def active_for(self, reader_key: str) -> list[AskRun]:
        with self._lock:
            self._prune_locked(time.monotonic())
            return [
                run for (rk, _cid), run in self._runs.items()
                if rk == reader_key and not run.done
            ]

    def stop(self, reader_key: str, conversation_id: str) -> bool:
        with self._lock:
            run = self._runs.get((reader_key, conversation_id))
        return run.stop() if run is not None else False
