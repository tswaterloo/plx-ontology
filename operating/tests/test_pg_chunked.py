"""The chunked runner (``operating.pg.chunked``) and its maintenance jobs on a throwaway
Postgres: a stopped run resumes to what one full run leaves; a failed slice loses only
itself; a statement past the budget is cut and split (the split survives a rerun); the disk
floor, a window lock and the run budget stop it cleanly before a slice starts; a rehearsal
keeps nothing and projects the full run; one run of a job at a time."""
from __future__ import annotations

import sys
import types
import unittest

from operating.pg import chunked, maintenance
from operating.tests import pg_testdb


def insert(conn, n):
    conn.execute("INSERT INTO t (n) VALUES (%s)", (n,))
    return {"n": n}


def insert_then_fail(conn, n):
    conn.execute("INSERT INTO t (n) VALUES (%s)", (n,))
    raise ValueError("boom")


def slow(conn, n):
    conn.execute("SELECT pg_sleep(3)")
    conn.execute("INSERT INTO t (n) VALUES (%s)", (n,))


def make_job(n=10, parts=2, fn=insert, name="j"):
    return chunked.Job(name=name, slices=[
        chunked.Slice(key=f"{i:03d}", part=f"p{i % parts}", fn=fn, args=(i,), weight=1 + i)
        for i in range(n)])


class _Quiet:
    def write(self, *_):
        pass

    def flush(self):
        pass


QUIET = _Quiet()


