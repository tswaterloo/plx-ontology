"""deploy/ops/lander/auto_deploy.sh's log line (DEPLOY-LOG 11-15), isolated fixture: a temporary
bare origin and clone, a releases dir whose ``current`` stays put, a stand-in deploy script. A
``plx-release build`` refusal (migration_pending) printed none of the lines the log kept, so the
log said only "deploy a -> a (main m): "; its reason now reaches auto-deploy.log."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
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
        releases = self.tmp / "releases"
        releases.mkdir()
        (releases / "oldrelease00").mkdir()
        (releases / "current").symlink_to(releases / "oldrelease00")
        self.env = {**{k: v for k, v in os.environ.items()
                       if k not in ("PLX_WORKBENCH_API_KEY", "PLX_PG_DSN")},
                    "AUTO_DEPLOY_ROOT": str(self.scratch), "AUTO_DEPLOY_REPO": str(self.repo),
                    "AUTO_DEPLOY_RELEASES": str(releases)}

    def run_with(self, deploy_text: str) -> tuple[int, str]:
        deploy = self.tmp / "deploy_pg.sh"
        deploy.write_text(deploy_text)
        run = subprocess.run(["bash", str(SCRIPT)], env={**self.env, "AUTO_DEPLOY_PG": str(deploy)},
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


if __name__ == "__main__":
    unittest.main()
