"""A read request whose connection closes cancels its work (``operator_api.request_cancel``,
``call_limits.Cancel``): a job still queued for it never runs, a running one has its store
statement cut (SQLite at its next progress check, Postgres cancelled on the server), and
the worker serves the next reader at once. A write, and a shared screen read others join,
are never cut. Evidence label: ``isolated-fixture`` (made-up reader, in-memory SQLite, a
throwaway Postgres database)."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
import unittest

from fastapi import FastAPI

from operating import call_limits
from operating.call_limits import interruptible
from operator_api import backend as be
from operator_api.request_cancel import CancelOnDisconnect

READER = SimpleNamespace(session=SimpleNamespace(principals=frozenset({"reader-x"})))
SLOW = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
        "WHERE i < 4000000000) SELECT count(*) FROM n")


class View:
    def bind(self, session):
        pass

    def close(self):
        pass


def call(app, method: str, path: str, *, drop_after: float | None) -> tuple[int, float]:
    """(status, seconds) of one request to ``app`` whose client closes the connection
    ``drop_after`` seconds in (None: waits for the answer)."""
    async def go() -> tuple[int, float]:
        closed = asyncio.Event()
        got: dict = {}
        sent_request = False

        async def receive() -> dict:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            if drop_after is None:
                await closed.wait()
            else:
                await asyncio.sleep(drop_after)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                got["status"] = message["status"]
            if message["type"] == "http.response.body" and not message.get("more_body"):
                closed.set()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "headers": [],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
        began = time.monotonic()
        await app(scope, receive, send)
        return got.get("status", 0), time.monotonic() - began
    return asyncio.run(go())


class RequestCancelTests(unittest.TestCase):
    def setUp(self):
        self.worker = be.Worker(lambda session: View())
        self.thread = threading.Thread(target=self.worker.loop, daemon=True)
        self.thread.start()
        self.addCleanup(self.worker.stop)
        self.ran: list[str] = []
        self.ended: list[str] = []

        def slow(view):
            self.ran.append("slow")
            conn = sqlite3.connect(":memory:")
            interruptible(conn)
            try:
                return conn.execute(SLOW).fetchone()
            except sqlite3.OperationalError as error:
                self.ended.append(str(error))
                raise

        def queued(view):
            self.ran.append("queued")
            return "late"

        app = FastAPI()
        app.add_middleware(CancelOnDisconnect)
        worker = self.worker

        def read_or_busy(job):
            try:
                return {"got": worker.run(READER, job, timeout=30)}
            except TimeoutError:
                return {"got": "busy"}

        @app.get("/slow")
        def get_slow() -> dict:
            return read_or_busy(slow)

        @app.get("/queued")
        def get_queued() -> dict:
            return read_or_busy(queued)

        @app.post("/write")
        def post_write() -> dict:
            def write(view):
                time.sleep(0.8)
                self.ran.append("write cancelled" if call_limits.cancelled() else "write")
                return "done"
            return read_or_busy(write)
        self.app = app

    def test_a_dropped_read_frees_the_worker_and_its_queued_job_never_runs(self):
        # the worker is busy with one reader's read; a second read queues behind it; both
        # browsers leave: the running statement is cut, the queued job dropped unrun
        first = threading.Thread(target=call, args=(self.app, "GET", "/slow"),
                                 kwargs={"drop_after": 0.6})
        first.start()
        time.sleep(0.2)
        status, took = call(self.app, "GET", "/queued", drop_after=0.3)
        first.join(10)
        self.assertFalse(first.is_alive())
        self.assertLess(took, 3.0)
        self.assertEqual(self.ended, ["interrupted"])
        began = time.monotonic()
        self.assertEqual(self.worker.run(READER, lambda view: "next", timeout=5), "next")
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual(self.ran, ["slow"])   # the dropped queued job never ran

    def test_a_read_still_waited_for_and_a_dropped_write_run_whole(self):
        status, _took = call(self.app, "GET", "/queued", drop_after=None)
        self.assertEqual(status, 200)
        status, _took = call(self.app, "POST", "/write", drop_after=0.2)
        self.assertEqual(status, 200)
        self.assertEqual(self.ran, ["queued", "write"])

    def test_a_shared_screen_read_is_no_one_requests(self):
        """A read others join (``_screen_read`` with a key) is not cut by the request that
        started it; a read of its own (no key) is."""
        from operator_api.backend_serving import ServingMixin
        host = type("Host", (), {})()
        host._lock = threading.Lock()
        cancel = call_limits.Cancel()
        cancel.set()
        seen = []
        with call_limits.cancel_scope(cancel):
            box, _ = ServingMixin._screen_read(host, ("k",), lambda: seen.append(
                call_limits.cancelled()) or "page", True)
            own, _ = ServingMixin._screen_read(host, None, lambda: seen.append(
                call_limits.cancelled()) or "page", True)
        self.assertEqual((box.result(1), own.result(1)), ("page", "page"))
        self.assertEqual(seen, [False, True])

    def test_the_app_cancels_dropped_reads(self):
        from operator_api import app as app_module
        import inspect
        self.assertIn("CancelOnDisconnect", inspect.getsource(app_module.create_app))


class LongReadSlotTests(unittest.TestCase):
    """Long reads (``long=True``: Home's old-engine reads) run on all but one of the read
    workers sharing a queue, so a short page read never waits behind them (11-18 walk:
    Meetings waited 47 s behind three long Home reads). Opt-in only: the Ask pool's
    priority-1 jobs still run side by side."""

    def _pool(self, n):
        from operator_api.serving import Worker
        first = Worker(lambda _s: View())
        workers = [first] + [Worker(lambda _s: View(), first.jobs) for _ in range(n - 1)]
        for w in workers:
            threading.Thread(target=w.loop, daemon=True).start()
        self.addCleanup(lambda: [w.stop() for w in workers])
        return first

    def test_two_ask_jobs_run_side_by_side_on_a_two_worker_pool(self):
        first = self._pool(2)
        reader = SimpleNamespace(session=SimpleNamespace(principals=("p",)))
        both = threading.Barrier(2)

        def ask(_view):
            both.wait(5)   # broken (an error) unless the other job runs at the same time
            return "ask"
        jobs = [first.submit(reader, ask, priority=1) for _ in range(2)]
        self.assertEqual([f.result(10) for f in jobs], ["ask", "ask"])

    def test_a_short_read_runs_while_long_reads_fill_the_other_workers(self):
        first = self._pool(3)
        reader = SimpleNamespace(session=SimpleNamespace(principals=("p",)))
        release = threading.Event()
        lock = threading.Lock()
        running = [0, 0]   # now, most at once

        def long_read(_view):
            with lock:
                running[0] += 1
                running[1] = max(running[1], running[0])
            release.wait(10)
            with lock:
                running[0] -= 1
            return "long"
        longs = [first.submit(reader, long_read, priority=1, long=True) for _ in range(4)]
        time.sleep(0.3)
        began = time.monotonic()
        short = first.submit(reader, lambda _v: "short")
        self.assertEqual(short.result(3), "short")
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(running[1], 2)   # all but one of the three workers
        release.set()
        self.assertEqual([f.result(5) for f in longs], ["long"] * 4)


class WatchlistWorkerTests(unittest.TestCase):
    """The watchlist reads on the access worker (short jobs) unless the reader has signal
    rules: their alerts read the gap inbox, a long read, on the read queue instead."""

    def _run(self, rules):
        from unittest import mock
        from operator_api import watch
        ran = []

        class Worker:
            def __init__(self, name):
                self.name, self.stopped = name, threading.Event()

            def run(self, reader, job, timeout=None, priority=0):
                ran.append(self.name)
                return job(SimpleNamespace(_request=lambda: mock.MagicMock()))

        class Store:
            def __init__(self, _path):
                pass

            def follows(self, _r):
                return []

            def rules(self, _r):
                return rules

            def raw_alerts(self, _r, _n):
                return []

            def signal_reads(self, _r):
                return {}

        class Backend(watch.WatchMixin):
            access_worker = Worker("access")
            reads = Worker("reads")

        ctx = watch.WatchContext("r", "/nowhere", lambda _k: True, frozenset(),
                                 lambda hs: {}, lambda _ids: {}, lambda *_a: [])
        with mock.patch.object(watch, "view_context", lambda _v: ctx), \
                mock.patch.object(watch.wl, "WatchStore", Store), \
                mock.patch.object(watch.wl, "signal_alerts", lambda *a, **k: []):
            got = Backend().watchlist(SimpleNamespace(key="r"))
        return ran, got

    def test_no_signal_rule_reads_on_the_access_worker(self):
        ran, got = self._run([])
        self.assertEqual(ran, ["access"])
        self.assertTrue(got.configured)

    def test_signal_rules_read_on_the_read_queue(self):
        rule = {"rule_id": "w1", "text": "t", "spec": {"signal": "late_po"},
                "description": "d", "at": "2026-11-18T00:00:00Z", "state": "active",
                "note": None, "baseline_at": "2026-11-18T00:00:00Z"}
        ran, got = self._run([rule])
        self.assertEqual(ran, ["access", "reads"])
        self.assertEqual([r.rule_id for r in got.rules], ["w1"])


class PostgresCancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)

    def test_a_cancelled_statement_ends_on_the_server_and_the_connection_serves_on(self):
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        cancel = call_limits.Cancel()
        out: Future = Future()

        def run() -> None:
            with call_limits.cancel_scope(cancel):
                try:
                    out.set_result(pg.execute("SELECT pg_sleep(30)").fetchall())
                except BaseException as error:  # noqa: BLE001 - the test reads it
                    out.set_exception(error)
        began = time.monotonic()
        threading.Thread(target=run, daemon=True).start()
        time.sleep(0.5)
        cancel.set()
        with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
            out.result(10)
        self.assertLess(time.monotonic() - began, 6.0)
        self.assertEqual(pg.execute("SELECT 1").fetchall()[0][0], 1)
        with call_limits.cancel_scope(cancel):   # already gone: never started
            with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                pg.execute("SELECT 1")

    def test_a_cancel_landing_after_its_statement_never_cuts_the_next_one(self):
        """The cancel is sent from another thread: one set as its statement ends reached
        the server after it, and cancelled the connection's next statement (another
        reader's). The statement's end waits for its
        cancel and absorbs it; the next statement always runs."""
        import random
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        limit = pg.execute("SHOW statement_timeout").fetchall()[0][0]
        hits = cut = 0
        for _ in range(300):
            cancel = call_limits.Cancel()
            timer = threading.Timer(0.02 + random.uniform(-0.004, 0.004), cancel.set)
            timer.start()
            with call_limits.cancel_scope(cancel):
                try:
                    pg.execute("SELECT pg_sleep(0.02)").fetchall()
                except sqlite3.OperationalError:
                    cut += 1
            timer.join()
            try:   # the next job's statement, owned by no dropped request
                pg.execute("SELECT pg_sleep(0.005)").fetchall()
            except sqlite3.OperationalError:
                hits += 1
        self.assertEqual(hits, 0, f"{hits} of 300 next statements were cancelled")
        self.assertGreater(cut, 0)   # the cancel did land on its own statements
        # the session's own limit stands after every cancel
        self.assertEqual(pg.execute("SHOW statement_timeout").fetchall()[0][0], limit)

    def test_a_late_cancel_in_a_failed_transaction_is_spent_and_the_transaction_kept(self):
        """A cancelled statement's transaction already failed: its late cancel is spent
        on a refused statement, the connection is kept and the transaction stays failed
        for its owner's rollback."""
        import psycopg
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn)
        self.addCleanup(pg.close)
        pg.execute("SELECT 1").fetchall()
        conn = pg._conn
        was = conn.autocommit
        conn.autocommit = False
        try:
            with self.assertRaises(psycopg.errors.DivisionByZero):
                conn.execute("SELECT 1 / 0")
            self.assertEqual(conn.pgconn.transaction_status,
                             psycopg.pq.TransactionStatus.INERROR)
            pg._settle_cancel(conn, True)
            self.assertIs(pg._conn, conn)
            self.assertEqual(conn.pgconn.transaction_status,
                             psycopg.pq.TransactionStatus.INERROR)
            conn.rollback()
        finally:
            conn.autocommit = was
        self.assertEqual(pg.execute("SELECT 2").fetchall()[0][0], 2)

    def test_a_read_for_a_dropped_request_is_never_kept(self):
        from operator_api.serving import ReaderCache
        cache = ReaderCache()
        cancel = call_limits.Cancel()
        cancel.set()
        with call_limits.cancel_scope(cancel):
            cache.put(("k",), "short")
        self.assertIsNone(cache.get(("k",)))
        cache.put(("k",), "whole")
        self.assertEqual(cache.get(("k",)), "whole")


