"""The retired console command refuses: ``python3 -m operator_api.tests.case_fixtures``
(and ``operator_api.workspace.case_console``) started without the app API's ``serve_with``.

The console page and its HTTP listener were retired;
the Next app over ``python3 -m operator_api serve`` is the one
front end, and its fixture world is exercised in ``operator_api.tests.test_backend_fixture``.
Starting the old command on a new fixture state directory exits 2, names the entry that
replaced it and prints no console URL. Evidence label: isolated-fixture; credential-shaped
variables are removed from the child's environment and nothing leaves the machine.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", re.I)


class RetiredConsoleTest(unittest.TestCase):

    def test_the_old_console_command_refuses_naming_the_app_api(self):
        env = {k: v for k, v in os.environ.items() if not CREDENTIAL.search(k)}
        with tempfile.TemporaryDirectory() as temp:
            done = subprocess.run(
                [sys.executable, "-m", "operator_api.tests.case_fixtures", "--state-dir",
                 str(Path(temp) / "state"), "--fixture", "--seed-fixtures",
                 "--principal", "staff:operations", "--port", "0"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(done.returncode, 2, done.stderr[-2000:])
        self.assertIn("console retired: python3 -m operator_api serve", done.stderr)
        self.assertNotIn("Operator console: http", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
