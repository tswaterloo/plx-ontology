"""deploy/ops/lander/batch_land.sh, the live batch lander (isolated fixture: a temporary bare origin,
clone and scratch dir; a stand-in land-fast.sh that pushes the batch head to main)."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
LANDER = ROOT / "deploy" / "ops" / "lander" / "batch_land.sh"
PASSING = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
# args: <batch branch> <head> -m <message> -t <module>...; pushes the head to main and prints LANDED
FAKE_LAND = """#!/bin/bash
echo "$@" >> "$FAKE_ARGS"
if [ "${FAKE_PUSH_FIRST:-0}" = 1 ]; then git -C "$FAKE_REPO" push -q origin "$2:refs/heads/main"; fi
touch "$FAKE_STARTED"; sleep "${FAKE_SLEEP:-0}"
git -C "$FAKE_REPO" push -q origin "$2:refs/heads/main" 2>/dev/null; echo "LANDED ${2:0:10}"
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


class BatchLanderShellTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True,
                       capture_output=True)
        git(self.repo, "config", "user.name", "Lander")
        git(self.repo, "config", "user.email", "lander@example.invalid")
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "__init__.py").write_text("")
        (self.repo / "tests" / "test_ok.py").write_text(PASSING)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.queue = self.scratch / "landq-batch.txt"
        self.inflight = self.scratch / "landq-batch.txt.inflight"
        self.failed = self.scratch / "landq-batch.failed"
        self.started = self.tmp / "started"
        self.args = self.tmp / "args"
        (self.tmp / "fake_land.sh").write_text(FAKE_LAND)
        (self.tmp / "quiet.sh").write_text("exit 0\n")

    def lane(self, name: str) -> str:
        git(self.repo, "checkout", "-q", "-B", f"lane/{name}", "origin/main")
        (self.repo / f"{name}.txt").write_text(name + "\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"lane {name}")
        git(self.repo, "push", "-q", "origin", f"lane/{name}")
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "--detach")
        return sha

    def queue_line(self, name: str, sha: str) -> str:
        return f"lane/{name}|{sha}|tests.test_ok|lane {name}"

    def start(self, **env: str) -> subprocess.Popen:
        self.started.unlink(missing_ok=True)
        full = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        full.update({"BATCH_LAND_ROOT": str(self.scratch), "BATCH_LAND_REPO": str(self.repo),
                     "LAND_SCRIPT": str(self.tmp / "fake_land.sh"), "LAND_QUIET": str(self.tmp / "quiet.sh"),
                     "LAND_DEPLOY": "true", "LAND_ONCE": "1", "FAKE_REPO": str(self.repo),
                     "FAKE_STARTED": str(self.started), "FAKE_ARGS": str(self.args), **env})
        return subprocess.Popen(["bash", str(LANDER)], env=full, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_started(self, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 60
        while not self.started.exists():
            self.assertIsNone(proc.poll(), "the lander exited before land-fast started")
            self.assertLess(time.monotonic(), deadline, "land-fast never started")
            time.sleep(0.1)

    def kill_batch(self, proc: subprocess.Popen) -> None:
        """SIGKILL the lander loop (an OOM kill or a stop past its timeout), then its leftover children."""
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def on_main(self, sha: str) -> bool:
        git(self.repo, "fetch", "-q", "origin")
        return subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", sha, "origin/main"]
                              ).returncode == 0

    def text(self, path: Path) -> str:
        return path.read_text() if path.exists() else ""

    def test_a_killed_batch_keeps_its_lanes_on_disk_and_the_restart_lands_them(self) -> None:
        a, b = self.lane("a"), self.lane("b")
        self.queue.write_text(self.queue_line("a", a) + "\n" + self.queue_line("b", b) + "\n")
        proc = self.start(FAKE_SLEEP="60")
        self.wait_started(proc)
        self.kill_batch(proc)
        self.assertEqual(self.text(self.queue), "")
        self.assertEqual(self.text(self.failed), "")
        self.assertEqual(self.inflight.read_text().splitlines(),
                         [self.queue_line("a", a), self.queue_line("b", b)])
        self.assertFalse(self.on_main(a) or self.on_main(b))

        proc = self.start(FAKE_SLEEP="0")
        self.assertEqual(proc.wait(timeout=120), 0)
        self.assertTrue(self.on_main(a) and self.on_main(b))
        self.assertFalse(self.inflight.exists())
        self.assertEqual(self.text(self.queue), "")
        self.assertEqual(self.text(self.failed), "")
        self.assertIn("recovered 2 in-flight lanes", self.text(self.scratch / "landq-batch.log"))

    def test_a_lane_that_landed_before_the_kill_is_not_requeued_or_landed_twice(self) -> None:
        a = self.lane("a")
        self.queue.write_text(self.queue_line("a", a) + "\n")
        proc = self.start(FAKE_SLEEP="60", FAKE_PUSH_FIRST="1")   # land-fast pushed, the lander died before reading it
        self.wait_started(proc)
        self.kill_batch(proc)
        self.assertTrue(self.on_main(a))
        self.assertIn(self.queue_line("a", a), self.text(self.inflight))
        b = self.lane("b")
        self.queue.write_text(self.queue_line("b", b) + "\n")
        self.args.unlink()

        proc = self.start(FAKE_SLEEP="0")
        self.assertEqual(proc.wait(timeout=120), 0)
        self.assertTrue(self.on_main(b))
        self.assertNotIn("lane/a", self.args.read_text())
        merges = git(self.repo, "log", "--format=%s", "origin/main").splitlines()
        self.assertEqual(sum(s.startswith("Batch: merge lane/a@") for s in merges), 1)
        self.assertEqual(self.text(self.queue), "")
        self.assertEqual(self.text(self.failed), "")
        self.assertFalse(self.inflight.exists())
        self.assertIn("already on origin/main: not requeued", self.text(self.scratch / "landq-batch.log"))

    def test_sigterm_lets_the_batch_in_flight_finish(self) -> None:
        a = self.lane("a")
        self.queue.write_text(self.queue_line("a", a) + "\n")
        proc = self.start(FAKE_SLEEP="3")
        self.wait_started(proc)
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=120), 0)
        self.assertTrue(self.on_main(a))
        self.assertFalse(self.inflight.exists())
        self.assertEqual(self.text(self.queue), "")
        self.assertIn("SIGTERM", self.text(self.scratch / "landq-batch.log"))


if __name__ == "__main__":
    unittest.main()
