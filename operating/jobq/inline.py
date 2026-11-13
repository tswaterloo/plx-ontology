"""A job its own caller runs: ``tools/land.sh``, ``tools/testslot.py`` and
``python3 -m operating.jobq run --class <class> -- <command>``.

The caller enqueues an inline job, waits (renewing its waiter lease, woken by ``NOTIFY
jobq`` or a short poll) until admission hands it its own job, then runs the command in a
new process group with ``PLX_JOBQ_JOB=<id>`` in its environment while a heartbeat thread
renews the lease. When the queue marks the job ``preempting``, or the job is no longer
this caller's, the command's group gets SIGTERM, then SIGKILL after ``hygiene.GRACE_S``.
When the command ends, its group and every process still carrying the tag are ended
(``hygiene.sweep``), and the outcome is recorded; a preempted run is queued again and run
again from the start. A preemptible class's command runs with ``oom_score_adj`` 800, so a
kernel OOM takes it before the jobs above it.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from . import hygiene
from . import queue as jq

ENABLED_ENV = "PLX_JOBQ"
POLL_S = 2.0
OOM_SCORE_ADJ = 800


class Duplicate(Exception):
    """A job of the same key is already queued by another caller."""


class Cancelled(Exception):
    """The queued job was cancelled (by hand, or as a dead waiter) before it started."""


class WaitTimeout(Exception):
    pass


def enabled(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(ENABLED_ENV, "") in ("1", "true", "yes", "on")


def _say(message: str) -> None:
    print(f"jobq: {message}", file=sys.stderr, flush=True)


class Ticket:
    """One inline job from enqueue to finish."""

    def __init__(self, queue: jq.Queue, class_name: str, *, key: str | None = None,
                 label: str | None = None, poll_s: float = POLL_S,
                 wait_timeout: float | None = None,
                 say: Callable[[str], None] = _say):
        self.queue = queue
        self.cls = queue.classes[class_name]
        self.key, self.label = key, label
        self.poll_s, self.wait_timeout, self.say = poll_s, wait_timeout, say
        self.owner = f"inline:{queue.host}:{os.getpid()}:{threading.get_ident()}"
        self.job_id: int | None = None
        self.proc: subprocess.Popen | None = None
        self.preempted = threading.Event()
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None
        self._listen = None

    # ---------------------------------------------------------------- waiting
    def _wake(self, timeout: float) -> None:
        if self._listen is None:
            try:
                import psycopg
                self._listen = psycopg.connect(self.queue.conninfo, autocommit=True,
                                               connect_timeout=10, application_name="plx-jobq-wait")
                self._listen.execute(f"LISTEN {jq.CHANNEL}")
            except Exception:  # noqa: BLE001 - the poll still wakes us
                self._listen = False
        if self._listen:
            try:
                for _ in self._listen.notifies(timeout=timeout, stop_after=1):
                    pass
                return
            except Exception:  # noqa: BLE001
                self._listen = False
        time.sleep(timeout)

    def wait(self) -> dict[str, Any]:
        """Block until this job is admitted; the claimed row."""
        if self.job_id is None:
            job_id, what = self.queue.enqueue(self.cls.name, key=self.key, label=self.label,
                                              kind="inline", waiter_pid=os.getpid())
            if what != "queued":
                raise Duplicate(f"a {self.cls.name} job with key {self.key!r} is already queued"
                                f" (job {job_id})")
            self.job_id = job_id
        started, said = time.monotonic(), None
        while True:
            decision = self.queue.try_admit(self.owner, lambda j: j["id"] == self.job_id,
                                            renew=[self.job_id], pid=os.getpid())
            if decision.claimed is not None:
                waited = time.monotonic() - started
                if waited >= 1:
                    self.say(f"job {self.job_id} ({self.cls.name}) started after {waited:.0f} s")
                self.preempted.clear()
                self.lost.clear()
                return decision.claimed
            row = self.queue.get(self.job_id)
            if row is None or row["state"] != "queued":
                raise Cancelled(f"job {self.job_id} is {row['state'] if row else 'gone'}")
            reason = decision.reason if decision.blocked else (
                "waiting for higher-priority jobs" if decision.reserved else
                "waiting for a free slot in its class" if not decision.busy else None)
            if reason and reason != said:
                self.say(f"job {self.job_id} ({self.cls.name}) waits: {reason}")
                said = reason
            if self.wait_timeout is not None and time.monotonic() - started > self.wait_timeout:
                self.queue.cancel(self.job_id, "wait timeout")
                raise WaitTimeout(f"job {self.job_id} not admitted within {self.wait_timeout:g} s")
            self._wake(0.2 if decision.busy else self.poll_s)

    # ---------------------------------------------------------------- running
    def env(self, base: Mapping[str, str]) -> dict[str, str]:
        return {**base, hygiene.TAG_ENV: str(self.job_id)}

    def attach(self, proc: subprocess.Popen) -> None:
        """The command started: record its group, raise its OOM score when preemptible,
        and start the heartbeat."""
        self.proc = proc
        if self.cls.preemptible:
            with contextlib.suppress(OSError):
                Path(f"/proc/{proc.pid}/oom_score_adj").write_text(str(OOM_SCORE_ADJ))
        with contextlib.suppress(Exception):
            self.queue.attach(self.job_id, self.owner, pgid=proc.pid)
        self._stop.clear()
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)
        self._beat.start()

    def _heartbeat(self) -> None:
        every = max(0.2, self.queue.lease_s / 6)
        while not self._stop.wait(every):
            try:
                state = self.queue.heartbeat(self.job_id, self.owner)
            except jq.Lost:
                self.lost.set()
                self._terminate("the job is no longer this caller's")
                return
            except Exception:  # noqa: BLE001 - a Postgres hiccup: the reaper spares a live owner
                continue
            if state == "preempting":
                self.preempted.set()
                self._terminate("preempted for a higher-priority job")
                return

    def _terminate(self, why: str) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self.say(f"job {self.job_id}: stopping the command: {why}")
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + hygiene.GRACE_S
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)

    def finish(self, code: int) -> str:
        """Stop the heartbeat, end every leftover process of the job, record the outcome;
        the job's next state (``queued``: preempted, run it again)."""
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=5)
        swept = hygiene.sweep(self.job_id, self.proc.pid if self.proc else None)
        if swept:
            self.say(f"job {self.job_id}: ended {swept} leftover process(es)")
        if self.lost.is_set():
            return "lost"
        ok = code == 0 and not self.preempted.is_set()
        try:
            state = self.queue.finish(self.job_id, self.owner, ok, exit_code=code,
                                      result="success" if ok else "preempted"
                                      if self.preempted.is_set() else "exit-code",
                                      error=None if ok else f"exit {code}")
        except jq.Lost:
            return "lost"
        self.proc = None
        return state

    def close(self) -> None:
        self._stop.set()
        if self._listen:
            with contextlib.suppress(Exception):
                self._listen.close()
        if self.job_id is not None:
            with contextlib.suppress(Exception):
                row = self.queue.get(self.job_id)
                if row and row["state"] == "queued":
                    self.queue.cancel(self.job_id, "caller left")


