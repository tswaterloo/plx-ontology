"""A repack takes the views-refresh lock: it never starts while a refresh runs (exit 3,
the command not run), and a refresh that starts during it is told another one is running."""
from __future__ import annotations

import sys
import unittest

from operating.pg import business_views as bv
from operating.pg import locked_repack
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


@unittest.skipIf(psycopg is None, "no psycopg")
class LockedRepack(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.dsn = pg_testdb.create_db()

    @classmethod
    def tearDownClass(cls) -> None:
        pg_testdb.drop_db(cls.db)

    def test_waits_out_nothing_when_a_refresh_holds_the_lock(self):
        with psycopg.connect(self.dsn, autocommit=True) as refresh:
            refresh.execute("SELECT pg_advisory_lock(%s)", (bv.REFRESH_LOCK,))
            got = locked_repack.run(self.dsn, [sys.executable, "-c", "raise SystemExit(7)"],
                                    wait_s=0.2, poll_s=0.05)
        self.assertEqual(got, 3)   # the command (exit 7) never ran

    def test_holds_the_lock_while_it_runs_and_releases_it(self):
        probe = ("import psycopg,sys;c=psycopg.connect(sys.argv[1],autocommit=True);"
                 "sys.exit(0 if not c.execute('SELECT pg_try_advisory_lock(%d)')"
                 ".fetchone()[0] else 9)" % bv.REFRESH_LOCK)
        self.assertEqual(locked_repack.run(self.dsn, [sys.executable, "-c", probe, self.dsn]), 0)
        with psycopg.connect(self.dsn, autocommit=True) as c:   # released after the run
            self.assertTrue(c.execute("SELECT pg_try_advisory_lock(%s)",
                                      (bv.REFRESH_LOCK,)).fetchone()[0])

    def test_passes_the_commands_exit_code_and_refuses_an_empty_command(self):
        self.assertEqual(locked_repack.run(self.dsn, [sys.executable, "-c",
                                                      "raise SystemExit(5)"]), 5)
        with self.assertRaises(ValueError):
            locked_repack.run(self.dsn, [])


if __name__ == "__main__":
    unittest.main()
