"""``operator_api serve --check`` (plx-release preflight, Fable 11-14 #3): every start-up
check runs and the process exits 0 without serving; a configuration the API cannot start
on exits non-zero before any release switch."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest

from operating import proactive_engine
from operator_api.server import main as serve_main
from operator_api.tests import case_fixtures
from operator_api.workspace import case_console
from operator_api.workspace import object_workspace as objects
from operating.pg import scratch_db

scratch_db.use_test_cluster()


class ServeCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="serve-check-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, store)
        proactive_engine.after_build(store)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, state: Path) -> tuple[int, str]:
        served = []
        out, err = io.StringIO(), io.StringIO()
        argv = ["--check", "--dev", "--insecure-cookie", "--state-dir", str(state),
                "--pg-dsn", "", "--app-data-dir", str(self.tmp / "app"),
                "--dev-fixture-principal", case_fixtures.OPERATIONS,
                "--console-arg=--source-mirror-audience",
                f"--console-arg={state / case_fixtures.MIRROR_AUDIENCE_FILE}"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = serve_main(argv, ready=served.append, fixtures=case_fixtures)
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        self.assertEqual(served, [], "a check never serves")
        return code, out.getvalue() + err.getvalue()

    def test_a_good_configuration_passes_without_serving(self):
        code, text = self.check(self.state)
        self.assertEqual(code, 0, text[-500:])
        self.assertIn('"operator_api_check": "ok"', text)

    def test_a_configuration_the_api_cannot_start_on_fails(self):
        broken = self.tmp / "no-store"
        broken.mkdir()
        code, _ = self.check(broken)
        self.assertNotEqual(code, 0)

    def test_the_mock_check_parses_and_exits(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(serve_main(["--check", "--mock"]), 0)
        self.assertIn("mock", out.getvalue())


if __name__ == "__main__":
    unittest.main()