def popen_kwargs() -> dict[str, Any]:
    """A new process group (not a new session: a test run keeps its session's nice)."""
    if sys.version_info >= (3, 11):
        return {"process_group": 0}
    return {"preexec_fn": os.setpgrp}  # noqa: PLW1509 - before any thread of ours runs


def run(command: Sequence[str], class_name: str, *, key: str | None = None,
        label: str | None = None, environ: Mapping[str, str] = os.environ,
        queue: jq.Queue | None = None, spawn: Callable[..., subprocess.Popen] | None = None,
        wait_timeout: float | None = None, say: Callable[[str], None] = _say) -> int:
    """Run `command` as an inline job of `class_name`; its exit status (a signal as 128 +
    its number). Raises Duplicate, Cancelled, WaitTimeout, or the queue's connection
    error (the caller decides whether to run without the queue)."""
    own = queue is None
    queue = queue or jq.Queue(environ=environ)
    ticket = Ticket(queue, class_name, key=key, label=label, wait_timeout=wait_timeout, say=say)
    spawn = spawn or (lambda cmd, env: subprocess.Popen(list(cmd), env=env, **popen_kwargs()))
    forwarded = {}

    def forward(signum: int, _frame: object) -> None:
        if ticket.proc is not None and ticket.proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(ticket.proc.pid, signum)
        else:
            raise KeyboardInterrupt

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                forwarded[signum] = signal.signal(signum, forward)
        while True:
            ticket.wait()
            try:
                proc = spawn(command, ticket.env(environ))
            except OSError as exc:
                ticket.finish(127)
                say(f"cannot start {command[0]!r}: {exc}")
                return 127
            ticket.attach(proc)
            code = proc.wait()
            code = 128 - code if code < 0 else code
            state = ticket.finish(code)
            if state == "queued" and ticket.preempted.is_set():
                say(f"job {ticket.job_id} was preempted; it runs again when admitted")
                continue
            if state == "lost":
                say(f"job {ticket.job_id} was taken from this caller; its run is void")
                return code or 1
            return code
    finally:
        for signum, handler in forwarded.items():
            signal.signal(signum, handler)
        ticket.close()
        if own:
            queue.close()
