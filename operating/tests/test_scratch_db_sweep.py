"""REL-R-16: which throwaway databases are leftovers, and which server tests make them on."""
import re
import tempfile
import unittest
from pathlib import Path

from operating.pg import scratch_db


class Stale(unittest.TestCase):
    def test_only_throwaway_names_of_a_dead_process_are_stale(self):
        names = ["plx", "postgres", "plx_test_a_100_0", "plx_test_d_100_12", "plx_test_qu_100",
                 "plx_test_b_100", "plx_test_appapi_100", "plx_test_a_200_0", "plx_test_a_absent_db",
                 "plx_test_a_100_0_extra", "plx_prod_100"]
        got = scratch_db.stale(names, alive=lambda pid: pid == 200)
        self.assertEqual(got, ["plx_test_a_100_0", "plx_test_appapi_100", "plx_test_b_100",
                               "plx_test_d_100_12", "plx_test_qu_100"])
        self.assertNotIn("plx", got)

    def test_a_restore_test_database_of_a_dead_run_is_stale(self):
        got = scratch_db.stale(["plx_restore_test_5", "plx_restore_test_6", "plx_restore"],
                               alive=lambda pid: pid == 6)
        self.assertEqual(got, ["plx_restore_test_5"])

    def test_nothing_alive_to_drop_when_every_creator_runs(self):
        self.assertEqual(scratch_db.stale(["plx_test_a_1_0"], alive=lambda pid: True), [])


class AdminServer(unittest.TestCase):
    def test_admin_dsn_follows_the_environment_at_call_time(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"PLX_PG_TEST_ADMIN_DSN": "postgresql://h:1/x"}):
            self.assertEqual(scratch_db.admin_dsn(), "postgresql://h:1/x")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(scratch_db.admin_dsn(), "postgresql:///postgres")

    def test_sweep_never_names_a_non_throwaway_database(self):
        self.assertEqual(scratch_db.stale(["plx", "plx_test_a_1_0_x", "template1"], alive=lambda p: False), [])


class TestCluster(unittest.TestCase):
    """A test run's throwaway databases go to the separate test cluster when one runs, so a
    served reader stuck in ClientWrite never holds their DROP (11-12 host-wide stall)."""

    def test_a_bare_run_is_pointed_at_the_running_test_cluster(self):
        with tempfile.TemporaryDirectory() as sockets:
            env: dict[str, str] = {}
            self.assertIsNone(scratch_db.use_test_cluster(env, sockets))   # none runs
            self.assertEqual(env, {})
            Path(sockets, ".s.PGSQL.5433").write_text("")
            self.assertEqual(scratch_db.use_test_cluster(env, sockets), "5433")
            self.assertEqual(env, {"PGPORT": "5433"})

    def test_a_chosen_server_or_an_unusable_port_is_left_alone(self):
        with tempfile.TemporaryDirectory() as sockets:
            Path(sockets, ".s.PGSQL.5433").write_text("")
            for chosen in ({"PGPORT": "5432"}, {"PGHOST": "/elsewhere"},
                           {"PLX_PG_TEST_ADMIN_DSN": "postgresql://h:1/x"},
                           {"PLX_TEST_PG_PORT": "x/../y"}, {"PLX_TEST_PG_PORT": "6000"}):
                env = dict(chosen)
                self.assertIsNone(scratch_db.use_test_cluster(env, sockets), chosen)
                self.assertEqual(env, chosen)

    def test_every_test_module_making_databases_routes_to_the_test_cluster(self):
        """A module that creates throwaway databases calls ``use_test_cluster`` or imports
        a fixture that does; otherwise a bare run of it drops databases on the served
        cluster again."""
        root = Path(__file__).resolve().parents[2]
        makes = re.compile(r"CREATE DATABASE|\bcreatedb\b|create_db\(")
        routed = re.compile(r"use_test_cluster\(|import pg_testdb|import search_fixture|"
                            r"search_fixture import")
        unrouted = []
        for folder in ("operating/tests", "operator_api/tests", "tests", "evals/tests",
                       "integrations/tests", "tools/tests"):
            for path in sorted((root / folder).glob("*.py")):
                text = path.read_text(encoding="utf-8")
                if path.name != "test_scratch_db_sweep.py" and makes.search(text) \
                        and not routed.search(text):
                    unrouted.append(str(path.relative_to(root)))
        self.assertEqual(unrouted, [])


if __name__ == "__main__":
    unittest.main()
