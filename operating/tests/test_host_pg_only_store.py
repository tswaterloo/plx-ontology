"""The console is given the host's projection path as its store on a Postgres-only host
whose SQLite generation was deleted: the gap inbox and watchlists live beside that path
(11-14: every alert read failed "could not be read" after the gen-11 delete)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operating import host as host_module
from operating.pg import ontology_reader


class ServesProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def env(self, value):
        env = {k: v for k, v in os.environ.items() if k != ontology_reader.ENV}
        if value is not None:
            env[ontology_reader.ENV] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def test_file_present_is_served(self) -> None:
        path = host_module.projection_path(self.state)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        with self.env(None):
            self.assertTrue(host_module._serves_projection(self.state))

    def test_pg_only_without_file_still_names_projection(self) -> None:
        host_module.projection_path(self.state).parent.mkdir(parents=True)
        with self.env(ontology_reader.ONLY):
            self.assertTrue(host_module._serves_projection(self.state))

    def test_not_pg_only_without_file_is_not_served(self) -> None:
        host_module.projection_path(self.state).parent.mkdir(parents=True)
        with self.env(None):
            self.assertFalse(host_module._serves_projection(self.state))

    def test_no_projection_dir_is_not_served(self) -> None:
        with self.env(ontology_reader.ONLY):
            self.assertFalse(host_module._serves_projection(self.state))


class CallSitesTest(unittest.TestCase):
    def test_console_uses_rule_mcp_keeps_file_check(self) -> None:
        # the console accepts a nominal path in PG-only mode; the MCP host refuses a
        # store path with no file, so it is given one only when the file is there
        import inspect
        self.assertIn("_serves_projection(host.state_dir)",
                      inspect.getsource(host_module.console_arguments))
        mcp = inspect.getsource(host_module.mcp_arguments)
        self.assertNotIn("_serves_projection", mcp)
        self.assertIn("projection_path(host.state_dir).is_file()", mcp)


if __name__ == "__main__":
    unittest.main()