class ChunkedTests(unittest.TestCase):
    def setUp(self):
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with chunked._connect(self.dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE t (n int PRIMARY KEY)")

    def rows(self):
        with chunked._connect(self.dsn, autocommit=True) as conn:
            return sorted(r[0] for r in conn.execute("SELECT n FROM t"))

    def marks(self, job="j"):
        return chunked.status(self.dsn, job)["checkpoints"]

    def test_stopped_run_resumes_to_full_run(self):
        started = []

        def admit(_sl):
            started.append(1)
            return None if len(started) <= 4 else "full"
        first = chunked.run(self.dsn, make_job(), workers=1, log=QUIET,
                            hooks=[chunked.admission(admit)])
        self.assertEqual((first["state"], first["exit"], first["done"]), ("stopped", 3, 4))
        self.assertEqual(first["stopped"], "full")
        self.assertEqual(len(self.rows()), 4)
        second = chunked.run(self.dsn, make_job(), workers=3, log=QUIET)
        self.assertEqual((second["state"], second["done_before"], second["done"]),
                         ("done", 4, 6))
        self.assertEqual(self.rows(), list(range(10)))
        self.assertEqual(self.marks(), 10)
        third = chunked.run(self.dsn, make_job(), workers=3, log=QUIET)
        self.assertEqual((third["done"], third["done_before"]), (0, 10))

    def test_heaviest_first_and_lightest_first(self):
        order = []
        job = make_job(5)
        chunked.run(self.dsn, job, workers=1, log=QUIET, hooks=[
            lambda sl, _r: order.append(sl.key)])
        self.assertEqual(order, ["004", "003", "002", "001", "000"])
        chunked.reset(self.dsn, "j")
        with chunked._connect(self.dsn, autocommit=True) as conn:
            conn.execute("TRUNCATE t")
        order.clear()
        job.order = "lightest"
        chunked.run(self.dsn, job, workers=1, log=QUIET, hooks=[
            lambda sl, _r: order.append(sl.key)])
        self.assertEqual(order, ["000", "001", "002", "003", "004"])

    def test_failed_slice_loses_only_itself(self):
        job = make_job(4)
        job.slices.append(chunked.Slice(key="bad", part="p0", fn=insert_then_fail,
                                        args=(99,), weight=0))
        out = chunked.run(self.dsn, job, workers=1, log=QUIET)
        self.assertEqual((out["state"], out["exit"]), ("failed", 1))
        self.assertEqual(out["failed"]["key"], "bad")
        self.assertNotIn(99, self.rows())          # its insert rolled back
        self.assertEqual(self.rows(), [0, 1, 2, 3])  # the others committed
        self.assertEqual(self.marks(), 4)

    def test_statement_budget_cuts_and_splits(self):
        job = chunked.Job(name="s", slices=[chunked.Slice(key="big", part="p", fn=slow,
                                                          args=(7,))])
        out = chunked.run(self.dsn, job, workers=1, statement_seconds=0.5, log=QUIET)
        self.assertEqual(out["failed"]["error"], "statement_budget")
        self.assertEqual(self.rows(), [])
        job.split = lambda sl: [chunked.Slice(key=f"{sl.key}/{i}", part=sl.part, fn=insert,
                                              args=(10 + i,)) for i in range(2)]
        out = chunked.run(self.dsn, job, workers=1, statement_seconds=0.5, log=QUIET)
        self.assertEqual((out["state"], out["split"], out["done"]), ("done", 1, 2))
        self.assertEqual(self.rows(), [10, 11])
        again = chunked.run(self.dsn, job, workers=1, statement_seconds=0.5, log=QUIET)
        self.assertEqual((again["split"], again["done_before"], again["done"]), (1, 2, 0))
        job.split = lambda sl: [chunked.Slice(key="other", part=sl.part, fn=insert,
                                              args=(1,))]
        changed = chunked.run(self.dsn, job, workers=1, log=QUIET)
        self.assertEqual(changed["failed"]["error"], "split_changed")

    def test_disk_floor_counts_running_slices(self):
        job = make_job(4, parts=1)
        job.slices = [chunked.Slice(key=s.key, part=s.part, fn=s.fn, args=s.args,
                                    weight=s.weight, disk_gb=10) for s in job.slices]
        hook = chunked.disk_floor("/x", 30, free_gb=lambda _p: 35)
        out = chunked.run(self.dsn, job, workers=2, log=QUIET, hooks=[hook])
        self.assertEqual(out["state"], "stopped")
        self.assertTrue(out["stopped"].startswith("disk_budget"))
        self.assertEqual(out["done"], 0)
        roomy = chunked.disk_floor("/x", 30, free_gb=lambda _p: 45)
        sl = job.slices[0]
        self.assertIsNone(roomy(sl, []))
        self.assertIsNotNone(roomy(sl, [sl]))   # a second one in flight would pass the floor

    def test_window_lock_and_budget_and_signal_stop(self):
        fake = types.ModuleType("ontology.window_lock")
        fake.JOB_UNITS = {"delta": ()}
        fake.refusal = lambda job, **_: "held by train (phase 1)" if job == "delta" else None
        live = [{"window": "mine", "expired": False, "reason": "repack"},
                {"window": "old", "expired": True},
                {"window": "train", "expired": False, "reason": "phase 2"}]
        fake.holds = lambda **_: list(live)
        sys.modules["ontology.window_lock"] = fake
        self.addCleanup(sys.modules.pop, "ontology.window_lock", None)
        out = chunked.run(self.dsn, make_job(), log=QUIET,
                          hooks=[chunked.window_free("delta")])
        self.assertEqual(out["stopped"], "window_held: held by train (phase 1)")
        # maintenance (a job the locks do not name) yields to any live hold but its own
        out = chunked.run(self.dsn, make_job(), log=QUIET,
                          hooks=[chunked.window_free(own="mine")])
        self.assertEqual(out["stopped"], "window_held: held by train (phase 2)")
        live.pop()
        hooks = [chunked.window_free(own="mine"), chunked.window_held("mine")]
        self.assertIsNone(hooks[0](make_job().slices[0], []))
        self.assertIsNone(hooks[1](make_job().slices[0], []))
        live[0]["expired"] = True   # the run's own lease ran out
        out = chunked.run(self.dsn, make_job(), log=QUIET, hooks=[chunked.window_held("mine")])
        self.assertEqual(out["stopped"], "lease_lost: window mine is no longer held")
        out = chunked.run(self.dsn, make_job(), log=QUIET, hooks=[chunked.window_held("w")])
        self.assertTrue(out["stopped"].startswith("lease_lost"))
        out = chunked.run(self.dsn, make_job(), log=QUIET, budget_minutes=0)
        self.assertEqual((out["stopped"], out["done"]), ("budget", 0))
        stop = chunked.Stop()
        stop.set("signal_SIGTERM")
        out = chunked.run(self.dsn, make_job(), log=QUIET, stop=stop)
        self.assertEqual((out["stopped"], out["exit"]), ("signal_SIGTERM", 3))
        self.assertEqual(self.rows(), [])

    def test_rehearsal_never_runs_untransactional_slices(self):
        job = make_job(4, parts=1)
        job.slices.append(chunked.Slice(key="vac", part="vacuum", fn=insert, args=(50,),
                                        transactional=False))
        out = chunked.run(self.dsn, job, rehearse=0.5, log=QUIET)
        self.assertEqual(out["unmeasured_parts"], ["vacuum"])
        self.assertFalse(out["fits_budget"])
        self.assertEqual(self.rows(), [])   # nothing ran that could not roll back
        out = chunked.run(self.dsn, job, rehearse=0.5, log=QUIET,
                          rehearse_untransactional=True)
        self.assertEqual(out["unmeasured_parts"], [])
        self.assertEqual(self.rows(), [50])   # on a scratch copy it runs, and is timed
        self.assertEqual(self.marks(), 0)

    def test_rehearsal_keeps_nothing_and_projects(self):
        out = chunked.run(self.dsn, make_job(20, parts=2), rehearse=0.1, log=QUIET,
                          budget_minutes=60)
        self.assertEqual(out["state"], "rehearsed")
        self.assertEqual(out["sampled"], 2)   # one of each part's ten
        self.assertEqual(sorted(out["projected_part_minutes"]), ["p0", "p1"])
        self.assertTrue(out["fits_budget"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.marks(), 0)
        self.assertEqual(len(chunked.sample(make_job(20, parts=2).slices, 0.25)), 6)

    def test_one_run_of_a_job_at_a_time(self):
        held = chunked._job_lock(self.dsn, make_job())
        self.addCleanup(held.close)
        out = chunked.run(self.dsn, make_job(), log=QUIET)
        self.assertEqual((out["state"], out["exit"]), ("job_running", 75))
        self.assertEqual(self.rows(), [])


class MaintenanceTests(unittest.TestCase):
    def test_factories_resolve_by_registered_name_only(self):
        from operating.pg import maintenance
        self.assertIs(chunked._resolve("operating.pg.maintenance:repack"), maintenance.repack)
        self.assertIs(chunked._resolve(maintenance.vacuum), maintenance.vacuum)
        with self.assertRaises(ValueError):
            chunked._resolve("os:system")

    def setUp(self):
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with chunked._connect(self.dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE src (n int, pad text)")
            conn.execute("INSERT INTO src SELECT g, repeat('x', 200) FROM "
                         "generate_series(1, 20000) g")
            conn.execute("CREATE INDEX src_n ON src (n)")

    def test_vacuum_and_drop_index_jobs(self):
        with chunked._connect(self.dsn, autocommit=True) as conn:
            job = maintenance.vacuum(conn, tables="public.src,public.missing", run="t")
            self.assertEqual([s.key for s in job.slices], ["public.src"])
            drop = maintenance.drop_indexes(conn, indexes="public.src_n", run="t")
        self.assertEqual(chunked.run(self.dsn, job, log=QUIET)["state"], "done")
        out = chunked.run(self.dsn, drop, log=QUIET)
        self.assertEqual((out["state"], out["done"]), ("done", 1))
        with chunked._connect(self.dsn, autocommit=True) as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass('public.src_n')").fetchone()[0])
            with self.assertRaises(ValueError):
                maintenance.vacuum(conn, tables="public.src; DROP TABLE src")

    def test_repack_declares_size_and_goes_lightest_first(self):
        with chunked._connect(self.dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE tiny (n int)")
            job = maintenance.repack(conn, tables="public.src,public.tiny", run="t")
        self.assertEqual(job.order, "lightest")
        sizes = {s.key: s.disk_gb for s in job.slices}
        self.assertGreater(sizes["public.src"], sizes["public.tiny"])
        hook = chunked.disk_floor("/x", 30, free_gb=lambda _p: 30 + sizes["public.src"] / 2)
        self.assertIsNone(hook(job.slices[1] if job.slices[1].key == "public.tiny"
                               else job.slices[0], []))

    def test_copy_sample_in_short_range_reads(self):
        name, target = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        with chunked._connect(target, autocommit=True) as conn:
            conn.execute("CREATE TABLE src (n int, pad text)")
            job = maintenance.copy_sample(conn, source_dsn=self.dsn, table="public.src",
                                          fraction="0.25", pages="20", run="t")
        self.assertGreater(len(job.slices), 1)
        out = chunked.run(target, job, workers=2, log=QUIET)
        self.assertEqual(out["state"], "done")
        with chunked._connect(target, autocommit=True) as conn:
            copied = conn.execute("SELECT count(*) FROM src").fetchone()[0]
        self.assertTrue(3000 < copied < 8000, copied)   # about a quarter of 20,000
        again = chunked.run(target, job, workers=2, log=QUIET)
        self.assertEqual(again["done"], 0)              # resumed: nothing copied twice
        with chunked._connect(target, autocommit=True) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM src").fetchone()[0], copied)


if __name__ == "__main__":
    unittest.main()
