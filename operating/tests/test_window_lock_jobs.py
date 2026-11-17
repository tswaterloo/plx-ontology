"""Window locks at the jobs' entry points: each refuses (exit 0, no work; the nightly backup
exits 75, so a refused night never reads as a backup taken) while a hold blocks
it (ontology/window_lock.py; the lock itself is tested in ontology/tests/test_window_lock.py)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from ontology import window_lock as wl
from ontology.tests.test_window_lock import REPO, WindowCase, idle_units  # noqa: F401


class EntryPoints(WindowCase):
    """Each job's entry point returns 0 and does no work while a hold blocks it."""

    def held(self, job):
        self.take("mig", blocks=job, reason="test")

    def run_main(self, main, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        try:
            line = json.loads(text)
        except ValueError:
            line = json.loads(text.splitlines()[-1])
        self.assertEqual((line["skipped"], line["held_by"]), ("window", "mig"))
        return line

    def no_work(self, *_a, **_k):
        raise AssertionError("the job ran while its window was held")

    def test_delta_run_and_compact(self):
        from ontology import delta
        state = self.tmp / "state"
        for job, cmd in (("delta", "run"), ("delta-compact", "compact")):
            self.held(job)
            with mock.patch.object(delta, "build_delta", self.no_work):
                line = self.run_main(delta.main, [cmd, "--state-dir", str(state),
                                                  "--mirror", str(self.tmp / "m.sqlite")])
            self.assertEqual(line["command"], cmd)
            wl.release("mig")
            wl.unguard(job)

    def test_delta_loop_stops_when_a_window_is_taken_mid_loop(self):
        from ontology import delta
        cycles = []

        taker = []

        def one_cycle(*_a, **_k):
            cycles.append(1)
            if len(cycles) == 2:     # the cycle is running: the window writes its hold, then waits for it
                thread = threading.Thread(target=lambda: taker.append(
                    self.take("mig", blocks="delta", wait_idle_seconds=30, poll_seconds=0.01)))
                thread.start()
                taker.append(thread)
                for _ in range(3000):
                    if wl.refusal("delta"):
                        break
                    time.sleep(0.01)
            return {"started_at": "x", "status": "ok"}

        out = io.StringIO()
        with mock.patch.object(delta, "build_delta", one_cycle), \
                contextlib.redirect_stdout(out):
            code = delta.main(["run", "--state-dir", str(self.tmp / "state"), "--mirror",
                               str(self.tmp / "m.sqlite"), "--loop", "0.01"])
        taker[0].join(30)
        self.assertEqual((code, len(cycles)), (0, 2))
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["skipped"], "window")
        self.assertEqual(taker[1]["busy"], [])   # the window got its hold once the cycle ended

    def test_broken_window_lock_never_stops_a_job(self):
        from operating import proactive_incremental
        ran = []
        with mock.patch.object(wl, "skip_report", side_effect=RuntimeError("broken")), \
                mock.patch.object(proactive_incremental, "run_pass",
                                  lambda *a, **k: ran.append(1) or {}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(proactive_incremental.main(["--state-dir", str(self.tmp)]), 0)
        self.assertEqual(ran, [1])
        self.assertIn("window check failed", err.getvalue())

    def test_pg_follow(self):
        from operating.pg import ontology_delta
        self.held("pg-follow")
        with mock.patch.object(ontology_delta, "follow", self.no_work):
            self.run_main(ontology_delta.main, ["--follow", str(self.tmp / "state"),
                                                "--dsn", "postgresql://none/none"])

    def test_proactive_incremental(self):
        from operating import proactive_incremental
        self.held("proactive")
        with mock.patch.object(proactive_incremental, "run_pass", self.no_work):
            self.run_main(proactive_incremental.main, ["--state-dir", str(self.tmp)])

    def test_vendor_watch(self):
        from operating import host, vendor_watch
        self.held("proactive")
        with mock.patch.object(vendor_watch, "run_pass", self.no_work), \
                mock.patch.object(host.HostConfig, "from_file", self.no_work):
            self.run_main(vendor_watch.main, ["--state-dir", str(self.tmp), "--config",
                                              str(self.tmp / "none.json")])

    def test_search_follow(self):
        from operating.embed import follow
        self.held("search-follow")
        with mock.patch.object(follow, "follow", self.no_work):
            self.run_main(follow.main, ["--state", str(self.tmp), "--mode", "layers"])

    def test_backup_script(self):
        tree = self.tmp / "tree"
        (tree / "deploy" / "backup").mkdir(parents=True)
        shutil.copy(REPO / "deploy" / "backup" / "backup.sh", tree / "deploy" / "backup")
        marker = self.tmp / "ran"
        (tree / "deploy" / "backup" / "plx_backup.py").write_text(
            f"open({str(marker)!r}, 'w').write('x')\n")
        env = {**os.environ, wl.ENV_DIR: str(self.dir), "PLX_PYTHON": sys.executable,
               "PYTHONPATH": str(REPO)}

        def run(env_):
            marker.unlink(missing_ok=True)
            r = subprocess.run(["bash", str(tree / "deploy" / "backup" / "backup.sh")],
                               env=env_, capture_output=True, text=True, timeout=60)
            return r.returncode, marker.exists(), r.stdout + r.stderr

        self.held("backup")
        code, ran, text = run(env)
        self.assertEqual((code, ran), (wl.EXIT_HELD, False))
        self.assertIn("backup: refused, held by mig", text)
        # a hold taken after the check: guard-exec refuses with 75 too, never 0
        script = tree / "deploy" / "backup" / "backup.sh"
        script.write_text("\n".join(line for line in script.read_text().splitlines()
                                    if "window_lock check backup" not in line) + "\n")
        try:
            code, ran, text = run(env)
        finally:
            shutil.copy(REPO / "deploy" / "backup" / "backup.sh", script)
        self.assertEqual((code, ran), (wl.EXIT_HELD, False))
        self.assertIn("backup: refused, held by mig", text)
        wl.release("mig")
        self.assertEqual(run(env)[:2], (0, True))
        # a running backup holds its lock: a window taken meanwhile reports it busy
        done = self.tmp / "done"
        (tree / "deploy" / "backup" / "plx_backup.py").write_text(
            f"import os,time\nopen({str(marker)!r}, 'w').write('x')\n"
            f"while not os.path.exists({str(done)!r}): time.sleep(0.05)\n")
        marker.unlink(missing_ok=True)
        proc = subprocess.Popen(["bash", str(tree / "deploy" / "backup" / "backup.sh")],
                                env=env)
        try:
            for _ in range(400):
                if marker.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            with self.assertRaises(wl.WindowBusy):
                self.take("mig", blocks="backup", wait_idle_seconds=0.2, poll_seconds=0.05)
        finally:
            done.touch()
            self.assertEqual(proc.wait(timeout=30), 0)
        wl.release("mig")
        self.held("backup")      # check itself broken (module missing): backup still runs
        code, ran, text = run({**env, "PYTHONPATH": str(self.tmp / "nowhere")})
        self.assertEqual((code, ran), (0, True))
        self.assertIn("window check failed", text)


if __name__ == "__main__":
    unittest.main()
