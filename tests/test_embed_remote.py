"""The remote embedding pass (``python3 -m operating.embed remote``) against a throwaway
Postgres with the real migrations, its hosts played by local worker processes that speak
``remote_worker``'s protocol with a deterministic encoder."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from operating.pg import scratch_db

scratch_db.use_test_cluster()   # the test cluster when one runs (bare runs too)

ROOT = Path(__file__).resolve().parents[1]
MODEL = "BAAI/bge-m3"

FAKE = textwrap.dedent('''
    import hashlib, json, sys
    import numpy as np
    sys.path.insert(0, sys.argv[1])
    from operating.embed import remote_worker as rw
    opts = json.loads(sys.argv[2])
    log = open(opts["log"], "a") if opts.get("log") else None
    calls = [0]

    def encode(texts):
        calls[0] += 1
        if log:
            log.write(json.dumps(len(texts)) + "\\n"); log.flush()
        if opts.get("die_after") is not None and calls[0] > opts["die_after"]:
            sys.exit(3)
        if opts.get("hang_after") is not None and calls[0] > opts["hang_after"]:
            import time; time.sleep(60)
        return np.stack([vec(t) for t in texts])

    def vec(text):
        seed = int.from_bytes(hashlib.sha256((opts.get("salt", "") + text).encode()).digest()[:8], "big")
        return np.random.default_rng(seed).standard_normal(1024)

    hello = {"model": "BAAI/bge-m3", "dim": 1024, "device": "fake", "precision": "fp32",
             "max_seq_length": rw.MAX_SEQ_LENGTH, "max_chars": rw.LOCAL_MAX_CHARS,
             "revision": opts.get("revision", rw.REVISIONS["BAAI/bge-m3"])}
    rw.serve(sys.stdin.buffer, sys.stdout.buffer, encode, hello)
''')


def expected(text: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    v = np.random.default_rng(seed).standard_normal(1024).astype(np.float32)
    return v / np.linalg.norm(v)


def _pg_available() -> bool:
    return shutil.which("createdb") is not None and shutil.which("psql") is not None


@unittest.skipUnless(_pg_available(), "no local Postgres")
class RemotePassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = f"plx_test_remote_{os.getpid()}"
        made = subprocess.run(["createdb", cls.db], capture_output=True, text=True)
        if made.returncode != 0:
            raise unittest.SkipTest("createdb failed")
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env["PLX_PG_DSN"] = f"postgresql:///{cls.db}"
        done = subprocess.run([sys.executable, "-m", "operating.pg", "migrate"], cwd=ROOT,
                              env=env, capture_output=True, text=True)
        if done.returncode != 0:
            subprocess.run(["dropdb", "--if-exists", cls.db], capture_output=True)
            raise AssertionError(f"migrate failed: {done.stderr[-500:]}")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["dropdb", "--if-exists", cls.db], capture_output=True)

    def setUp(self):
        from operating.embed import store as st
        self.st = st
        self.dsn = f"postgresql:///{self.db}"
        self.conn = st.connect(self.dsn)
        self.addCleanup(self.conn.close)
        self.conn.execute("TRUNCATE search.chunks, search.entities")
        self.conn.commit()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "fake.py").write_text(FAKE, encoding="utf-8")
        env = mock.patch.dict(os.environ, {"PLX_PG_DSN": self.dsn, "PLX_EMBED_MODEL": MODEL,
                                           "PLX_ENV_FILE": str(self.tmp / "none.env")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OPENAI_API_KEY", None)

    def add(self, n, body, kind="mail", vector=None):
        from operating.chunking import content_sha
        header = f"Source: x\nTitle: T{n % 7}"
        self.conn.execute(
            "INSERT INTO search.chunks (chunk_id, source_handle, kind, header, body, content_sha,"
            " audience_key, authority, updated_at, embedding, embed_model) VALUES "
            "(%s, %s, %s, %s, %s, %s, 'mail:box-a', 2, now(), %s::halfvec(1024), %s)",
            (f"c{n}", f"fx://{n}", kind, header, body, content_sha(header, body),
             None if vector is None else self.st.vector_text(vector, 1024),
             None if vector is None else MODEL))
        self.conn.commit()
        return header + "\n\n" + body

    def worker(self, **opts):
        opts.setdefault("log", str(self.tmp / "calls.log"))
        return f"{sys.executable} {self.tmp / 'fake.py'} {ROOT} '{json.dumps(opts)}'"

    def run_pass(self, *extra, workers=None):
        from operating.embed.remote import main
        workers = workers or [self.worker(), self.worker()]
        argv = ["--state", str(self.tmp / "state"), "--host", "a", "--host", "b",
                "--page", "4", "--block", "7", "--writers", "2", "--inflight", "2",
                "--report-seconds", "0.2", "--start-wait-seconds", "1", "--min-free-gb", "0",
                *extra]
        for cmd in workers:
            argv += ["--local-cmd", cmd]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return code, lines[-1] if lines else {}

    def vectors(self):
        rows = self.conn.execute("SELECT chunk_id, embedding::text, embed_model FROM "
                                 "search.chunks").fetchall()
        self.conn.commit()
        return {cid: (None if v is None else np.asarray(json.loads(v), dtype=np.float32), m)
                for cid, v, m in rows}

    def assert_embedded(self, texts):
        got = self.vectors()
        for cid, text in texts.items():
            vec, model = got[cid]
            self.assertEqual(model, MODEL, cid)
            self.assertGreater(float(vec @ expected(text)), 0.9999, cid)

    def test_every_pending_hash_stored_once_at_any_size_and_again_nothing(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.conn.execute("TRUNCATE search.chunks")
                self.conn.commit()
                (self.tmp / "calls.log").unlink(missing_ok=True)
                texts = {f"c{i}": self.add(i, f"body {i % max(1, n - 3)} " + "x" * (i * 37 % 900))
                         for i in range(n)}
                kept = expected("anything else")
                self.add(1000, "already embedded", vector=kept)
                self.add(1001, "a reference code", kind="record_words")
                distinct = len(set(texts.values()))
                code, report = self.run_pass("--from-start", "--verify", "0")
                self.assertEqual(code, 0, report)
                self.assertEqual(report["texts"], distinct)       # one encode per hash
                self.assertEqual(report["rows"], n)               # every row of a hash
                self.assert_embedded(texts)
                got = self.vectors()
                self.assertGreater(float(got["c1000"][0] @ kept), 0.9999)
                self.assertEqual(got["c1001"], (None, None))      # words only: never
                sent = sum(json.loads(x) for x in
                           (self.tmp / "calls.log").read_text().split())
                self.assertEqual(sent, distinct)
                code, again = self.run_pass("--from-start", "--verify", "0")
                self.assertEqual((code, again["texts"], again["rows"]), (0, 0, 0))

    def test_a_dying_host_hands_its_pages_on_and_a_run_with_none_left_resumes(self):
        texts = {f"c{i}": self.add(i, f"text {i}") for i in range(40)}
        code, report = self.run_pass("--verify", "0",
                                     workers=[self.worker(die_after=1), self.worker()])
        self.assertEqual(code, 0, report)
        self.assertEqual(report["hosts_alive"], 1)
        self.assert_embedded(texts)
        self.conn.execute("UPDATE search.chunks SET embedding = NULL, embed_model = NULL")
        self.conn.commit()
        code, report = self.run_pass("--from-start", "--verify", "0",
                                     workers=[self.worker(die_after=2),
                                              self.worker(die_after=2)])
        self.assertEqual(code, 1)
        self.assertEqual(report["failed"], "no_host_left")
        stored = sum(1 for v, _ in self.vectors().values() if v is not None)
        self.assertGreater(stored, 0)
        self.assertLess(stored, 40)
        code, report = self.run_pass("--verify", "0")          # from the cursor
        self.assertEqual(code, 0, report)
        self.assert_embedded(texts)

    def test_a_hanging_host_ends_the_run_instead_of_waiting_forever(self):
        for i in range(20):
            self.add(i, f"text {i}")
        code, report = self.run_pass("--verify", "0", "--stall-seconds", "2",
                                     workers=[self.worker(hang_after=1),
                                              self.worker(hang_after=1)])
        self.assertEqual((code, report["failed"]), (1, "stalled"))

    def test_a_full_disk_holds_every_request_back(self):
        self.add(1, "pending")
        free = shutil.disk_usage(self.tmp).free / 1e9
        code, report = self.run_pass("--verify", "0", "--stall-seconds", "1",
                                     "--max-seconds", "3", "--min-free-gb", str(free + 1000))
        self.assertEqual((code, report.get("ended"), report.get("texts")),
                         (0, "max_seconds", 0), report)
        self.assertEqual(self.vectors()["c1"], (None, None))
        self.assertEqual((self.tmp / "calls.log").read_text(), "")   # no request sent
        code, report = self.run_pass("--verify", "0", "--min-free-gb", "0")
        self.assertEqual((code, report["texts"]), (0, 1))

    def test_a_standby_too_far_behind_holds_every_request_back(self):
        """11-21 live: the backfill wrote 23 MB/s of WAL, 2.8 reached the standby (18 GB
        behind of a 40 GB slot cap). Past the WAL budget's pause line nothing is sent; an
        unreadable lag holds it back too; back under the resume line the pass goes on."""
        from operating.pg import wal_budget
        self.add(1, "pending")
        for lag in ({"return_value": (9 * 1024 ** 3, "plx_s7d3d")},
                    {"side_effect": RuntimeError("no stats")}):
            with self.subTest(lag=lag), mock.patch.object(wal_budget, "lag", **lag):
                code, report = self.run_pass("--verify", "0", "--stall-seconds", "1",
                                             "--max-seconds", "3")
                self.assertEqual((code, report.get("ended"), report.get("texts")),
                                 (0, "max_seconds", 0), report)
                self.assertTrue(report["paused"], report)
                self.assertEqual(self.vectors()["c1"], (None, None))
                self.assertFalse((self.tmp / "calls.log").exists()
                                 and (self.tmp / "calls.log").read_text())   # none sent
        with mock.patch.object(wal_budget, "lag", return_value=(1024 ** 3, "plx_s7d3d")):
            code, report = self.run_pass("--verify", "0")
        self.assertEqual((code, report["texts"]), (0, 1))

    def test_dry_run_ships_no_pending_text_and_writes_nothing(self):
        kept = self.add(0, "stored before")
        self.conn.execute("UPDATE search.chunks SET embedding = %s::halfvec(1024), "
                          "embed_model = %s", (self.st.vector_text(expected(kept), 1024), MODEL))
        self.conn.commit()
        for i in range(1, 6):
            self.add(i, f"pending {i}")
        before = self.vectors()
        code, report = self.run_pass("--dry-run", "--count")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["pending_before"], 5)
        self.assertEqual(set(report["verify_min_cosine"]), {"a", "b"})
        sent = [json.loads(x) for x in (self.tmp / "calls.log").read_text().split()]
        self.assertEqual(sent, [1, 1])                    # the stored row, once per host
        after = self.vectors()
        self.assertEqual({k: v[1] for k, v in after.items()},
                         {k: v[1] for k, v in before.items()})
        self.assertFalse((self.tmp / "state" / "search" / "remote_embed.json").exists())

    def test_a_host_unlike_the_indexing_model_is_refused_before_any_text(self):
        kept = self.add(0, "stored before")
        self.conn.execute("UPDATE search.chunks SET embedding = %s::halfvec(1024), "
                          "embed_model = %s", (self.st.vector_text(expected(kept), 1024), MODEL))
        self.conn.commit()
        self.add(1, "pending")
        cases = {"revision": [self.worker(), self.worker(revision="other")],
                 "parity": [self.worker(), self.worker(salt="other weights")]}
        for case, workers in cases.items():
            with self.subTest(case=case):
                (self.tmp / "calls.log").unlink(missing_ok=True)
                code, report = self.run_pass(workers=workers)
                self.assertEqual(code, 2, report)
                self.assertEqual(self.vectors()["c1"], (None, None))
                log = self.tmp / "calls.log"
                sent = log.read_text().split() if log.exists() else []
                self.assertEqual(sum(map(int, sent)), 0 if case == "revision" else 2)

    def test_another_backfill_holding_the_lock_refuses_the_run(self):
        import fcntl
        self.add(1, "pending")
        lock = self.tmp / "state" / "search" / "backfill.lock"
        lock.parent.mkdir(parents=True)
        with open(lock, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            code, report = self.run_pass("--verify", "0")
        self.assertEqual((code, report.get("refused")), (2, "another_backfill_running"))
        self.assertEqual(self.vectors()["c1"], (None, None))

    # --- the search follower's step 3 on a Spark GPU (operating.embed.spark_embed) ----

    def follow_embed(self, workers, *, hosts=("a",), budget=60.0, **fields):
        """``follow._embed_spark_first`` over the pending rows: the Spark path through the
        real ``embed`` with the played host; the box path recorded (no CPU model here)."""
        import argparse
        import functools
        from operating.embed import follow, spark_embed
        from operating.embed.__main__ import embed as real_embed
        local = []

        def embed(conn, values, embedder=None, **kw):
            if embedder is None:
                local.append(kw["budget_seconds"])
                return {"local": True}
            return real_embed(conn, values, embedder=embedder, **kw)
        opened = functools.partial(spark_embed.embedder, local_cmd=workers)
        options = {"embed_seconds": budget, "embed_here": False, "embed_batch": 0,
                   "embed_rows": 0, "embed_write_mb_per_hour": 0.0}
        options.update(fields)
        args = argparse.Namespace(**options)
        with mock.patch.object(spark_embed, "fit_hosts", lambda state: list(hosts)), \
                mock.patch.object(spark_embed, "embedder", opened):
            got = follow._embed_spark_first(args, self.tmp, self.conn,
                                            {"PLX_EMBED_MODEL": MODEL}, embed)
        return got, local

    def test_the_followers_embedding_runs_on_a_fit_spark_and_never_on_the_box(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.conn.execute("TRUNCATE search.chunks")
                self.conn.commit()
                texts = {f"c{i}": self.add(i, f"follow {n} {i} " + "y" * (i * 31 % 700))
                         for i in range(n)}
                got, local = self.follow_embed([self.worker()])
                self.assertEqual(got.get("on"), "spark", got)
                self.assertEqual(local, [])
                self.assert_embedded(texts)          # no row lost, each its own vector

    def test_the_followers_write_budget_bounds_rows_and_persists_across_runs(self):
        """One hash shared by six chunks can be a write burst by itself (every wide row takes
        the vector). A one-row batch cap writes exactly one row; the measured WAL fills the
        tiny rolling budget, and the next one-shot run starts no host and writes nothing."""
        for n in (0, 7, 14, 21, 28, 35):       # equal headers and bodies: one content hash
            self.add(n, "shared across recipients")
        fields = {"embed_batch": 1, "embed_rows": 1, "embed_write_mb_per_hour": 0.000001}
        got, local = self.follow_embed([self.worker()], **fields)
        self.assertEqual((got.get("on"), got.get("texts_embedded"), got.get("rows_updated")),
                         ("spark", 1, 1), got)
        self.assertGreater(got.get("wal_bytes") or 0, 0, got)
        self.assertTrue(got["write_budget"]["blocked"], got)
        self.assertEqual(got["pending_after"], 1, got)       # five rows of the hash remain
        self.assertEqual(local, [])
        stored = [cid for cid, (vec, _model) in self.vectors().items() if vec is not None]
        self.assertEqual(len(stored), 1)
        (self.tmp / "calls.log").unlink(missing_ok=True)
        again, local = self.follow_embed([self.worker()], **fields)
        self.assertEqual(again.get("skipped"), "write_budget_exhausted", again)
        self.assertEqual(local, [])
        log = self.tmp / "calls.log"
        self.assertFalse(log.exists() and log.read_text())   # no host, no text sent
        stored_again = [cid for cid, (vec, _model) in self.vectors().items()
                        if vec is not None]
        self.assertEqual(stored_again, stored)

    def test_the_write_budget_defers_the_unbounded_reuse_update(self):
        """A duplicate of an already embedded hash is normally copied by one unbounded UPDATE.
        Under the write budget it is encoded again in a bounded batch instead; only the rows
        still missing the model are written."""
        body = "same body in two inboxes"
        full = "Source: x\nTitle: T0" + "\n\n" + body
        self.add(0, body, vector=expected(full))
        self.add(7, body)                                   # same header and body
        got, _local = self.follow_embed([self.worker()], embed_batch=16, embed_rows=32,
                                        embed_write_mb_per_hour=256)
        self.assertEqual((got.get("reused_rows"), got.get("rows_updated")), (0, 1), got)
        self.assertEqual(got.get("reuse_deferred"), "write_budget", got)
        sent = [json.loads(x) for x in (self.tmp / "calls.log").read_text().split()]
        self.assertEqual(sent, [1, 1])                      # parity probe, then the hash
        self.assertTrue(all(vec is not None for vec, _ in self.vectors().values()))

    def test_a_host_unlike_this_box_or_off_in_parity_is_not_used_and_the_box_embeds(self):
        self.add(1, "pending")
        got, local = self.follow_embed([self.worker(revision="other")])
        self.assertEqual((got.get("on"), len(local)), ("box", 1))
        self.assertEqual(self.vectors()["c1"], (None, None))   # nothing from that host
        # same model words, other vectors: refused by the parity check on stored rows
        self.add(2, "stored", vector=expected("Source: x\nTitle: T2\n\nstored"))
        got, local = self.follow_embed([self.worker(salt="drift")])
        self.assertEqual((got.get("on"), len(local)), ("box", 1))
        self.assertEqual(self.vectors()["c1"], (None, None))

    def test_a_host_failing_part_way_keeps_what_it_stored_and_the_box_does_the_rest(self):
        from operating.embed import __main__ as em
        texts = {f"c{i}": self.add(i, f"part {i}") for i in range(12)}
        with mock.patch.object(em, "EMBED_BATCH", 4):
            got, local = self.follow_embed([self.worker(die_after=1)], budget=60.0)
        self.assertEqual(got.get("on"), "box_after_spark", got)
        self.assertEqual(len(local), 1)
        self.assertLess(local[0], 60.0)              # only the budget left
        stored = [cid for cid, (vec, _) in self.vectors().items() if vec is not None]
        self.assertTrue(stored)                      # the first batch stayed
        self.assert_embedded({cid: texts[cid] for cid in stored})

    def test_no_fit_host_or_an_unreadable_health_file_embeds_on_the_box(self):
        from operating.embed import spark_embed
        self.assertEqual(spark_embed.fit_hosts(self.tmp), [])     # no health file
        self.assertEqual(spark_embed.fit_hosts(self.tmp, load=lambda state: {
            "hosts": {"s1": {"embed": False}, "s2": {"embed": "yes"}}}), [])
        self.assertEqual(spark_embed.fit_hosts(self.tmp, load=lambda state: {
            "hosts": {"s1": {"embed": True}}}), ["s1"])
        self.add(1, "pending")
        got, local = self.follow_embed([self.worker()], hosts=())
        self.assertEqual((got.get("on"), len(local)), ("box", 1))



class WorkerParityTests(unittest.TestCase):
    def test_worker_constants_match_the_indexing_provider(self):
        from operating.embed import models as md
        from operating.embed import remote_worker as rw
        self.assertEqual(rw.LOCAL_MODELS, md.LOCAL_MODELS)
        self.assertEqual(rw.LOCAL_MAX_CHARS, md.LOCAL_MAX_CHARS)
        source = (ROOT / "operating/embed/models.py").read_text(encoding="utf-8")
        self.assertIn(f"st.max_seq_length = {rw.MAX_SEQ_LENGTH}", source)


if __name__ == "__main__":
    unittest.main()
