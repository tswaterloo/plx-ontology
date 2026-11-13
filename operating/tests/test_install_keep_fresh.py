"""OPS-004: the upkeep installer's unit verification fails on a real systemd error."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "ops" / "install_keep_fresh.sh"
FAKE = "#!/bin/bash\necho \"$FAKE_OUT\" >&2\nexit ${FAKE_RC:-0}\n"


class Verification(unittest.TestCase):
    def dry_run(self, out: str, rc: int):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "bin").mkdir()
        fake = tmp / "bin" / "systemd-analyze"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{tmp / 'bin'}:{os.environ['PATH']}", FAKE_OUT=out,
                   FAKE_RC=str(rc))
        for key in list(env):
            if any(w in key for w in ("KEY", "SECRET", "TOKEN", "DSN", "PASSWORD")):
                del env[key]
        done = subprocess.run(["bash", str(SCRIPT), "--dry-run"], cwd=tmp, env=env,
                              capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    def test_clean_and_documented_noise_pass(self):
        self.assertEqual(self.dry_run("", 0)[0], 0)
        self.assertEqual(self.dry_run("plx-x.service: Command /x is not executable: No such "
                                      "file", 1)[0], 0)

    def test_a_real_error_fails_even_though_nice_and_memorymax_are_present(self):
        code, text = self.dry_run("plx-sheet-readings.service:3: Unknown key name 'Bogus'", 1)
        self.assertEqual(code, 1)
        self.assertIn("Unknown key name", text)
        self.assertNotIn("rendered and verified", text)

    def test_a_silent_nonzero_exit_fails(self):
        self.assertEqual(self.dry_run("", 1)[0], 1)


class AutopilotFollowUnit(unittest.TestCase):
    """CAP-L285: the installer ships the autopilot's live feed (service, timer and the path
    on each delta publish) with the ordered event queue on, reading Postgres; the unit
    prepares items only: it runs ``follow`` and nothing that sends, writes or approves."""

    def test_installed_and_prepares_only(self):
        text = SCRIPT.read_text()
        self.assertIn("plx-autopilot-follow", text.split("SYS_UNITS=", 1)[1].splitlines()[0])
        self.assertIn('SYS_PATHS="plx-autopilot-follow"', text)
        self.assertIn('enable --now "$u.path"', text)
        tmp = Path(tempfile.mkdtemp())
        env = tmp / "deploy.env"
        env.write_text("PLX_USER=u\nPLX_HOME=/home/u\n")
        render = SCRIPT.parents[1] / "render.py"
        done = subprocess.run(["python3", str(render), "--out", str(tmp / "out"), "--env",
                               str(env)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        unit = (tmp / "out" / "plx-autopilot-follow.service").read_text()
        lines = [ln for ln in unit.splitlines() if ln and not ln.startswith("#")]
        start = [ln for ln in lines if ln.startswith("ExecStart=")]
        self.assertEqual(len(start), 1)
        self.assertIn("-m operating.autopilot follow --state-dir", start[0])
        self.assertIn("Environment=PLX_AUTOPILOT_EVQ=1", lines)
        self.assertIn("Environment=PLX_ONTOLOGY_PG=1", lines)
        for live in ("autopilot_write", "PLX_AUTOPILOT_WRITES", "PLX_ACTIONS_LIVE",
                     "PLX_AUTOPILOT_OFF=0"):
            self.assertFalse([ln for ln in lines if live in ln], live)
        self.assertTrue((tmp / "out" / "plx-autopilot-follow.timer").is_file())
        path = (tmp / "out" / "plx-autopilot-follow.path").read_text()
        self.assertIn("Unit=plx-autopilot-follow.service", path)


if __name__ == "__main__":
    unittest.main()
