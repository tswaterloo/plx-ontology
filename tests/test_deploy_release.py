"""One release for every unit: the committed deploy files name no user or home directory,
every rendered unit runs from the current release, and plx-release builds, switches
atomically and rolls back."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
PERSONAL = re.compile(r"/home/[a-z_][a-z0-9_-]*|/Users/\w+|\btanush\b", re.IGNORECASE)


def tracked(folder: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", str(folder)], cwd=ROOT, text=True,
                         capture_output=True, check=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


class DeployFilesTest(unittest.TestCase):
    def test_no_username_or_home_path_is_committed_under_deploy(self) -> None:
        found = [f"{p.relative_to(ROOT)}:{n}" for p in tracked(DEPLOY) if p.is_file()
                 for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1)
                 if PERSONAL.search(line)]
        self.assertEqual(found, [])

    def test_every_script_parses(self) -> None:
        for path in tracked(DEPLOY):
            if path.suffix == ".sh" or path.name == "plx-release":
                subprocess.run(["bash", "-n", str(path)], check=True)

    def test_every_rendered_unit_runs_from_the_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, "deploy.env")
            env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
            for user in ([], ["--user"]):
                subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", f"{tmp}/out",
                                "--env", str(env), *user], check=True, capture_output=True)
            rendered = [p for p in Path(tmp, "out").rglob("*") if p.is_file()]
            self.assertTrue(any(p.name == "plx-api.service" for p in rendered))
            for path in rendered:
                text = path.read_text()
                self.assertNotIn("@PLX_", text, path)
                for wd in re.findall(r"^WorkingDirectory=(.*)$", text, re.M):
                    self.assertTrue(wd.startswith("/srv/rel/current"), (path, wd))
                for unit_path in re.findall(r"(?:/data/OntologyProject|/data/scratch/wt/)", text):
                    self.fail(f"{path} runs from a checkout: {unit_path}")

    def test_proactive_units_keep_the_store_and_calling_refresh_and_name_no_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, "deploy.env")
            env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
            subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", f"{tmp}/out",
                            "--env", str(env)], check=True, capture_output=True)
            out = Path(tmp, "out")
            proactive = (out / "plx-proactive.service").read_text()
            self.assertIn("Environment=PLX_ONTOLOGY_PG=1", proactive)
            self.assertIn("--refresh calling", proactive)
            self.assertNotIn("--refresh always", proactive)
            for name in ("plx-proactive-inc.service", "plx-proactive-nightly.service"):
                text = (out / name).read_text()
                self.assertIn("User=someone", text, name)
                self.assertIn("WorkingDirectory=/srv/rel/current", text, name)
            self.assertTrue((out / "plx-proactive-inc.path").is_file())
            self.assertTrue((out / "plx-proactive-nightly.timer").is_file())
            secret = (out / "plx-api.service.d" / "live-secret.conf").read_text()
            # required, never optional: a missing live secret must stop the API, not fall
            # back to the development session secret another env file sets
            self.assertIn("EnvironmentFile=/etc/plx/live-session.env", secret)
            self.assertNotIn("EnvironmentFile=-", secret)
            for path in out.rglob("*"):
                if path.is_file():
                    text = path.read_text()
                    self.assertNotIn("/home/tanush", text, path)
                    self.assertNotIn("/data/OntologyProject", text, path)


class ReleaseTest(unittest.TestCase):
    def test_build_switch_and_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for text in ("one", "two"):
                Path(repo, "code.txt").write_text(text)
                subprocess.run([*git, "add", "-A"], check=True)
                subprocess.run([*git, "commit", "-qm", text], check=True)
            Path(tmp, "state").mkdir()
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_REPO": str(repo),
                   "PLX_RELEASES": f"{tmp}/rel", "PLX_STATE": f"{tmp}/state"}
            tool = str(DEPLOY / "release" / "plx-release")

            def run(*args: str) -> str:
                return subprocess.run([tool, *args], env=env, text=True, capture_output=True,
                                      check=True).stdout.strip().splitlines()[-1]

            first = run("build", "HEAD~1", "--no-app")
            second = run("build", "HEAD", "--no-app")
            self.assertEqual(run("build", "HEAD", "--no-app"), second)  # built once
            run("switch", first)
            run("switch", second)
            current = Path(tmp, "rel", "current")
            self.assertEqual(Path(current, "code.txt").read_text(), "two")
            self.assertEqual(os.readlink(Path(tmp, "rel", "previous")), first)
            run("rollback")
            self.assertEqual(Path(current, "code.txt").read_text(), "one")
            self.assertEqual(os.readlink(current), first)
            refused = subprocess.run([tool, "switch", "nope"], env=env, capture_output=True)
            self.assertNotEqual(refused.returncode, 0)


if __name__ == "__main__":
    unittest.main()
