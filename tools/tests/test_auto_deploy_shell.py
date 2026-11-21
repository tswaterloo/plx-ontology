"""deploy/ops/lander/auto_deploy.sh's log line (DEPLOY-LOG 11-15), isolated fixture: a temporary
bare origin and clone, a releases dir whose ``current`` stays put, a stand-in deploy script. A
``plx-release build`` refusal (migration_pending) printed none of the lines the log kept, so the
log said only "deploy a -> a (main m): "; its reason now reaches auto-deploy.log."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "ops" / "lander" / "auto_deploy.sh"
# what deploy_pg.sh passes on when plx-release build's schema gate refuses (stderr, exit 1)
REFUSED = """#!/bin/bash
echo "Postgres holds another schema or step code (migration_pending; code abc, served def)" >&2
echo "plx-release: not built (--no-schema-check to build without the check)" >&2
exit 1
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


class AutoDeployLogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.repo)], check=True,
                       capture_output=True)
        git(self.repo, "config", "user.name", "Lander")
        git(self.repo, "config", "user.email", "lander@example.invalid")
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "push", "-q", "origin", "main")
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        releases = self.releases = self.tmp / "releases"
        releases.mkdir()
        (releases / "oldrelease00").mkdir()
        (releases / "current").symlink_to(releases / "oldrelease00")
        self.env = {**{k: v for k, v in os.environ.items()
                       if k not in ("PLX_WORKBENCH_API_KEY", "PLX_PG_DSN")},
                    "AUTO_DEPLOY_ROOT": str(self.scratch), "AUTO_DEPLOY_REPO": str(self.repo),
                    "AUTO_DEPLOY_RELEASES": str(releases)}

    def run_with(self, deploy_text: str, **env: str) -> tuple[int, str]:
        deploy = self.tmp / "deploy_pg.sh"
        deploy.write_text(deploy_text)
        run = subprocess.run(["bash", str(SCRIPT)],
                             env={**self.env, "AUTO_DEPLOY_PG": str(deploy), **env},
                             capture_output=True, text=True, timeout=60)
        return run.returncode, (self.scratch / "auto-deploy.log").read_text()

    def test_a_build_refusal_logs_its_reason(self):
        rc, log = self.run_with(REFUSED)
        self.assertEqual(rc, 1, "nothing switched")
        line, = log.splitlines()
        self.assertIn("deploy oldrelease00 -> oldrelease00", line)
        self.assertIn("migration_pending", line)
        self.assertIn("plx-release: not built", line)

    def test_an_unknown_failure_logs_its_exit_and_last_line(self):
        rc, log = self.run_with("#!/bin/bash\necho starting\necho 'something odd' >&2\n"
                                "echo\nexit 3\n")
        self.assertEqual(rc, 1)
        self.assertIn("refused (exit 3): something odd", log)

    def test_the_known_summary_lines_are_kept_as_before(self):
        text = ("#!/bin/bash\necho 'plx-release: noise'\n"
                "echo '1:00 PM release x not deployed: Postgres migrations held for a "
                "migration window; the current release oldrelease00 keeps serving' >&2\nexit 75\n")
        rc, log = self.run_with(text)
        self.assertIn("not deployed: Postgres migrations held", log)
        self.assertNotIn("noise", log, "a summary line found: no fallback")