if __name__ == "__main__":
    unittest.main()


class AccessWarmTests(unittest.TestCase):
    """A reader who signs in after the access worker started has their policy decided on
    it at once (behind every live check), so their first door read never pays it; once
    per reader, never for the owner viewing as someone, never when already decided."""

    def test_a_new_reader_is_decided_once_on_the_access_worker(self):
        from operator_api.backend_warm import WarmMixin
        from operator_api.serving import Worker
        decided = []

        class Ws(View):
            def _request(self):
                import contextlib
                return contextlib.nullcontext()
        worker = Worker(lambda _s: Ws())
        threading.Thread(target=worker.loop, daemon=True).start()
        self.addCleanup(worker.stop)
        fake = SimpleNamespace(access_worker=worker, _lock=threading.Lock())
        reader = SimpleNamespace(session=SimpleNamespace(principals=("p:new",)))
        from unittest import mock
        with mock.patch("operator_api.backend.page_key",
                        side_effect=lambda view, key: decided.append(key)):
            self.assertTrue(WarmMixin._warm_access(fake, reader))
            self.assertFalse(WarmMixin._warm_access(fake, reader))   # asked already
            for _ in range(100):
                if decided:
                    break
                time.sleep(0.02)
            self.assertEqual(decided, [("policy",)])
            self.assertIn("p:new", worker.cache)
            for _ in range(100):   # marked served once its job ended
                if "p:new" in worker.served:
                    break
                time.sleep(0.02)
            self.assertIn("p:new", worker.served)
            fake.__dict__.pop("_access_warmed")
            self.assertFalse(WarmMixin._warm_access(fake, reader))   # decided there
            viewing = SimpleNamespace(session=SimpleNamespace(principals=("p:x",),
                                                              view_as=True))
            self.assertFalse(WarmMixin._warm_access(fake, viewing))


class SharedReadsWarmTests(unittest.TestCase):
    """The process-wide reads a first page waits on (the marked documents, the search
    coverage) are read once at start in the background; a failure leaves them to be read
    on first use, never fails the start."""

    def test_both_shared_reads_are_started_and_a_failure_is_swallowed(self):
        from unittest import mock
        from operator_api import backend_warm
        from operating.search import coverage, document_marks
        with mock.patch.object(document_marks, "enabled", return_value=True), \
                mock.patch.object(document_marks, "withheld_set") as marks, \
                mock.patch.object(coverage, "coverage") as cov:
            backend_warm.prewarm_shared_reads()
        marks.assert_called_once_with(())
        cov.assert_called_once_with(wait=0)
        with mock.patch.object(document_marks, "enabled", return_value=True), \
                mock.patch.object(document_marks, "withheld_set", side_effect=OSError):
            backend_warm.prewarm_shared_reads()   # no exception
