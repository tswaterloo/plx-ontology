"""The streaming duplicate regroup (``operating.embed.regroup``) on a real Postgres: the
same clusters as the in-memory ``dedup.groups``, written to search.chunk_groups only,
resumable, never past a chunk rewritten since its read."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from operating.chunking import body_sha, make_chunk  # noqa: E402
from operating.chunking import dedup as dd  # noqa: E402


def _pg_available() -> bool:
    return shutil.which("createdb") is not None and shutil.which("psql") is not None


LETTER = ("Hello team, the castile soap pallets for the spring order slipped because the "
          "screen print artwork for the bottle labels was not approved in time by the "
          "brand group, so the supplier moved the ship date and asked us to confirm the "
          "revised plan before Friday so the carrier can be booked for the new window. ")


def corpus(n: int) -> list[tuple[str, str]]:
    """(name, body) rows: per i an original, exact copies, a forward with one added line,
    a record differing only in its amount, and a short singleton."""
    rows = []
    for i in range(n):
        text = f"Order note {i}. " + LETTER + f"Total {i}0 dollars, as agreed."
        rows.append((f"orig{i}", text))
        if i % 2 == 0:
            rows.append((f"copy{i}", "  " + text.upper() + "  "))       # exact (normalised)
        if i % 3 == 0:
            rows.append((f"fwd{i}", text + " Forwarded by the buyer."))  # near copy
        if i % 5 == 0:
            rows.append((f"amt{i}", text.replace(f"Total {i}0", f"Total {i}5")))  # distinct
        rows.append((f"short{i}", f"ok {i}"))
    return rows


@unittest.skipUnless(_pg_available(), "no local Postgres")
class RegroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = f"plx_test_regroup_{os.getpid()}"
        if subprocess.run(["createdb", cls.db], capture_output=True).returncode != 0:
            raise unittest.SkipTest("createdb failed")
        cls.dsn = f"postgresql:///{cls.db}"
        done = subprocess.run([sys.executable, "-m", "operating.pg", "migrate"], cwd=ROOT,
                              env={**os.environ, "PLX_PG_DSN": cls.dsn},
                              capture_output=True, text=True)
        if done.returncode != 0:
            subprocess.run(["dropdb", "--if-exists", cls.db], capture_output=True)
            raise AssertionError("migrations failed: " + done.stderr[-2000:])

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["dropdb", "--if-exists", cls.db], capture_output=True)

    def setUp(self):
        from operating.embed import regroup as rg
        from operating.embed import store as st
        self.rg, self.st = rg, st
        self.conn = st.connect(self.dsn)
        self.addCleanup(self.conn.close)
        self.conn.execute("TRUNCATE search.chunks, search.chunk_groups")
        self.conn.commit()
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)

    def load(self, rows):
        chunks = [make_chunk(kind="mail", parts=("mail:box", name, "0"),
                             source_handle=f"fx://{name}", object_id=name, title=name,
                             author_role=None, occurred_at="2026-10-01T00:00:00Z",
                             header=f"Title: {name}", body=body, linked_ids=[],
                             audience_key="mail:box", markings=())
                  for name, body in rows]
        self.st.ChunkWriter(self.conn).write(chunks)
        self.conn.commit()
        return {c.chunk_id: c for c in chunks}

    def expected(self, chunks):
        rows = [(cid, body_sha(c.body), c.body) for cid, c in sorted(chunks.items())]
        return dd.groups(rows)

    def table(self):
        return dict(self.conn.execute(
            "SELECT g.chunk_id, g.dup_group FROM search.chunk_groups g JOIN search.chunks c "
            "ON c.chunk_id = g.chunk_id AND c.content_sha = g.content_sha").fetchall())

    def chunks_snapshot(self):
        return self.conn.execute("SELECT chunk_id, xmin::text, dup_group, updated_at FROM "
                                 "search.chunks ORDER BY chunk_id").fetchall()

    def regroup(self, **kw):
        kw.setdefault("workers", 2)
        kw.setdefault("shards", 4)
        kw.setdefault("ranges", 3)
        report = self.rg.run(self.conn, self.dsn, self.work, **kw)
        self.conn.commit()
        return report

    def test_same_clusters_as_the_in_memory_grouping_and_no_chunk_row_written(self):
        chunks = self.load(corpus(12))
        before = self.chunks_snapshot()
        report = self.regroup()
        want = self.expected(chunks)
        self.assertEqual(self.table(), want)
        self.assertEqual(self.chunks_snapshot(), before)     # search.chunks untouched
        self.assertEqual(report["written"]["insert"], len(want))
        self.assertEqual(report["rows_read"], len(chunks))
        names = {cid: c.object_id for cid, c in chunks.items()}
        got = {names[cid]: g for cid, g in self.table().items()}
        self.assertEqual(got["orig0"], got["copy0"])
        self.assertEqual(got["orig0"], got["fwd0"])          # near copy confirmed
        self.assertNotIn("amt5", got)                         # distinct record stays apart
        self.assertNotIn("amt0", got)
        self.assertFalse(any(n.startswith("short") for n in got))
        # a second run with nothing changed writes nothing
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        again = self.regroup(workers=1)
        self.assertEqual(again["written"], {"stale": 0, "remove": 0, "change": 0, "insert": 0})
        m = self.rg.measure(self.conn, n=200, seed=1115)
        self.assertEqual((m["wrongly_merged"], m["missed"]), (0, 0))
        self.assertGreater(m["groups_checked"], 0)

    def test_cardinality_no_row_lost_or_duplicated(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.conn.execute("TRUNCATE search.chunks, search.chunk_groups")
                self.conn.commit()
                shutil.rmtree(self.work, ignore_errors=True)
                chunks = self.load(corpus(n))
                report = self.regroup(ranges=7)
                want = self.expected(chunks)
                got = self.table()
                self.assertEqual(got, want)
                self.assertEqual(report["rows_read"], len(chunks))
                self.assertEqual(self.conn.execute(
                    "SELECT count(*) FROM search.chunk_groups").fetchone()[0], len(want))

    def test_a_run_that_died_resumes_and_matches_a_whole_run(self):
        chunks = self.load(corpus(9))
        the_plan = self.rg.plan(self.conn, 1, 3)
        the_plan["shards"] = 4
        self.work.mkdir(exist_ok=True)
        self.rg._atomic_json(self.work / "plan.json", the_plan)
        lo, hi = the_plan["ranges"][0]
        self.rg.map_range(self.dsn, str(self.work), 0, lo, hi, 4)   # range 0 finished
        dead = self.work / "map" / "r00001.tmp"                   # range 1 died mid-way
        dead.mkdir(parents=True)
        (dead / "e.0").write_text("garbage\tnot\ta row\n")
        report = self.regroup(workers=1)
        self.assertTrue(report["resumed"])
        self.assertEqual(report["rows_read"], len(chunks))
        self.assertEqual(self.table(), self.expected(chunks))

    def test_a_chunk_rewritten_after_its_read_gets_no_cluster_and_old_rows_go(self):
        chunks = self.load(corpus(6))
        orphan = ("mail:gone", "d:0000", "x")
        self.conn.execute("INSERT INTO search.chunk_groups VALUES (%s, %s, %s)", orphan)
        self.conn.commit()
        rg = self.rg
        real = rg.write

        def rewrite_then_write(conn, rows, dry=False):
            # the delta rewrites copy0 between the regroup's read and its write
            conn.execute("UPDATE search.chunks SET content_sha = repeat('e', 64), body = 'other' "
                         "WHERE object_id = 'copy0'")
            conn.commit()
            return real(conn, rows, dry=dry)

        rg.write = rewrite_then_write
        try:
            report = self.regroup(workers=1)
        finally:
            rg.write = real
        names = {cid: c.object_id for cid, c in chunks.items()}
        got = {names.get(cid, cid): g for cid, g in self.table().items()}
        self.assertNotIn("copy0", got)
        self.assertEqual(got["orig0"], got["fwd0"])
        self.assertEqual(report["written"]["stale"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM search.chunk_groups WHERE chunk_id = 'mail:gone'").fetchone())

    def test_dry_writes_nothing_and_a_second_run_is_refused_while_one_holds_the_lock(self):
        self.load(corpus(4))
        report = self.regroup(dry=True, workers=1)
        self.assertGreater(report["would_write"]["insert"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM search.chunk_groups").fetchone()[0], 0)
        import fcntl
        with open(self.work / "run.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.regroup(fresh=False), {"skipped": "regroup_running"})

    def test_another_runs_files_are_refused_and_bad_arguments_fail_closed(self):
        self.work.mkdir(exist_ok=True)
        (self.work / "plan.json").write_text('{"version": 0, "shards": 4, "ranges": []}')
        with self.assertRaises(self.rg.RegroupError):
            self.regroup()
        env = {**os.environ, "PLX_PG_DSN": self.dsn}
        for bad in (["--workers", "0"], ["--shards", "0"], ["--ranges", "-1"], ["--nope"]):
            done = subprocess.run([sys.executable, "-m", "operating.embed", "regroup",
                                   "--work", str(self.work), *bad], cwd=ROOT, env=env,
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, bad)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM search.chunk_groups").fetchone()[0], 0)


class ZeroTailHashTests(unittest.TestCase):
    """A body hash whose last byte is 0x00 must survive the numpy round trip (fixed-width
    byte strings drop trailing NULs), or its text can never join a cluster."""

    def test_hash_ending_in_zero_byte_confirms_and_joins(self):
        import numpy as np
        from operating.embed import regroup as rg
        a = next(t for t in (LETTER + f"zero {i}" for i in range(100000))
                 if body_sha(t).endswith("00"))
        b = a + " Forwarded."
        self.assertFalse(body_sha(b).endswith("00") and body_sha(b) == body_sha(a))
        sa, sb = bytes.fromhex(body_sha(a)), bytes.fromhex(body_sha(b))
        self.assertEqual(sa[-1], 0)
        with tempfile.TemporaryDirectory() as d:
            w = Path(d)
            (w / "conf").mkdir()
            pairs = np.zeros(1, rg.PAIR_DT)
            pairs["a"], pairs["b"] = sorted([sa, sb])[0], sorted([sa, sb])[1]
            np.save(w / "pairs.npy", pairs)
            reps = np.zeros(2, [("sha", "V32"), ("cid", "S96")])
            reps["sha"] = [bytes(pairs["a"][0]), bytes(pairs["b"][0])]
            reps = np.sort(reps, order="sha")
            reps["cid"] = [b"c1", b"c2"] if bytes(reps["sha"][0]) == sa else [b"c2", b"c1"]
            np.save(w / "reps.npy", reps)
            bodies = {"c1": a, "c2": b}
            done = rg.confirm_batch("", str(w), 0, 0, 1, body_of=lambda ids: bodies)
            self.assertEqual(done["confirmed"], 1)
            self.assertEqual(done["stale"], 0)
            rg.components(str(w))
            comp = np.load(w / "comp.npy")
            self.assertEqual({bytes(x) for x in comp["sha"]}, {sa, sb})
            self.assertEqual(len({bytes(x) for x in comp["root"]}), 1)

    def test_second_run_closes_its_lock_file(self):
        import fcntl
        from operating.embed import regroup as rg
        with tempfile.TemporaryDirectory() as d:
            held = open(Path(d) / "run.lock", "w")
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            opened = []
            real = open

            def spy(*a, **k):
                f = real(*a, **k)
                opened.append(f)
                return f
            rg.open = spy
            try:
                self.assertEqual(rg.run(None, "", d), {"skipped": "regroup_running"})
            finally:
                del rg.open
                held.close()
            self.assertTrue(opened and all(f.closed for f in opened))


if __name__ == "__main__":
    unittest.main()