class AutoDeploySpacingTests(AutoDeployLogTests):
    """In the working day a release goes live at most every AUTO_DEPLOY_SPACING_MIN minutes
    after the last switch; deploy-urgent goes at once; outside the day nothing waits."""
    RAN = "#!/bin/bash\necho ran >> \"$AUTO_DEPLOY_ROOT/ran\"\nexit 1\n"

    def switched(self, seconds_ago: int) -> None:
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))
        (self.releases / "history.log").write_text(
            f"{at} built x from origin/main (x)\n{at} switch a -> oldrelease00\n")

    def test_a_deploy_soon_after_the_last_waits_then_runs_once(self):
        self.switched(50)   # spacing 1 min: about 10 s left
        began = time.monotonic()
        rc, log = self.run_with(self.RAN, AUTO_DEPLOY_SPACING_MIN="1", AUTO_DEPLOY_DAY="0-24")
        waited = time.monotonic() - began
        self.assertGreaterEqual(waited, 8)
        self.assertIn("waits: one deploy per 1 min in the day", log)
        self.assertEqual((self.scratch / "ran").read_text(), "ran\n")

    def test_a_second_trigger_while_one_waits_ends_at_once(self):
        self.switched(45)   # spacing 1 min: the first waits about 15 s
        deploy = self.tmp / "deploy_pg.sh"
        deploy.write_text(self.RAN)
        env = {**self.env, "AUTO_DEPLOY_PG": str(deploy), "AUTO_DEPLOY_SPACING_MIN": "1",
               "AUTO_DEPLOY_DAY": "0-24"}
        first = subprocess.Popen(["bash", str(SCRIPT)], env=env)
        self.addCleanup(first.kill)
        waiting = self.scratch / "auto-deploy.waiting"
        for _ in range(100):
            if waiting.exists():
                break
            time.sleep(0.1)
        began = time.monotonic()
        second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60)
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(first.wait(timeout=60), 1)
        log = (self.scratch / "auto-deploy.log").read_text()
        self.assertIn("goes live with it", log)
        self.assertEqual((self.scratch / "ran").read_text(), "ran\n", "one deploy for both")
        self.assertFalse(waiting.exists())

    def test_a_waiting_pid_reused_by_another_process_does_not_hold_a_deploy(self):
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.kill)
        (self.scratch / "auto-deploy.waiting").write_text(f"{other.pid}\n")
        _, log = self.run_with(self.RAN)
        self.assertNotIn("goes live with it", log)
        self.assertTrue((self.scratch / "ran").exists())

    def test_a_stale_urgent_is_dropped_when_main_is_already_live(self):
        (self.scratch / "deploy-urgent").touch()
        git(self.repo, "push", "-q", "origin", "main")
        head = git(self.repo, "rev-parse", "--short=12", "HEAD")
        (self.releases / head).mkdir()
        (self.releases / "current").unlink()
        (self.releases / "current").symlink_to(self.releases / head)
        _, log = self.run_with(self.RAN)
        self.assertIn("already deployed", log)
        self.assertFalse((self.scratch / "deploy-urgent").exists())

    def test_urgent_goes_at_once_and_is_used_up(self):
        self.switched(5)
        (self.scratch / "deploy-urgent").touch()
        began = time.monotonic()
        rc, log = self.run_with(self.RAN, AUTO_DEPLOY_SPACING_MIN="30", AUTO_DEPLOY_DAY="0-24")
        self.assertLess(time.monotonic() - began, 20)
        self.assertNotIn("waits", log)
        self.assertTrue((self.scratch / "ran").exists())
        self.assertFalse((self.scratch / "deploy-urgent").exists())

    def test_outside_the_day_or_long_after_nothing_waits(self):
        for env, ago in (({"AUTO_DEPLOY_DAY": "0-0"}, 5), ({"AUTO_DEPLOY_DAY": "0-24"}, 3600)):
            with self.subTest(env=env, ago=ago):
                self.switched(ago)
                began = time.monotonic()
                _, log = self.run_with(self.RAN, AUTO_DEPLOY_SPACING_MIN="30", **env)
                self.assertLess(time.monotonic() - began, 20)
                self.assertNotIn("waits", log)

    def test_unreadable_history_does_not_hold_a_deploy(self):
        (self.releases / "history.log").write_text("garbage switch line\n")
        _, log = self.run_with(self.RAN, AUTO_DEPLOY_SPACING_MIN="30", AUTO_DEPLOY_DAY="0-24")
        self.assertTrue((self.scratch / "ran").exists())


if __name__ == "__main__":
    unittest.main()
