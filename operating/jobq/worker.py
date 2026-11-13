"""The queue worker (``plx-jobq-worker.service``: ``python3 -m operating.jobq worker``).

Each pass, woken by ``NOTIFY jobq`` or every ``POLL_S`` seconds:
1. reaps held jobs whose lease lapsed: a unit job whose activation still runs is adopted
   (a restarted worker carries on), one that ended is recorded from its systemd
   ``Result``; an inline job whose caller is gone (its pid is dead, or reused: the start
   time differs) has its process group and tagged processes ended and is recorded failed;
2. follows its own unit jobs: renews their leases and records each activation's end
   (``Result=success`` is done; anything else is a failure: a retry after backoff, then
   the dead letter); a job marked ``preempting`` gets ``systemctl stop`` (graceful: the
   unit's SIGTERM and stop timeout);
3. preempts: under pressure (MemAvailable below half the floor) the lowest-priority
   preemptible job, one per pass; and for a head blocked longer than its class's
   ``preempt_after_s``, the lower-priority preemptible jobs whose memory makes it fit;
4. admits: claims due unit jobs while admission allows and starts each one's service with
   ``systemctl start --no-block``, keeping that activation's ``InvocationID``;
5. writes the status snapshot (``status.write_snapshot``).
The worker starts no job process itself: each unit job runs in its own service's cgroup,
which systemd ends with the oneshot.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from . import hygiene
from . import queue as jq
from . import status as js

POLL_S = 5.0
SYSTEMCTL_ENV = "PLX_JOBQ_SYSTEMCTL"
START_WAIT_S = 30.0
PRUNE_EVERY_S = 3600.0


@dataclass
class UnitState:
    active: str          # ActiveState
    result: str          # Result
    invocation: str      # InvocationID
    exit_status: int | None

    @property
    def finished(self) -> bool:
        return self.active in ("inactive", "failed")


class StartFailed(Exception):
    pass


class SystemdExecutor:
    """Starts, stops and reads system units through ``systemctl`` (``PLX_JOBQ_SYSTEMCTL``
    may name e.g. ``sudo -n systemctl``)."""

    def __init__(self, environ: Mapping[str, str] = os.environ,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.systemctl = shlex.split(environ.get(SYSTEMCTL_ENV) or "systemctl")
        self._run = run

    def _call(self, *args: str) -> subprocess.CompletedProcess:
        return self._run([*self.systemctl, *args], capture_output=True, text=True, timeout=60)

    def state(self, unit: str) -> UnitState:
        out = self._call("show", unit, "-p", "ActiveState,Result,InvocationID,ExecMainStatus")
        if out.returncode != 0:
            raise OSError(f"systemctl show {unit}: {out.stderr.strip()[:200]}")
        values = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
        try:
            status = int(values.get("ExecMainStatus", ""))
        except ValueError:
            status = None
        return UnitState(values.get("ActiveState", ""), values.get("Result", ""),
                         values.get("InvocationID", ""), status)

    def start(self, unit: str) -> str:
        """Start the unit; the InvocationID of the activation (an already active unit's own)."""
        before = self.state(unit)
        if before.active in ("active", "activating", "reloading") and before.invocation:
            return before.invocation
        out = self._call("start", "--no-block", unit)
        if out.returncode != 0:
            raise StartFailed(f"systemctl start {unit}: {out.stderr.strip()[:300]}")
        deadline = time.monotonic() + START_WAIT_S
        while time.monotonic() < deadline:
            now = self.state(unit)
            if now.invocation and now.invocation != before.invocation:
                return now.invocation
            time.sleep(0.5)
        raise StartFailed(f"{unit} did not start within {START_WAIT_S:g} s")

    def stop(self, unit: str) -> None:
        self._call("stop", "--no-block", unit)


def _say(message: str) -> None:
    print(f"jobq worker: {message}", file=sys.stderr, flush=True)


class Worker:
    def __init__(self, queue: jq.Queue, executor: Any, *, owner: str | None = None,
                 state_dir: Path | None = None, say: Callable[[str], None] = _say):
        self.queue, self.executor, self.say = queue, executor, say
        self.owner = owner or f"worker:{os.uname().nodename}"
        self.state_dir = state_dir
        self.stopping: set[int] = set()
        self.blocked_since: dict[int, float] = {}
        self.last_decision: jq.Decision | None = None
        self._pruned = 0.0

    # ---------------------------------------------------------------- 1. reap
    def reap(self) -> None:
        for job in self.queue.lapsed():
            if job["owner"] == self.owner and job["kind"] == "unit":
                self.queue.adopt(job["id"], self.owner)    # our own, after a restart
                continue
            if job["kind"] == "unit":
                self._reap_unit(job)
            else:
                self._reap_inline(job)

    def _reap_unit(self, job: dict[str, Any]) -> None:
        try:
            state = self.executor.state(job["unit"])
        except OSError as exc:
            self.say(f"job {job['id']}: cannot read {job['unit']}: {exc}")
            return
        mine = job["invocation"] and state.invocation == job["invocation"]
        if mine or (not job["invocation"] and not state.finished and state.invocation):
            if self.queue.adopt(job["id"], self.owner):
                if not job["invocation"]:
                    self.queue.attach(job["id"], self.owner, invocation=state.invocation)
                self.say(f"job {job['id']} ({job['class']}): adopted {job['unit']}")
            return
        try:
            self.queue.finish(job["id"], None, False, result="lost",
                              error="lease lapsed and the activation is gone")
        except jq.Lost:
            pass

    def _reap_inline(self, job: dict[str, Any]) -> None:
        if job["host"] != self.queue.host:
            return
        pid = job["pid"]
        if pid and job["pid_start"] and hygiene.process_start(pid) == job["pid_start"]:
            return        # the caller lives: a Postgres hiccup, not a lost job
        swept = hygiene.sweep(job["id"], job["pgid"], verify_group=True)
        try:
            self.queue.finish(job["id"], None, False, result="owner-gone",
                              error=f"caller gone; {swept} leftover process(es) ended")
            self.say(f"job {job['id']} ({job['class']}): caller gone, {swept} process(es) ended")
        except jq.Lost:
            pass

    # ---------------------------------------------------------------- 2. follow
    def follow(self) -> None:
        for job in self.queue.held(self.owner):
            if job["kind"] != "unit":
                continue
            try:
                held_state = self.queue.heartbeat(job["id"], self.owner)
            except jq.Lost:
                continue
            try:
                state = self.executor.state(job["unit"])
            except OSError as exc:
                self.say(f"job {job['id']}: cannot read {job['unit']}: {exc}")
                continue
            replaced = job["invocation"] and state.invocation != job["invocation"]
            if replaced or state.finished:
                ok = not replaced and state.result == "success"
                result = "invocation-replaced" if replaced else state.result or "unknown"
                try:
                    after = self.queue.finish(job["id"], self.owner, ok,
                                              exit_code=state.exit_status, result=result,
                                              error=None if ok else f"{job['unit']}: {result}")
                except jq.Lost:
                    continue
                self.stopping.discard(job["id"])
                self.say(f"job {job['id']} ({job['class']}) ended: {result} -> {after}")
            elif held_state == "preempting" and job["id"] not in self.stopping:
                self.executor.stop(job["unit"])
                self.stopping.add(job["id"])
                self.say(f"job {job['id']} ({job['class']}): stopping {job['unit']} (preempted)")

    # ---------------------------------------------------------------- 3. preempt
    def preempt(self) -> None:
        held = self.queue.held()
        if any(j["state"] == "preempting" for j in held):
            return                          # one at a time: let the last one free its memory
        victims = sorted((j for j in held if j["state"] == "running"
                          and self._preemptible(j) and j["preemptions"] < jq.MAX_PREEMPTIONS),
                         key=lambda j: (-j["priority"], -(j["started_at"].timestamp()
                                                         if j["started_at"] else 0)))
        if not victims:
            return
        try:
            avail = self.queue.probe.mem_available_mb()
        except OSError:
            return
        if avail < self.queue.floor_mb // 2:
            job = victims[0]
            if self.queue.preempt(job["id"], f"memory pressure: {avail} MB available"):
                self.say(f"job {job['id']} ({job['class']}) preempted: {avail} MB available")
            return
        decision = self.last_decision
        head = decision.blocked if decision else None
        if not head or not decision.reason or not decision.reason.startswith("memory"):
            return
        cls = self.queue.classes.get(head["class"])
        since = self.blocked_since.get(head["id"])
        if not cls or not cls.preempt_after_s or since is None \
                or time.monotonic() - since < cls.preempt_after_s:
            return
        shortfall = -(decision.headroom_mb or 0) + head["mem_mb"]
        chosen, freed = [], 0
        for job in victims:
            if job["priority"] <= head["priority"]:
                continue
            current = self.queue.probe.unit_memory_mb(job["unit"]) if job["unit"] else None
            freed += max(job["mem_mb"], current or 0)
            chosen.append(job)
            if freed >= shortfall:
                break
        if freed < shortfall:
            return                         # stopping them all would not make it fit
        for job in chosen:
            if self.queue.preempt(job["id"], f"job {head['id']} ({head['class']}) needs the memory"):
                self.say(f"job {job['id']} ({job['class']}) preempted for job {head['id']}")

    def _preemptible(self, job: dict[str, Any]) -> bool:
        cls = self.queue.classes.get(job["class"])
        return bool(cls and cls.preemptible)

    # ---------------------------------------------------------------- 4. admit
    def admit(self) -> int:
        started = 0
        while True:
            decision = self.queue.try_admit(self.owner, lambda j: j["kind"] == "unit")
            if decision.busy:
                return started
            self.last_decision = decision
            head = decision.blocked
            if head is not None:
                self.blocked_since.setdefault(head["id"], time.monotonic())
                self.blocked_since = {head["id"]: self.blocked_since[head["id"]]}
            else:
                self.blocked_since = {}
            job = decision.claimed
            if job is None:
                return started
            try:
                invocation = self.executor.start(job["unit"])
            except (StartFailed, OSError, subprocess.SubprocessError) as exc:
                self.say(f"job {job['id']} ({job['class']}): {exc}")
                try:
                    self.queue.finish(job["id"], self.owner, False, result="start-failed",
                                      error=str(exc)[:500])
                except jq.Lost:
                    pass
                continue
            self.queue.attach(job["id"], self.owner, invocation=invocation)
            self.say(f"job {job['id']} ({job['class']}) started {job['unit']}"
                     f" after {job['waited_s'] or 0:.0f} s in the queue")
            started += 1

    # ---------------------------------------------------------------- loop
    def run_pass(self) -> None:
        self.reap()
        self.follow()
        self.preempt()
        self.admit()
        if self.state_dir is not None:
            try:
                js.write_snapshot(self.state_dir, self.queue.status())
            except Exception as exc:  # noqa: BLE001 - the snapshot never stops the worker
                self.say(f"snapshot not written: {type(exc).__name__}")
        if time.monotonic() - self._pruned > PRUNE_EVERY_S:
            self.queue.prune()
            self._pruned = time.monotonic()

    def serve(self, poll_s: float = POLL_S, passes: int | None = None) -> None:
        import psycopg
        listen = psycopg.connect(self.queue.conninfo, autocommit=True, connect_timeout=10,
                                 application_name="plx-jobq-worker-listen")
        listen.execute(f"LISTEN {jq.CHANNEL}")
        self.say(f"serving as {self.owner}")
        done = 0
        try:
            while passes is None or done < passes:
                self.run_pass()
                done += 1
                for _ in listen.notifies(timeout=poll_s, stop_after=1):
                    pass
        finally:
            listen.close()
