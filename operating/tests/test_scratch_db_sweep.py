"""REL-R-16: which throwaway databases are leftovers."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
