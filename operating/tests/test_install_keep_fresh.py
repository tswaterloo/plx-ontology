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


if __name__ == "__main__":
    unittest.main()
