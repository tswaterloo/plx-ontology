"""The host job queue (operating.jobq) on a scratch Postgres database: claim races, lease
expiry, retries and the dead letter, admission under low memory, strict priority,
preemption, idempotency, and that every process a job spawned ends with it."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from operating.jobq import classes as jc
from operating.jobq import hygiene, inline, status as js
from operating.jobq import queue as jq
from operating.jobq.worker import UnitState, Worker
from tools import testslot

try:
    from operating.pg import scratch_db
    import psycopg  # noqa: F401
    scratch_db.use_test_cluster()
    _NAME, _DSN = scratch_db.create_db()
except Exception:  # noqa: BLE001 - no local Postgres: skipped
    _NAME = _DSN = None

GB = 1024
CLASSES = {
    "delta": jc.JobClass("delta", 10, 24, 12, 1, unit="plx-delta.service", backoff_s=30,
                         preempt_after_s=0.01),
    "search": jc.JobClass("search", 30, 8, 6, 1, unit="plx-search-follow.service",
                          preemptible=True, max_attempts=3, backoff_s=0.01),
    "many": jc.JobClass("many", 30, 1, 0.1, 100, unit="plx-many.service"),
    "three": jc.JobClass("three", 30, 1, 0.1, 3, unit="plx-three.service"),
    "tests": jc.JobClass("tests", 60, 4, 1, 15, preemptible=True, max_attempts=1,
                         max_runtime_s=540),
    "land": jc.JobClass("land", 50, 1, 1, 1, max_attempts=1, max_runtime_s=540),
    "backup": jc.JobClass("backup", 40, 3, 2, 1, unit="plx-backup.service"),
    "manual": jc.JobClass("manual", 20, 16, 4, 1, unit="plx-proactive.service", queued=False),
}


class FakeProbe(jq.Probe):
    def __init__(self, avail_mb: int = 60 * GB):
        self.avail = avail_mb
        self.units: dict[str, int] = {}

    def mem_available_mb(self) -> int:
        return self.avail

    def unit_memory_mb(self, unit: str) -> int | None:
        return self.units.get(unit)


class FakeExecutor:
    """systemd as the worker sees it: start gives a new invocation; tests set the end."""

    def __init__(self) -> None:
        self.units: dict[str, UnitState] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.n = 0

    def state(self, unit: str) -> UnitState:
        return self.units.get(unit, UnitState("inactive", "success", "", 0))

    def start(self, unit: str) -> str:
        self.n += 1
        self.units[unit] = UnitState("activating", "success", f"inv{self.n}", None)
        self.started.append(unit)
        return f"inv{self.n}"

    def stop(self, unit: str) -> None:
        self.stopped.append(unit)

    def end(self, unit: str, result: str = "success") -> None:
        state = self.units[unit]
        self.units[unit] = UnitState("failed" if result != "success" else "inactive", result,
                                     state.invocation, 0 if result == "success" else 1)


def tearDownModule() -> None:
    if _NAME:
        scratch_db.drop_db(_NAME)


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = {jq.FLOOR_ENV: "4", jq.CPU_ENV: "64"}
        self.probe = FakeProbe()
        self.q = self.queue()
        self.q.conn.execute("DROP SCHEMA IF EXISTS jobq CASCADE")
        self.q.init()
        self.ex = FakeExecutor()
        self.state = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        self.q.close()

    def queue(self, **kw) -> jq.Queue:
        return jq.Queue(_DSN, classes=kw.pop("classes", CLASSES), probe=self.probe,
                        environ=self.env, **kw)

    def worker(self, q: jq.Queue | None = None) -> Worker:
        return Worker(q or self.q, self.ex, owner="worker:test", state_dir=self.state,
                      say=lambda m: None)

    def states(self) -> dict[int, str]:
        return {r["id"]: r["state"] for r in self.q.conn.execute("SELECT id, state FROM jobq.jobs")}

    # ---------------------------------------------------------------- claims
    def test_concurrent_claimers_take_each_job_once_at_every_size(self):
        self.probe.avail = 200 * GB                     # memory never limits this one
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.q.conn.execute("TRUNCATE jobq.jobs")
                ids = [self.q.enqueue("many", key=f"k{n}-{i}")[0] for i in range(n)]
                claimed: list[int] = []
                lock = threading.Lock()

                def claimer(k: int) -> None:
                    q = self.queue()
                    while True:
                        d = q.try_admit(f"c{k}", lambda j: True)
                        if d.claimed:
                            with lock:
                                claimed.append(d.claimed["id"])
                        elif not d.busy:
                            break                  # a full pass found nothing due
                    q.close()

                threads = [threading.Thread(target=claimer, args=(k,)) for k in range(8)]
                started = time.monotonic()
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(sorted(claimed), sorted(ids))     # none lost, none twice
                self.assertTrue(all(s == "running" for s in self.states().values()))
                self.assertLess(time.monotonic() - started, 30)

    def test_class_concurrency_holds_under_racing_claimers(self):
        for i in range(10):
            self.q.enqueue("three", key=f"t{i}")
        claimed: list[int] = []

        def claimer(k: int) -> None:
            q = self.queue()
            for _ in range(30):
                d = q.try_admit(f"c{k}", lambda j: True)
                if d.claimed:
                    claimed.append(d.claimed["id"])
            q.close()

        threads = [threading.Thread(target=claimer, args=(k,)) for k in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(claimed), 3)
        self.assertEqual(list(self.states().values()).count("running"), 3)

    def test_one_queued_and_one_running_per_key_and_once(self):
        a, what = self.q.enqueue("delta")
        self.assertEqual(what, "queued")
        self.assertEqual(self.q.enqueue("delta"), (a, "coalesced"))
        self.assertEqual(self.q.try_admit("w", lambda j: True).claimed["id"], a)
        b, what = self.q.enqueue("delta")              # a follow-up while one runs
        self.assertEqual(what, "queued")
        self.assertIsNone(self.q.try_admit("w", lambda j: True).claimed)  # never two at once
        self.q.finish(a, "w", True)
        self.assertEqual(self.q.try_admit("w", lambda j: True).claimed["id"], b)
        self.q.finish(b, "w", True)
        self.assertEqual(self.q.enqueue("delta", key="backup:2026-11-12", once=True)[1], "queued")
        job = self.q.try_admit("w", lambda j: True).claimed
        self.q.finish(job["id"], "w", True)
        self.assertEqual(self.q.enqueue("delta", key="backup:2026-11-12", once=True),
                         (None, "done_once"))

    def test_unknown_class_kind_or_override_fails_closed(self):
        with self.assertRaises(jc.UnknownClass):
            self.q.enqueue("rm-rf")
        with self.assertRaises(jc.UnknownClass):
            self.q.enqueue("manual")                    # accounted only, never queued
        with self.assertRaises(ValueError):
            self.q.enqueue("tests")                     # no unit to start
        with self.assertRaises(ValueError):
            self.q.enqueue("delta", kind="shell")
        bad = Path(tempfile.mkdtemp()) / "c.json"
        for body in ({"nope": {"mem_gb": 1}}, {"delta": {"unit": "x.service"}},
                     {"delta": {"mem_gb": "1"}}, {"delta": {"max_concurrent": 0}}, [1]):
            bad.write_text(json.dumps(body))
            with self.assertRaises(ValueError):
                jc.load({jc.OVERRIDES_ENV: str(bad)})
        bad.write_text(json.dumps({"delta": {"mem_gb": 20}}))
        self.assertEqual(jc.load({jc.OVERRIDES_ENV: str(bad)})["delta"].mem_mb, 20 * GB)

    # ---------------------------------------------------------------- admission
    def test_low_memory_refuses_with_reason_and_counts_what_running_jobs_may_take(self):
        self.probe.avail = 10 * GB                     # 10G - 4G floor < 24G
        a, _ = self.q.enqueue("delta")
        d = self.q.try_admit("w", lambda j: True)
        self.assertIsNone(d.claimed)
        self.assertEqual(d.blocked["id"], a)
        self.assertIn("memory", d.reason)
        row = self.q.get(a)
        self.assertEqual((row["state"], row["refusals"]), ("queued", 1))
        self.assertIn("needs 24576 MB", row["last_refusal"])
        self.probe.avail = 40 * GB
        self.assertEqual(self.q.try_admit("w", lambda j: True).claimed["id"], a)
        # the running delta holds 6G of its 24G: 18G more is promised, so 40-18-4 < 8+... no search
        self.probe.units["plx-delta.service"] = 6 * GB
        self.probe.avail = 28 * GB
        s, _ = self.q.enqueue("search")
        d = self.q.try_admit("w", lambda j: True)
        self.assertIsNone(d.claimed)
        self.assertEqual(d.blocked["id"], s)
        self.probe.units["plx-delta.service"] = 20 * GB    # grown: only 4G more promised
        self.probe.avail = 18 * GB
        self.assertEqual(self.q.try_admit("w", lambda j: True).claimed["id"], s)

    def test_a_class_unit_started_outside_the_queue_is_counted(self):
        self.probe.units["plx-proactive.service"] = 2 * GB   # 14G more may come
        self.probe.avail = 30 * GB
        self.q.enqueue("delta")
        self.assertIsNone(self.q.try_admit("w", lambda j: True).claimed)
        self.probe.units["plx-delta.service"] = 1 * GB       # a manual delta: no second one
        self.probe.avail = 200 * GB
        self.assertIsNone(self.q.try_admit("w", lambda j: True).claimed)
        del self.probe.units["plx-delta.service"]
        self.assertIsNotNone(self.q.try_admit("w", lambda j: True).claimed)

    def test_strict_priority_no_lower_job_passes_a_blocked_head(self):
        t, _ = self.q.enqueue("many", key="low")            # priority 30, 1G
        time.sleep(0.01)
        self.q.conn.execute("UPDATE jobq.jobs SET priority = 60 WHERE id = %s", (t,))
        d_id, _ = self.q.enqueue("delta")                  # priority 10, 24G, enqueued later
        self.probe.avail = 20 * GB                          # tests would fit, delta does not
        d = self.q.try_admit("w", lambda j: True)
        self.assertIsNone(d.claimed)
        self.assertEqual(d.blocked["id"], d_id)
        self.assertEqual(self.q.get(t)["state"], "queued")
        self.probe.avail = 60 * GB
        self.assertEqual(self.q.try_admit("w", lambda j: True).claimed["id"], d_id)
        self.assertEqual(self.q.try_admit("w", lambda j: True).claimed["id"], t)

    def test_short_jobs_pass_a_blocked_head_backups_and_alerts_do_not(self):
        d_id, _ = self.q.enqueue("delta")                  # 24G: blocked
        b_id, _ = self.q.enqueue("backup")                 # 3G, long: waits behind it
        t_id, _ = self.q.enqueue("tests", kind="inline", waiter_pid=os.getpid())
        self.probe.avail = 11 * GB                         # backup (3G) would fit too: it waits
        d = self.q.try_admit("t", lambda j: j["id"] == t_id, renew=[t_id])
        self.assertEqual(d.blocked["id"], d_id)
        self.assertEqual(d.claimed["id"], t_id)            # 11 - 4 >= 4: fits beside the floor
        self.assertEqual(self.q.get(b_id)["state"], "queued")
        self.assertEqual(self.q.get(b_id)["refusals"], 0)  # never considered past the head
        # a second one (11 - 4 promised - 4 < 4) does not fit beside the floor: it waits
        t2, _ = self.q.enqueue("tests", kind="inline", waiter_pid=os.getpid())
        d = self.q.try_admit("t", lambda j: j["id"] == t2, renew=[t2])
        self.assertIsNone(d.claimed)
        self.assertEqual(d.blocked["id"], d_id)
        self.assertIn("memory", self.q.get(t2)["last_refusal"])
        self.assertTrue(CLASSES["land"].short and CLASSES["tests"].short)
        self.assertFalse(jc.load({})["backup"].short or jc.load({})["proactive-inc"].short)

    def test_a_fitting_job_of_another_caller_is_reserved_not_skipped(self):
        a, _ = self.q.enqueue("delta")                     # the worker's, 24G
        self.probe.avail = 34 * GB                          # 34-24-4 = 6: one 4G test fits after it
        ticket_job, _ = self.q.enqueue("tests", kind="inline", waiter_pid=os.getpid())
        mine = self.q.try_admit("t", lambda j: j["id"] == ticket_job, renew=[ticket_job])
        self.assertEqual(mine.reserved, [a])
        self.assertEqual(mine.claimed["id"], ticket_job)
        self.probe.avail = 30 * GB                          # now the test would not fit after delta
        self.q.finish(ticket_job, "t", True)
        t2, _ = self.q.enqueue("tests", kind="inline", waiter_pid=os.getpid())
        d = self.q.try_admit("t", lambda j: j["id"] == t2, renew=[t2])
        self.assertIsNone(d.claimed)
        self.assertEqual(d.blocked["id"], t2)

    def test_a_dead_waiter_is_cancelled_and_never_blocks_the_head(self):
        dead, _ = self.q.enqueue("tests", kind="inline", waiter_pid=os.getpid())
        self.q.conn.execute("UPDATE jobq.jobs SET lease_until = now() - interval '1 s',"
                            " priority = 1 WHERE id = %s", (dead,))
        a, _ = self.q.enqueue("delta")
        self.assertEqual(self.q.try_admit("w", lambda j: True).claimed["id"], a)
        self.assertEqual(self.q.get(dead)["state"], "cancelled")

    # ---------------------------------------------------------------- leases, retries
    def test_unit_job_failure_retries_with_backoff_then_dead_letter_then_retry(self):
        cls = {**CLASSES, "search": replace(CLASSES["search"], backoff_s=2)}
        q = self.queue(classes=cls)
        w = self.worker(q)
        job, _ = q.enqueue("search")
        for attempt in (1, 2, 3):
            q.conn.execute("UPDATE jobq.jobs SET run_after = now() WHERE id = %s", (job,))
            w.run_pass()
            self.assertEqual(q.get(job)["state"], "running")
            self.ex.end("plx-search-follow.service", "exit-code")
            w.run_pass()
            row = q.get(job)
            self.assertEqual(row["attempts"], attempt)
            if attempt < 3:
                self.assertEqual(row["state"], "queued")
                wait = q.conn.execute("SELECT extract(epoch FROM run_after - now()) AS s"
                                      " FROM jobq.jobs WHERE id = %s", (job,)).fetchone()["s"]
                self.assertGreater(wait, 2 * 2 ** (attempt - 1) * 0.9)   # backoff doubles
                self.assertLessEqual(wait, 2 * 2 ** (attempt - 1) * 1.25 + 0.5)
        self.assertEqual(q.get(job)["state"], "dead")
        self.assertIn("exit-code", q.get(job)["last_error"])
        self.assertEqual(len(self.ex.started), 3)
        self.assertTrue(q.retry(job))
        w.run_pass()
        self.assertEqual(q.get(job)["state"], "running")
        q.close()

    def test_success_is_done_and_the_next_waits_for_the_cooldown(self):
        cls = {**CLASSES, "delta": replace(CLASSES["delta"], cooldown_s=300)}
        q = self.queue(classes=cls)
        w = self.worker(q)
        job, _ = q.enqueue("delta")
        w.run_pass()
        self.ex.end("plx-delta.service", "success")
        w.run_pass()
        self.assertEqual(q.get(job)["state"], "done")
        nxt, _ = q.enqueue("delta")
        w.run_pass()
        self.assertEqual(q.get(nxt)["state"], "queued")
        self.assertEqual(self.ex.started, ["plx-delta.service"])
        q.close()

    def test_lapsed_unit_lease_is_adopted_while_its_activation_runs(self):
        other = Worker(self.q, self.ex, owner="worker:old", say=lambda m: None)
        job, _ = self.q.enqueue("delta")
        other.admit()
        self.q.conn.execute("UPDATE jobq.jobs SET lease_until = now() - interval '1 s'")
        w = self.worker()
        w.run_pass()
        row = self.q.get(job)
        self.assertEqual((row["state"], row["owner"]), ("running", "worker:test"))
        with self.assertRaises(jq.Lost):              # the old owner is fenced off
            self.q.finish(job, "worker:old", True)
        self.ex.end("plx-delta.service", "oom-kill")
        w.run_pass()
        self.assertEqual(self.q.get(job)["state"], "queued")
        self.assertEqual(self.q.get(job)["result"], "oom-kill")

    def test_lapsed_unit_lease_whose_activation_is_gone_is_a_failed_attempt(self):
        other = Worker(self.q, self.ex, owner="worker:old", say=lambda m: None)
        job, _ = self.q.enqueue("delta")
        other.admit()
        self.ex.units["plx-delta.service"] = UnitState("inactive", "success", "someone-else", 0)
        self.q.conn.execute("UPDATE jobq.jobs SET lease_until = now() - interval '1 s'")
        self.worker().reap()
        row = self.q.get(job)
        self.assertEqual((row["state"], row["result"]), ("queued", "lost"))

    def test_lapsed_inline_lease_of_a_live_caller_is_spared_a_dead_one_is_swept(self):
        live, _ = self.q.enqueue("tests", kind="inline", waiter_pid=os.getpid())
        self.q.try_admit("me", lambda j: j["id"] == live, pid=os.getpid())
        # a caller that died, leaving a child carrying its job's tag
        dead, _ = self.q.enqueue("tests", kind="inline")
        self.q.try_admit("gone", lambda j: j["id"] == dead)
        parent = subprocess.Popen([sys.executable, "-c", "pass"])
        parent.wait()
        orphan = subprocess.Popen(["sleep", "300"], start_new_session=True,
                                  env={**os.environ, hygiene.TAG_ENV: str(dead)})
        self.q.conn.execute("UPDATE jobq.jobs SET pid = %s, pid_start = 1, lease_until ="
                            " now() - interval '1 s' WHERE id IN (%s, %s)", (parent.pid, live, dead))
        self.q.conn.execute("UPDATE jobq.jobs SET pid = %s, pid_start = %s WHERE id = %s",
                            (os.getpid(), hygiene.process_start(os.getpid()), live))
        self.worker().reap()
        self.assertEqual(self.q.get(live)["state"], "running")
        self.assertEqual(self.q.get(dead)["state"], "failed")
        self.assertEqual(self.q.get(dead)["result"], "owner-gone")
        self.assertIsNotNone(orphan.wait(timeout=30))

    # ---------------------------------------------------------------- preemption
    def test_blocked_head_preempts_lower_preemptible_unit_job_gracefully(self):
        w = self.worker()
        s, _ = self.q.enqueue("search")
        w.run_pass()
        self.assertEqual(self.q.get(s)["state"], "running")
        self.probe.units["plx-search-follow.service"] = 7 * GB
        self.probe.avail = 25 * GB          # 25 - 1 promised - 4 floor < 24: delta blocked
        d, _ = self.q.enqueue("delta")
        w.run_pass()                        # blocked: noted
        time.sleep(0.02)
        w.run_pass()                        # past preempt_after: search marked
        self.assertEqual(self.q.get(s)["state"], "preempting")
        w.run_pass()
        self.assertEqual(self.ex.stopped, ["plx-search-follow.service"])
        self.ex.end("plx-search-follow.service", "signal")
        del self.probe.units["plx-search-follow.service"]
        self.probe.avail = 32 * GB
        w.run_pass()
        row = self.q.get(s)
        self.assertEqual((row["state"], row["attempts"], row["preemptions"]), ("queued", 0, 1))
        self.assertEqual(self.q.get(d)["state"], "running")

    def test_no_preemption_when_it_would_not_make_the_head_fit_or_of_a_higher_class(self):
        w = self.worker()
        s, _ = self.q.enqueue("search")
        w.run_pass()
        self.probe.units["plx-search-follow.service"] = 1 * GB
        self.probe.avail = 6 * GB           # even without search, delta (24G) would not fit
        self.q.enqueue("delta")
        for _ in range(3):
            w.run_pass()
            time.sleep(0.02)
        self.assertEqual(self.q.get(s)["state"], "running")

    def test_memory_pressure_preempts_the_running_test_run_and_it_runs_again(self):
        q = self.queue(lease_s=1.2)
        w = self.worker(q)
        marker = self.state / "runs"
        script = (f"import time,pathlib; p=pathlib.Path({str(marker)!r});"
                  " p.write_text(p.read_text()+'x' if p.exists() else 'x');"
                  " n=len(p.read_text()); time.sleep(60 if n == 1 else 0)")
        out: dict[str, int] = {}
        runner = threading.Thread(target=lambda: out.setdefault("code", inline.run(
            [sys.executable, "-c", script], "tests", queue=self.queue(lease_s=1.2),
            say=lambda m: None)))
        runner.start()
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        self.probe.avail = 1 * GB            # below half the floor
        w.preempt()
        self.probe.avail = 60 * GB
        runner.join(timeout=60)
        self.assertFalse(runner.is_alive())
        self.assertEqual(out["code"], 0)
        self.assertEqual(marker.read_text(), "xx")                  # run again from the start
        rows = q.conn.execute("SELECT state, preemptions FROM jobq.jobs").fetchall()
        self.assertEqual([(r["state"], r["preemptions"]) for r in rows], [("done", 1)])
        q.close()

    # ---------------------------------------------------------------- hygiene
    def test_inline_job_ends_every_process_it_spawned(self):
        pidfile = self.state / "orphan.pid"
        script = ("import subprocess,sys;"
                  "p=subprocess.Popen(['sleep','300'],start_new_session=True);"
                  f"open({str(pidfile)!r},'w').write(str(p.pid))")
        code = inline.run([sys.executable, "-c", script], "tests", queue=self.queue(),
                          say=lambda m: None)
        self.assertEqual(code, 0)
        pid = int(pidfile.read_text())
        for _ in range(100):
            if not hygiene._alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(hygiene._alive(pid), "the detached grandchild outlived its job")
        row = self.q.conn.execute("SELECT state, exit_code FROM jobq.jobs").fetchone()
        self.assertEqual((row["state"], row["exit_code"]), ("done", 0))

    def test_inline_failure_is_failed_not_retried_and_land_runs_one_at_a_time(self):
        code = inline.run([sys.executable, "-c", "raise SystemExit(3)"], "tests",
                          queue=self.queue(), say=lambda m: None)
        self.assertEqual(code, 3)
        self.assertEqual(list(self.states().values()), ["failed"])
        first = inline.Ticket(self.queue(), "land", key="land:a")
        first.wait()
        second = inline.Ticket(self.queue(), "land", key="land:b", wait_timeout=0.5, poll_s=0.1)
        with self.assertRaises(inline.WaitTimeout):
            second.wait()
        with self.assertRaises(inline.Duplicate):
            dup = inline.Ticket(self.queue(), "tests", key="same")
            other = inline.Ticket(self.queue(), "tests", key="same")
            self.probe.avail = 0
            threading.Thread(target=lambda: self._swallow(dup.wait), daemon=True).start()
            time.sleep(0.3)
            other.wait()
        self.probe.avail = 60 * GB
        first.finish(0)
        self.assertEqual(self.q.get(first.job_id)["state"], "done")

    @staticmethod
    def _swallow(fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- status
    def test_status_snapshot_reports_depth_running_waits_refusals_and_staleness(self):
        w = self.worker()
        self.q.enqueue("delta")
        w.run_pass()
        self.probe.avail = 5 * GB
        self.q.enqueue("search")
        w.run_pass()
        snap = js.read_snapshot(self.state)
        self.assertTrue(snap["fresh"])
        self.assertEqual(snap["queued"], 1)
        self.assertEqual([j["class"] for j in snap["running"]], ["delta"])
        self.assertEqual(snap["waiting"][0]["refusals"], 1)
        self.assertIn("memory", snap["waiting"][0]["last_refusal"])
        self.assertEqual(snap["last_hour"].get("refused"), 1)
        self.assertNotIn("command", json.dumps(snap))
        from operating.jobq import events as ev
        evq = ev.EventQueue(_DSN, topic="autopilot")
        evq.publish([{"event_id": "e1", "event_type": "task.assigned", "group_key": "task:1",
                      "priority": 20}])
        evq.close()
        text = js.render(self.q.status())
        self.assertIn("waiting (1)", text)
        self.assertIn("events autopilot: 1 ready", text)
        from datetime import datetime, timedelta, timezone
        later = datetime.now(timezone.utc) + timedelta(seconds=js.STALE_S + 5)
        self.assertFalse(js.read_snapshot(self.state, now=later)["fresh"])
        # a check that took ``now`` just before the worker wrote (11-13: a false page)
        earlier = datetime.now(timezone.utc) - timedelta(seconds=js.CLOCK_SKEW_S - 5)
        self.assertTrue(js.read_snapshot(self.state, now=earlier)["fresh"])
        self.assertEqual(js.read_snapshot(self.state, now=earlier)["age_s"], 0)
        long_before = datetime.now(timezone.utc) - timedelta(seconds=js.CLOCK_SKEW_S + 60)
        self.assertFalse(js.read_snapshot(self.state, now=long_before)["fresh"])
        self.assertEqual(js.read_snapshot(Path(tempfile.mkdtemp()))["configured"], False)


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class TestslotThroughTheQueueTests(unittest.TestCase):
    """tools/testslot.py with PLX_JOBQ=1: a test run is a ``tests`` job of the queue."""

    def setUp(self) -> None:
        q = jq.Queue(_DSN, environ={})
        q.conn.execute("DROP SCHEMA IF EXISTS jobq CASCADE")
        q.init()
        self.q = q
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {**os.environ, "PLX_JOBQ": "1", jq.DSN_ENV: _DSN,
                    "PLX_TEST_SLOT_DIR": str(self.tmp / "slots"), "PLX_TEST_SLOTS": "2"}
        for name in ("mem_available_mb", "unit_memory_mb"):
            patch = mock.patch.object(jq.Probe, name,
                                      return_value=200 * GB if name == "mem_available_mb" else None)
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self.q.close()

    def test_run_is_a_tests_job_and_its_detached_children_end_with_it(self):
        pidfile = self.tmp / "child.pid"
        script = ("import os,subprocess;"
                  "p=subprocess.Popen(['sleep','300'],start_new_session=True);"
                  f"open({str(pidfile)!r},'w').write(str(p.pid));"
                  "raise SystemExit(0 if os.environ.get('PLX_JOBQ_JOB') else 9)")
        code = testslot.run([sys.executable, "-c", script], environ=self.env)
        self.assertEqual(code, 0)
        rows = self.q.conn.execute("SELECT class, kind, state FROM jobq.jobs").fetchall()
        self.assertEqual([(r["class"], r["kind"], r["state"]) for r in rows],
                         [("tests", "inline", "done")])
        pid = int(pidfile.read_text())
        for _ in range(100):
            if not hygiene._alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(hygiene._alive(pid))

    def test_a_failing_run_keeps_its_status_and_a_blocked_run_times_out_as_124(self):
        self.assertEqual(testslot.run([sys.executable, "-c", "raise SystemExit(5)"],
                                      environ=self.env), 5)
        jq.Probe.mem_available_mb.return_value = 1 * GB
        env = {**self.env, "PLX_TEST_SLOT_WAIT": "1"}
        self.assertEqual(testslot.run([sys.executable, "-c", "pass"], environ=env),
                         testslot.SLOT_TIMEOUT_EXIT)
        states = sorted(r["state"] for r in self.q.conn.execute("SELECT state FROM jobq.jobs"))
        self.assertEqual(states, ["cancelled", "failed"])

    def test_queue_off_or_in_a_gate_check_never_touches_it(self):
        for env in ({**self.env, "PLX_JOBQ": ""}, {**self.env, testslot.GATE_CHECK: "1"}):
            self.assertEqual(testslot.run([sys.executable, "-c", "pass"], environ=env), 0)
        self.assertIsNone(self.q.conn.execute("SELECT 1 FROM jobq.jobs").fetchone())


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class LandLaneTests(unittest.TestCase):
    """tools/land.sh with PLX_JOBQ=1: one landing at a time, in order, through the queue."""

    def test_an_unreachable_queue_lands_without_it(self):
        tmp = Path(tempfile.mkdtemp())
        env = {**os.environ, "PLX_JOBQ": "1", jq.DSN_ENV: "host=/nonexistent dbname=x connect_timeout=1",
               "LAND_LOGS": str(tmp / "logs"), "LAND_WORKDIR": str(tmp / "wt")}
        env.pop("PLX_JOBQ_LAND_HELD", None)
        land = Path(__file__).resolve().parents[2] / "tools" / "land.sh"
        done = subprocess.run(["bash", str(land), "main", "-m", "x"], env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 1)
        self.assertIn("job queue unreachable; landing without it", done.stderr)
        self.assertIn("the lane is main", done.stdout)

    def test_a_second_landing_waits_in_the_lane_then_runs(self):
        q = jq.Queue(_DSN, environ={})
        self.addCleanup(q.close)
        q.conn.execute("DROP SCHEMA IF EXISTS jobq CASCADE")
        q.init()
        tmp = Path(tempfile.mkdtemp())
        zero = tmp / "classes.json"     # the host's real memory never decides this test
        zero.write_text(json.dumps({c.name: {"mem_gb": 0, "cpus": 0}
                                    for c in jc.load({}).values()}))
        env = {**os.environ, "PLX_JOBQ": "1", jq.DSN_ENV: _DSN, jq.FLOOR_ENV: "0",
               jc.OVERRIDES_ENV: str(zero), "LAND_LOGS": str(tmp / "logs"),
               "LAND_WORKDIR": str(tmp / "wt")}
        env.pop("PLX_JOBQ_LAND_HELD", None)
        holder = inline.Ticket(jq.Queue(_DSN, environ=env), "land", key="land:other",
                               say=lambda m: None)
        self.addCleanup(holder.queue.close)
        holder.wait()
        land = Path(__file__).resolve().parents[2] / "tools" / "land.sh"
        proc = subprocess.Popen(["bash", str(land), "main", "-m", "x"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            row = q.conn.execute("SELECT state FROM jobq.jobs WHERE idem_key = 'land:main'").fetchone()
            if row:
                break
            time.sleep(0.05)
        time.sleep(0.5)
        self.assertEqual(row["state"], "queued")
        self.assertIsNone(proc.poll())
        holder.finish(0)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1)                 # land.sh's own refusal, unchanged
        self.assertIn("NOT LANDED", out)
        self.assertIn("the lane is main", out)
        self.assertEqual(q.conn.execute("SELECT state FROM jobq.jobs WHERE idem_key ="
                                        " 'land:main'").fetchone()["state"], "failed")


class ProbeTests(unittest.TestCase):
    def test_an_empty_leftover_cgroup_is_no_running_unit(self):
        root = Path(tempfile.mkdtemp())
        for unit, populated in (("live.service", 1), ("left.service", 0)):
            (root / unit).mkdir()
            (root / unit / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
            (root / unit / "memory.current").write_text(str(3 << 30))
        self.assertEqual(hygiene.unit_memory_mb("live.service", root), 3 * GB)
        self.assertIsNone(hygiene.unit_memory_mb("left.service", root))
        self.assertIsNone(hygiene.unit_memory_mb("gone.service", root))


if __name__ == "__main__":
    unittest.main()
