"""Window locks: per-window holds the jobs check at start (ontology/window_lock.py).

Temp directories only; systemd is injected (``unit_state``), never called."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ontology import window_lock as wl

REPO = Path(__file__).resolve().parents[2]


def idle_units(_units):
    return {}


class WindowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wl-"))
        self.dir = self.tmp / "windows"
        self.env = mock.patch.dict(os.environ, {wl.ENV_DIR: str(self.dir)})
        self.env.start()
        for job in list(wl._RUNNING):
            wl.unguard(job)

    def tearDown(self):
        for job in list(wl._RUNNING):
            wl.unguard(job)
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def take(self, window, **kw):
        kw.setdefault("unit_state", idle_units)
        return wl.take(window, **kw)

    def cli(self, *argv, unit_state=idle_units):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wl.main(list(argv), unit_state=unit_state)
        return code, out.getvalue(), err.getvalue()


class HoldRules(WindowCase):
    def test_hold_refuses_and_release_frees(self):
        self.assertIsNone(wl.refusal("delta"))
        self.take("mig-1", blocks="delta,pg-follow", reason="schema step 3")
        text = wl.refusal("delta")
        self.assertRegex(text, r"^held by mig-1 \(schema step 3, since .* (ET|UTC)\)$")
        self.assertIsNone(wl.refusal("backup"))          # not blocked by this window
        self.assertTrue(wl.release("mig-1"))
        self.assertFalse(wl.release("mig-1"))           # idempotent
        self.assertIsNone(wl.refusal("delta"))

    def test_default_blocks_every_job(self):
        self.take("maint")
        for job in wl.JOBS:
            self.assertIsNotNone(wl.refusal(job), job)

    def test_releasing_one_window_keeps_the_other_blocking(self):
        """GA-F3: one window's exit must not re-arm a job another window still blocks."""
        self.take("train", blocks="delta,delta-compact", reason="migration train")
        self.take("ga-f3", blocks="delta", reason="one step")
        wl.release("ga-f3")
        self.assertRegex(wl.refusal("delta"), r"^held by train ")
        self.assertRegex(wl.refusal("delta-compact"), r"^held by train ")
        wl.release("train")
        self.assertIsNone(wl.refusal("delta"))

    def test_same_window_refuses_unless_reenter_by_same_pid_or_replace(self):
        self.take("mig", blocks="delta", pid=os.getpid())
        with self.assertRaises(wl.WindowHeld):
            self.take("mig", blocks="delta", pid=os.getpid())
        with self.assertRaises(wl.WindowHeld):
            self.take("mig", blocks="delta", pid=1, reenter=True)
        self.take("mig", blocks="delta,backup", pid=os.getpid(), reenter=True)
        self.assertIsNotNone(wl.refusal("backup"))
        self.take("mig", blocks="proactive", pid=1, replace=True)
        self.assertIsNone(wl.refusal("delta"))
        self.assertIsNotNone(wl.refusal("proactive"))

    def test_dead_holder_keeps_its_hold(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.take("mig", blocks="delta", reason="half done", pid=proc.pid)
        text = wl.refusal("delta")
        self.assertIn(f"holder pid {proc.pid} gone", text)
        code, out, _ = self.cli("check", "delta")
        self.assertEqual(code, 75)
        self.assertIn("gone", out)

    def test_lease_expiry_unblocks_and_dead_holder_blocks_until_then(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.take("short", blocks="delta", pid=proc.pid, lease_seconds=0.4)
        self.assertIn(f"holder pid {proc.pid} gone", wl.refusal("delta"))   # still blocks
        time.sleep(0.5)
        self.assertIsNone(wl.refusal("delta"))
        state = json.loads(self.cli("status")[1])["holds"][0]["state"]
        self.assertEqual(state, f"short: expired (holder pid {proc.pid} gone)")
        self.take("short", blocks="delta")               # an expired hold is retaken
        self.assertIsNotNone(wl.refusal("delta"))

    def test_every_hold_has_a_bounded_lease(self):
        hold = self.take("w", blocks="delta")["hold"]
        lease = (datetime.fromisoformat(hold["expires"])
                 - datetime.fromisoformat(hold["started"]))
        self.assertEqual(lease, timedelta(minutes=60))
        for bad in (0, -1, 1441):
            with self.assertRaises(wl.UsageError):
                self.take("w2", lease_minutes=bad)
        self.assertEqual(self.cli("take", "w3", "--lease-min", "2000")[0], 2)

    def test_renew_extends_and_missing_window_fails(self):
        self.take("w", blocks="delta", lease_seconds=0.3)
        self.assertTrue(wl.renew("w", lease_minutes=5))
        time.sleep(0.4)
        self.assertIsNotNone(wl.refusal("delta"))
        self.assertFalse(wl.renew("nothere"))
        self.assertFalse((self.dir / "holds" / "nothere.json").exists())
        self.assertEqual(self.cli("renew", "nothere")[0], 1)
        self.assertEqual(self.cli("renew", "w", "--lease-min", "10")[0], 0)

    def test_hold_without_readable_expiry_blocks(self):
        self.take("w", blocks="delta")
        path = self.dir / "holds" / "w.json"
        hold = json.loads(path.read_text())
        del hold["expires"]
        path.write_text(json.dumps(hold))
        self.assertIsNotNone(wl.refusal("delta"))

    def test_unreadable_hold_blocks_everything(self):
        (self.dir / "holds").mkdir(parents=True)
        (self.dir / "holds" / "broken.json").write_text("{not json")
        self.assertRegex(wl.refusal("backup"), r"held by broken \(unreadable hold file")

    def test_unknown_names_refuse(self):
        with self.assertRaises(wl.UsageError):
            wl.refusal("detla")
        with self.assertRaises(wl.UsageError):
            self.take("mig", blocks="delta,detla")
        for bad in ("", "-x", "a/b", "../x", "x" * 65, "a b"):
            with self.assertRaises(wl.UsageError, msg=bad):
                self.take(bad)
        self.assertFalse((self.dir / "holds").exists() and any((self.dir / "holds").iterdir()))
        self.assertEqual(self.cli("check", "detla")[0], 2)
        self.assertEqual(self.cli("take", "mig", "--blocks", "delta,detla")[0], 2)
        self.assertEqual(self.cli("take", "../etc")[0], 2)
        self.assertEqual(self.cli("release", "a/b")[0], 2)


class Ordering(WindowCase):
    def test_running_job_makes_take_wait_then_refuse_76_holding_nothing(self):
        self.assertIsNone(wl.guard("delta"))             # job started first: LOCK_SH kept
        t0 = time.monotonic()
        with self.assertRaises(wl.WindowBusy) as caught:
            self.take("mig", blocks="delta,backup", wait_idle_seconds=0.3, poll_seconds=0.05)
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        self.assertEqual(caught.exception.busy, ["delta"])
        self.assertIsNone(wl.refusal("delta"))           # refused: nothing stays held
        self.assertIn("refused: still running", str(caught.exception))
        wl.unguard("delta")                               # the job finished
        self.take("mig", blocks="delta", replace=True)    # now idle: no wait
        self.assertIsNotNone(wl.guard("delta", log=io.StringIO()))   # next start refused

    def test_running_lock_held_by_another_process(self):
        code = ("import sys,time; from ontology import window_lock as w;"
                "print(w.guard('pg-follow'), flush=True); time.sleep(30)")
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, stdout=subprocess.PIPE,
                                text=True, env={**os.environ, wl.ENV_DIR: str(self.dir)})
        try:
            self.assertEqual(proc.stdout.readline().strip(), "None")
            code_, _, err = self.cli("take", "mig", "--blocks", "pg-follow",
                                     "--wait-idle-min", "0.005")
            self.assertEqual(code_, 76)
            self.assertIn("pg-follow", err)
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
        self.assertEqual(self.cli("check", "pg-follow")[0], 0)

    def test_the_cli_waits_20_minutes_by_default_and_no_wait_opts_out(self):
        seen = []
        real = wl.take

        def take(window, **kw):
            seen.append(kw["wait_idle_seconds"])
            return real(window, **{**kw, "wait_idle_seconds": 0})
        with mock.patch.object(wl, "take", take):
            self.assertEqual(self.cli("take", "a")[0], 0)
            self.assertEqual(self.cli("take", "b", "--no-wait")[0], 0)
        self.assertEqual(seen, [1200, 0])

    def test_wait_idle_waits_for_legacy_units(self):
        calls = []

        def states(units):
            calls.append(list(units))
            return {"plx-delta.service": "active" if len(calls) < 3 else "inactive"}

        took = self.take("mig", blocks="delta", wait_idle_seconds=5, poll_seconds=0.01,
                         unit_state=states)
        self.assertEqual(took["busy"], [])
        self.assertEqual(len(calls), 3)
        self.assertIn("plx-delta.service", calls[0])
        with self.assertRaises(wl.WindowBusy):
            self.take("mig2", blocks="proactive", wait_idle_seconds=0.05, poll_seconds=0.01,
                      unit_state=lambda u: {"plx-proactive-inc.service": "activating"})

    def test_guard_logs_refusal(self):
        self.take("mig", blocks="search-follow", reason="reindex")
        err = io.StringIO()
        text = wl.guard("search-follow", log=err)
        self.assertRegex(err.getvalue(), r"^search-follow: refused, held by mig \(reindex")
        line = json.loads((self.dir / "refusals.log").read_text().splitlines()[-1])
        self.assertEqual((line["job"], line["held_by"], line["text"]),
                         ("search-follow", "mig", text))
        self.assertNotIn("search-follow", wl._RUNNING)    # a refused job is not "running"

    def test_broken_window_dir_fails_open(self):
        blocker = self.tmp / "file"
        blocker.write_text("x")
        err = io.StringIO()
        self.assertIsNone(wl.guard("delta", directory=blocker / "windows", log=err))
        self.assertIn("window check failed", err.getvalue())


class Cli(WindowCase):
    def test_take_check_status_release(self):
        code, out, _ = self.cli("take", "mig", "--blocks", "delta", "--reason", "r")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["pid"], os.getppid())
        self.assertEqual(self.cli("take", "mig")[0], 75)
        code, out, _ = self.cli("check", "delta")
        self.assertEqual((code, out.split(" (")[0]), (75, "held by mig"))
        self.assertEqual(self.cli("check", "backup")[0], 0)
        status = json.loads(self.cli("status")[1])
        self.assertEqual(list(status["blocked"]), ["delta"])
        self.assertEqual(self.cli("release", "mig")[0], 0)
        self.assertEqual(self.cli("check", "delta")[0], 0)

    def test_run_releases_on_success_and_failure(self):
        code, _, _ = self.cli("run", "w", "--blocks", "delta", "--", sys.executable, "-c",
                              "import sys; sys.exit(0)")
        self.assertEqual(code, 0)
        self.assertIsNone(wl.refusal("delta"))
        code, _, _ = self.cli("run", "w", "--blocks", "delta", "--", sys.executable, "-c",
                              "import sys; sys.exit(3)")
        self.assertEqual(code, 3)
        self.assertIsNone(wl.refusal("delta"))

    def test_run_keep_on_failure_keeps_hold(self):
        probe = ("import os,subprocess,sys;"
                 "r=subprocess.run([sys.executable,'-m','ontology.window_lock','check','delta']);"
                 "sys.exit(10 + r.returncode - 75)")     # held inside the window: 10
        code, _, err = self.cli("run", "w", "--blocks", "delta", "--keep-on-failure", "--",
                                sys.executable, "-c", probe)
        self.assertEqual(code, 10)
        self.assertIn("hold KEPT", err)
        self.assertRegex(wl.refusal("delta"), r"^held by w ")
        code, _, _ = self.cli("run", "w2", "--blocks", "backup", "--keep-on-failure", "--",
                              sys.executable, "-c", "pass")
        self.assertEqual(code, 0)
        self.assertIsNone(wl.refusal("backup"))

    def test_run_keeper_renews_past_one_lease(self):
        probe = ("import subprocess,sys,time; time.sleep(5.0);"
                 "r=subprocess.run([sys.executable,'-m','ontology.window_lock','check','delta']);"
                 "sys.exit(0 if r.returncode == 75 else 1)")   # still held after 3 leases
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wl.main(["run", "long", "--blocks", "delta", "--", sys.executable, "-c",
                            probe], unit_state=idle_units, lease_seconds=2.0)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIsNone(wl.refusal("delta"))           # released on exit

    def test_run_keep_on_failure_final_lease(self):
        code, _, err = self.cli("run", "w", "--blocks", "delta", "--keep-on-failure",
                                "--fail-lease-min", "30", "--", sys.executable, "-c",
                                "import sys; sys.exit(4)")
        self.assertEqual(code, 4)
        self.assertIn("final lease of 30 min", err)
        hold = wl.holds()[0]
        left = datetime.fromisoformat(hold["expires"]) - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=29) < left <= timedelta(minutes=30), left)

    def test_context_manager_keeps_and_releases(self):
        with wl.window("py", blocks="delta", lease_seconds=1.5, unit_state=idle_units):
            time.sleep(3.5)
            self.assertIsNotNone(wl.refusal("delta"))
        self.assertIsNone(wl.refusal("delta"))
        with self.assertRaises(RuntimeError):
            with wl.window("py", blocks="delta", unit_state=idle_units):
                raise RuntimeError("step failed")
        self.assertIsNone(wl.refusal("delta"))

    def test_token_guards_renew_and_release(self):
        with wl.window("py", blocks="delta", unit_state=idle_units) as took:
            self.assertFalse(wl.renew("py", token="not-the-holder"))
            self.take("py", blocks="delta", replace=True)          # someone else took it
            self.assertFalse(wl.renew("py", token=took["token"]))
        self.assertRegex(wl.refusal("delta"), r"^held by py ")  # the newer hold stays
        self.assertFalse(wl.release("py", token=took["token"]))
        self.assertTrue(wl.release("py"))                       # by name: the override

    def test_guard_exec_holds_the_running_lock_for_the_command(self):
        done = self.tmp / "done"
        script = (f"import os,time\nopen({str(self.tmp / 'up')!r},'w').close()\n"
                  f"while not os.path.exists({str(done)!r}): time.sleep(0.05)\n")
        proc = subprocess.Popen(
            [sys.executable, "-m", "ontology.window_lock", "guard-exec", "backup", "--",
             sys.executable, "-c", script], cwd=REPO,
            env={**os.environ, wl.ENV_DIR: str(self.dir)})
        try:
            for _ in range(200):
                if (self.tmp / "up").exists():
                    break
                time.sleep(0.05)
            with self.assertRaises(wl.WindowBusy) as caught:
                self.take("mig", blocks="backup", wait_idle_seconds=0.2, poll_seconds=0.05)
            self.assertEqual(caught.exception.busy, ["backup"])
        finally:
            done.touch()
            proc.wait(timeout=20)
        self.take("mig", blocks="backup", replace=True)          # idle once it ended
        marker = self.tmp / "ran"
        argv = ["guard-exec", "backup", "--", sys.executable, "-c",
                f"open({str(marker)!r},'w').close()"]
        self.assertEqual(self.cli(*argv)[0], 0)
        self.assertEqual(self.cli("guard-exec", "--exit-code", *argv[1:])[0], 75)
        self.assertFalse(marker.exists())
        self.assertEqual(self.cli("guard-exec", "detla", "--", "true")[0], 2)

    def test_run_sigterm_forwards_then_releases(self):
        script = self.tmp / "child.py"
        script.write_text("import signal,sys,time\n"
                          "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
                          "print('up', flush=True)\ntime.sleep(30)\nsys.exit(9)\n")
        proc = subprocess.Popen(
            [sys.executable, "-m", "ontology.window_lock", "run", "sig", "--no-wait", "--blocks", "delta",
             "--", sys.executable, str(script)], cwd=REPO, stdout=subprocess.PIPE, text=True,
            env={**os.environ, wl.ENV_DIR: str(self.dir)})
        self.assertIn("taken", proc.stdout.readline())
        self.assertEqual(proc.stdout.readline().strip(), "up")
        self.assertIsNotNone(wl.refusal("delta"))
        proc.terminate()
        self.assertEqual(proc.wait(timeout=20), 0)
        proc.stdout.close()
        self.assertIsNone(wl.refusal("delta"))


if __name__ == "__main__":
    unittest.main()
