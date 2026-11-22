"""The WAL budget (``operating.pg.wal_budget``) and the heavy writers it holds back.

11-21 live: an embed backfill wrote 23 MB/s of WAL, 2.8 MB/s reached the Spark standby; it
was 18 GB behind of a 40 GB slot cap when it was stopped by hand. A heavy writer now pauses
past 8 GB behind and resumes under 2 GB; an unreadable lag pauses it too (fail closed); a
paused job starts no new slice/chunk/step and a pause past the longest wait stops it with
what it committed kept. The lag is read from a real physical slot on the test cluster."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from operating.pg import chunked, wal_budget
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

GB = wal_budget.GB


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def scripted(values):
    """A lag reader that plays ``values`` (an Exception instance raises) then repeats the
    last one."""
    seq = list(values)

    def read():
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(v, Exception):
            raise v
        return v, "plx_s7d3d"
    return read


class Verdicts(unittest.TestCase):
    def budget(self, values, **kw):
        clock = Clock()
        kw.setdefault("log", None)
        return wal_budget.Budget(scripted(values), clock=clock, check_seconds=0,
                                 pause_bytes=8 * GB, resume_bytes=2 * GB, **kw), clock

    def test_pauses_past_the_pause_line_and_holds_until_under_the_resume_line(self):
        b, _ = self.budget([1 * GB, 9 * GB, 5 * GB, 2 * GB + 1, 1 * GB, 5 * GB, 8 * GB + 1])
        got = [b.blocked() for _ in range(7)]
        self.assertEqual(got, [None, "standby_lag:plx_s7d3d", "standby_lag:plx_s7d3d",
                               "standby_lag:plx_s7d3d", None, None,
                               "standby_lag:plx_s7d3d"])
        self.assertEqual(b.pauses, 2)

    def test_an_unreadable_lag_pauses_and_a_long_pause_stops_the_job(self):
        b, clock = self.budget([psycopg.OperationalError("gone") if psycopg else
                                RuntimeError("gone")])
        self.assertTrue(b.blocked().startswith("standby_lag_unknown:"))
        with self.assertRaisesRegex(wal_budget.BudgetTimeout, "still paused after 60 s"):
            b.wait(max_seconds=60, sleep=clock.sleep)
        self.assertGreaterEqual(clock.t, 60)

    def test_wait_returns_once_the_standby_has_caught_up(self):
        b, clock = self.budget([9 * GB, 6 * GB, 3 * GB, 1 * GB])
        waited = b.wait(max_seconds=3600, sleep=clock.sleep)
        self.assertGreater(waited, 0)
        self.assertIsNone(b.blocked())

    def test_the_disk_floor_pauses_and_an_unreadable_disk_pauses(self):
        b, _ = self.budget([0], free_path="/x", min_free=75 * GB, free=lambda p: 74 * GB)
        self.assertEqual(b.blocked(), "disk_below_min_free")

        def gone(p):
            raise OSError("no disk")
        b, _ = self.budget([0], free_path="/x", min_free=75 * GB, free=gone)
        self.assertEqual(b.blocked(), "disk_unreadable:OSError")
        b, _ = self.budget([0], free_path="/x", min_free=75 * GB, free=lambda p: 76 * GB)
        self.assertIsNone(b.blocked())

    def test_thresholds_from_the_environment_refuse_anything_but_numbers(self):
        for bad in ({"PLX_WAL_PAUSE_GB": "lots"}, {"PLX_WAL_RESUME_GB": "-1"},
                    {"PLX_WAL_PAUSE_GB": "1", "PLX_WAL_RESUME_GB": "2"},
                    {"PLX_WAL_MAX_WAIT_S": "0"}):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, bad), \
                    self.assertRaises(ValueError):
                wal_budget.thresholds()
                wal_budget.max_wait_seconds()
        with mock.patch.dict(os.environ, {"PLX_WAL_PAUSE_GB": "", "PLX_WAL_RESUME_GB": ""}):
            self.assertEqual(wal_budget.thresholds(), (8 * GB, 2 * GB))


@unittest.skipIf(psycopg is None, "no psycopg")
class OnPostgres(unittest.TestCase):
    """A temporary physical slot (reserved now, never consumed) holds back every byte of
    WAL written after it: exactly a standby that does not keep up."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.dsn = pg_testdb.create_db()
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute("CREATE TABLE t (n int PRIMARY KEY, pad text)")

    @classmethod
    def tearDownClass(cls) -> None:
        pg_testdb.drop_db(cls.db)

    def slot(self):
        """A session holding a temporary reserved slot (dropped when it closes)."""
        holder = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(holder.close)
        name = f"walbudget_{os.getpid()}_{id(self) % 100000}"
        try:
            holder.execute("SELECT pg_create_physical_replication_slot(%s, true, true)",
                           (name,))
        except psycopg.Error as exc:   # wal_level minimal / no REPLICATION: say why
            self.skipTest(f"no physical slot on the test cluster: {str(exc).splitlines()[0]}")
        return name

    def write_wal(self, rows=2000):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO t SELECT g, repeat('x', 200) FROM generate_series(%s, %s) g "
                      "ON CONFLICT (n) DO UPDATE SET pad = EXCLUDED.pad",
                      (1, rows))

    def test_the_lag_is_the_wal_a_slot_holds_back_and_names_it(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            before, _ = wal_budget.lag(c)
            name = self.slot()
            self.write_wal()
            after, who = wal_budget.lag(c)
        self.assertGreater(after, 100_000)
        self.assertGreaterEqual(after, before)
        if after > before:
            self.assertEqual(who, name)

    def test_a_relabel_pauses_on_a_lagging_standby_and_stops_with_nothing_written(self):
        from operating.pg import generic_records as gr
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("CREATE SCHEMA IF NOT EXISTS rl")
            c.execute("CREATE TABLE IF NOT EXISTS rl.records (rid int, label text, type text)")
            c.execute("INSERT INTO rl.records SELECT g, '0', 'X' FROM generate_series(1, 50) g")
        env = {"PLX_WAL_MAX_WAIT_S": "1"}
        with mock.patch.dict(os.environ, env), behind(9 * GB), \
                self.assertRaisesRegex(wal_budget.BudgetTimeout, "records_relabel"):
            gr.relabel(self.dsn, schema="rl", step=10)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM rl.records WHERE label = '0'")
                             .fetchone()[0], 50)          # the junk label not yet cleared

    def test_a_guarded_command_never_starts_while_paused_and_is_stopped_past_the_abort(self):
        marker = os.path.join(tempfile.mkdtemp(), "ran")
        cmd = [sys.executable, "-c", f"open({marker!r}, 'w').close()"]
        with behind(9 * GB):
            got = wal_budget.run_guarded(self.dsn, cmd, min_free=0, max_wait=0.5, poll=0.05,
                                         log=None)
        self.assertEqual(got, 4)
        self.assertFalse(os.path.exists(marker))     # never ran
        # admitted at 1 GB behind, then the standby falls past the abort line: stopped
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        seq = iter([1 * GB] * 3)
        with mock.patch.object(wal_budget, "lag",
                               side_effect=lambda c: (next(seq, 31 * GB), "plx_s7d3d")):
            got = wal_budget.run_guarded(self.dsn, sleeper, abort_bytes=30 * GB, min_free=0,
                                         max_wait=5, poll=0.05, log=None)
        self.assertEqual(got, 5)
        # an unreadable lag mid-run (3 polls running) stops it too: never fail open
        seq2 = iter([(1 * GB, "plx_s7d3d")] * 3)

        def flaky(c):
            got = next(seq2, None)
            if got is None:
                raise RuntimeError("stats gone")
            return got
        with mock.patch.object(wal_budget, "lag", side_effect=flaky):
            got = wal_budget.run_guarded(self.dsn, sleeper, abort_bytes=30 * GB, min_free=0,
                                         max_wait=5, poll=0.05, log=None)
        self.assertEqual(got, 5)
        # and a command run to its end under the line keeps its own exit code
        with behind(0):
            self.assertEqual(wal_budget.run_guarded(
                self.dsn, [sys.executable, "-c", "raise SystemExit(7)"], min_free=0,
                poll=0.05, log=None), 7)

    def test_chunked_slices_wait_out_a_pause_and_every_one_runs_once(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("CREATE TABLE IF NOT EXISTS s (n int PRIMARY KEY)")
        for n in (1, 10, 60):
            with self.subTest(n=n):
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    c.execute("TRUNCATE s")
                budget = Pausing(pauses=3)
                job = chunked.Job(name=f"wb{n}", slices=[
                    chunked.Slice(key=f"{i:03d}", part="p", fn=_insert_s, args=(i,), weight=1)
                    for i in range(n)])
                out = chunked.run(self.dsn, job, workers=4, log=_QUIET, wal=budget,
                                  sleep=lambda s: None)
                self.assertEqual((out["state"], out["done"]), ("done", n), out)
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    got = sorted(r[0] for r in c.execute("SELECT n FROM s"))
                self.assertEqual(got, list(range(n)))     # none lost, none twice
                self.assertEqual(budget.left, 0)          # the pause was asked and waited

    def test_a_chunked_run_paused_past_the_longest_wait_stops_resumable(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("CREATE TABLE IF NOT EXISTS s2 (n int PRIMARY KEY)")
        clock = Clock()
        budget = Pausing(pauses=10 ** 9)
        job = chunked.Job(name="wbstop", slices=[
            chunked.Slice(key=f"{i:03d}", part="p", fn=_insert_s, args=(i,), weight=1)
            for i in range(5)])
        with mock.patch.dict(os.environ, {"PLX_WAL_MAX_WAIT_S": "30"}):
            out = chunked.run(self.dsn, job, workers=2, log=_QUIET, wal=budget,
                              monotonic=clock, sleep=clock.sleep)
        self.assertEqual(out["state"], "stopped", out)
        self.assertTrue(out["stopped"].startswith("wal_budget: standby_lag"), out)
        self.assertEqual(out["left"], 5)


def behind(n):
    """The standby ``n`` bytes behind (at the library's one read of it)."""
    return mock.patch.object(wal_budget, "lag", return_value=(n, "plx_s7d3d"))


class MissingStandby(unittest.TestCase):
    """No slot reads as 0 behind; an expected slot that is gone pauses
    (and stops a running one-statement writer); a standby/copy in recovery is never paused."""

    def fake(self, slots, recovery=False):
        class R:
            def __init__(self, rows): self.rows = rows
            def fetchone(self): return self.rows[0]
            def fetchall(self): return self.rows
        class C:
            autocommit = True
            def execute(self, sql, *a):
                if "pg_is_in_recovery" in sql: return R([(recovery,)])
                if "pg_replication_slots" in sql and "slot_name FROM" in sql:
                    return R([(x,) for x in slots])
                return R([(x, 0) for x in slots])
        return C()

    def test_an_expected_slot_gone_pauses_and_present_does_not(self):
        with mock.patch.dict(os.environ, {"PLX_WAL_STANDBY_SLOTS": "plx_s7d3d, plx_sb4ec_archive"}):
            b = wal_budget.Budget.for_conn(self.fake(["plx_sb4ec_archive"]), log=None,
                                           check_seconds=0)
            self.assertEqual(b.blocked(), "standby_missing:plx_s7d3d")
            b = wal_budget.Budget.for_conn(self.fake(["plx_s7d3d", "plx_sb4ec_archive"]),
                                           log=None, check_seconds=0)
            self.assertIsNone(b.blocked())
            b = wal_budget.Budget.for_conn(self.fake([], recovery=True), log=None,
                                           check_seconds=0)
            self.assertIsNone(b.blocked())
        b = wal_budget.Budget.for_conn(self.fake([]), log=None, check_seconds=0)
        self.assertIsNone(b.blocked())         # no standby expected: none missing


class Pausing:
    """A budget paused for its first ``pauses`` verdicts."""

    def __init__(self, pauses):
        self.left, self.check_seconds = pauses, 0

    def blocked(self):
        if self.left > 0:
            self.left -= 1
            return "standby_lag:test"
        return None


def _insert_s(conn, n):
    conn.execute("INSERT INTO s (n) VALUES (%s)", (n,))
    return {"n": n}


class _Quiet:
    def write(self, *_):
        pass

    def flush(self):
        pass


_QUIET = _Quiet()


if __name__ == "__main__":
    unittest.main()
