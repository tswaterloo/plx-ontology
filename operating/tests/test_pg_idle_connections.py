"""A process's kept Postgres connections are closed once idle past
``pool.idle_keep_seconds`` (operating/pg/pool.py ``reap_idle``): the plx.sql/pages pool,
the search retrievers' pool (operating/search/core.py) and the entity index's
(operating/query_understanding.py). Live 11-18: Ask hosts kept warm or spare by the API
held the connections of their last question for as long as they lived, filled
max_connections and the live API was refused connections for 26 minutes.
Evidence label: ``isolated-fixture`` (a throwaway local Postgres database for the pool's
server-side count; stand-in connections for the other two)."""

from __future__ import annotations

from types import SimpleNamespace
import os
import threading
import time
import unittest
from unittest import mock

try:
    import psycopg
    from operating.pg import pool as pg_pool
    from operating.search import core
except ImportError:  # psycopg not installed
    pg_pool = None


class Conn:
    def __init__(self):
        self.closed = False
        self.info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)

    def execute(self, sql, *args):
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


@unittest.skipIf(pg_pool is None, "psycopg not installed")
class IdleConnectionTests(unittest.TestCase):
    def later(self, seconds):
        return time.monotonic() + seconds

    def test_a_pool_frees_its_server_slots_once_idle_and_lends_again(self):
        from operating.tests import pg_testdb
        try:
            name, dsn = pg_testdb.create_db()
        except Exception as exc:  # noqa: BLE001 - no local server: nothing to count
            self.skipTest(f"no test database: {type(exc).__name__}")
        self.addCleanup(pg_testdb.drop_db, name)
        # no session setup: the throwaway database has no pgvector type (Spark scratch DB)
        pool = pg_pool.Pool(dsn, max_size=4, configure=lambda conn: None)
        self.addCleanup(pool.close)

        def backends(want, *, stays=False):
            """The server's backends on the database; a closed client's backend leaves the
            list a moment after its close (4 of 16 parallel runs read it early), so ``want``
            is waited for, up to 5 s, and the count read then is returned. ``stays``: a count
            that must NOT drop is read once after that settling time instead."""
            if stays:
                time.sleep(1.0)
            deadline = time.monotonic() + (0 if stays else 5)
            with psycopg.connect(dsn, autocommit=True) as c:
                while True:
                    n = c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = "
                                  "current_database() AND pid <> pg_backend_pid()"
                                  ).fetchone()[0]
                    if n == want or time.monotonic() > deadline:
                        return n
                    time.sleep(0.05)
        held, lent = threading.Barrier(3), []

        def borrow():
            with pool.connection() as conn:
                conn.execute("SELECT 1")
                held.wait(10)
                lent.append(conn)
        threads = [threading.Thread(target=borrow) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(backends(pg_pool.IDLE_KEPT_MAX), pg_pool.IDLE_KEPT_MAX)   # 3 used, 2 kept: the cap
        self.assertEqual(pool._open, pg_pool.IDLE_KEPT_MAX)
        pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS / 2))
        self.assertEqual(backends(2, stays=True), 2)   # not idle long enough yet
        pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS + 1))
        self.assertEqual(backends(0), 0)          # an idle process holds no server slot
        self.assertEqual(pool._open, 0)
        with pool.connection() as conn:          # and lends a new one as before
            self.assertEqual(conn.execute("SELECT 41 + 1").fetchone()[0], 42)
            pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS + 1))
            self.assertFalse(conn.closed)        # a lent connection is never reaped
        self.assertEqual(backends(1), 1)
        with mock.patch.dict(os.environ, {pg_pool.IDLE_KEEP_ENV: "0"}):
            pg_pool.reap_now(self.later(10 ** 6))
        self.assertEqual(backends(1, stays=True), 1)   # 0: kept for ever, as before

    def test_the_search_pool_closes_idle_connections_and_keeps_fresh_ones(self):
        made = []

        def connect(target):
            made.append(Conn())
            return made[-1]
        with mock.patch.dict(core._IDLE, clear=True), \
                mock.patch.object(core, "connect", side_effect=connect):
            all3 = threading.Barrier(3)

            def job(conn):
                all3.wait(10)   # the lists hold a connection at once, as in a search
                return [("chunk-a", 1.0)]
            jobs = {name: job for name in ("bm25", "fuzzy", "dense")}
            core._run(jobs, "postgresql://made-up/db", {}, [])
            self.assertEqual(len(made), 3)
            self.assertEqual(sum(c.closed for c in made), 1)   # 2 kept: the cap
            pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS / 2))
            self.assertEqual(sum(c.closed for c in made), 1)
            pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS + 1))
            self.assertTrue(all(c.closed for c in made))
            self.assertEqual(core._IDLE["postgresql://made-up/db"], [])
            core._run(jobs, "postgresql://made-up/db", {}, [])
            self.assertEqual(len(made), 6)       # the next search connects anew

    def test_the_entity_index_closes_idle_connections(self):
        from operating.query_understanding import PgEntityIndex
        made = []

        def connect(dsn):
            made.append(Conn())
            return made[-1]
        index = PgEntityIndex("postgresql://made-up/db", connect=connect)
        index._run("SELECT 1", ())
        self.assertEqual((len(made), made[0].closed), (1, False))
        pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS + 1))
        self.assertTrue(made[0].closed)
        index._run("SELECT 1", ())
        self.assertEqual(len(made), 2)


    def test_the_exclusion_lookup_closes_its_login_once_idle(self):
        """``people_excluded``'s per-process lookup connection was kept for the process's
        life: one idle slot of the role's 100 in every API, warm-up and Ask host process
        (571 s idle, 11-21). Now closed past ``idle_keep_seconds`` and opened again."""
        from operating.pg import people_excluded
        made = []

        class Rows(Conn):
            def execute(self, sql, *args):
                return self

            def fetchall(self):
                return [("h@x", 3)]

        def login(*a, **k):
            made.append(Rows())
            return made[-1]
        lookup = people_excluded._Lookup()
        with mock.patch("psycopg.connect", side_effect=login):
            self.assertEqual(lookup.versions(["h@x"]), {"h@x": 3})
            pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS / 2))
            self.assertFalse(made[0].closed)          # used a moment ago: kept
            pg_pool.reap_now(self.later(pg_pool.IDLE_KEEP_SECONDS + 1))
            self.assertTrue(made[0].closed)           # idle past the keep: its slot freed
            self.assertEqual(lookup.versions(["h@x"]), {"h@x": 3})
            self.assertEqual(len(made), 2)            # the next lookup logs in again

    def test_the_marked_and_own_issued_sets_wait_for_a_slot_at_the_role_limit(self):
        """Both sets were read on a login of their own, refused at once at the role's
        limit: the marked set then read as unreadable and every kept page's access check
        said no for 30 s (live 11-21 9:45 AM ET, Home, briefing, team read whole again);
        now the login waits for a slot (``pool.when_free``) and the set is read."""
        from operating.search import document_marks
        from operating.pg import own_issued
        full = psycopg.OperationalError('FATAL:  too many connections for role "plx_api"')
        tries = []

        class Rows(Conn):
            def execute(self, sql, *args):
                self.sql = sql
                return self

            def fetchall(self):
                return ([("Doc:1", "sp://d/1#p2", ["finance"])] if "chunks" in self.sql
                        else [("SalesInvoice:1",)])

            def fetchone(self):
                return ("ontology.own_issued",)

        def login(*a, **k):
            tries.append(1)
            if len(tries) % 3:   # two refusals, then a slot
                raise full
            return Rows()
        with mock.patch("psycopg.connect", side_effect=login), \
                mock.patch.dict(os.environ, {pg_pool.FULL_WAIT_ENV: "5"}):
            ids, handles = document_marks._Marked._read("postgresql://made-up/marks")
            self.assertEqual(ids, {"Doc:1": frozenset({"finance"})})
            self.assertEqual(own_issued._Hidden._read(), frozenset({"SalesInvoice:1"}))
        self.assertEqual(len(tries), 6)


class Login(Conn):
    """A stand-in login a ``pool.Pool`` can lend, reset and keep."""
    open_now: list = []
    peak = 0
    lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self.autocommit = self.read_only = self.isolation_level = None
        self.broken = False
        with Login.lock:
            Login.open_now.append(self)
            Login.peak = max(Login.peak, sum(not c.closed for c in Login.open_now))

    def commit(self):
        pass

    def rollback(self):
        pass


@unittest.skipIf(pg_pool is None, "psycopg not installed")
class SessionBudgetTests(unittest.TestCase):
    """``PLX_PG_SESSIONS_MAX`` (set for the warm-up process, ``warm_process``): one process's
    Postgres sessions over all its pools at most that many at once; a borrow past it waits
    (the process's idle logins closed first) and is served, never refused at once. Live
    11-21 9:14 AM ET: a warm-up pass held the API's whole share of the role (100 of 100), so
    the deploy found no room for a second API and restarted cold."""

    def borrow_all(self, environ, threads=9, pools=3, barrier=None):
        Login.open_now, Login.peak = [], 0
        made = [pg_pool.Pool("postgresql://made-up/budget", max_size=4,
                             configure=lambda conn: None) for _ in range(pools)]
        for p in made:
            self.addCleanup(p.close)
        served, failed = [], []

        def borrow(n):
            try:
                with made[n % pools].connection() as conn:
                    conn.execute("SELECT 1")
                    barrier.wait(5) if barrier else time.sleep(0.05)
                    served.append(n)
            except Exception as exc:  # noqa: BLE001 - counted
                failed.append(exc)
        with mock.patch("psycopg.connect", side_effect=lambda *a, **k: Login()), \
                mock.patch.dict(os.environ, environ):
            workers = [threading.Thread(target=borrow, args=(n,)) for n in range(threads)]
            for t in workers:
                t.start()
            for t in workers:
                t.join(30)
        return served, failed

    def test_a_process_budget_bounds_its_sessions_and_serves_every_borrow(self):
        # unbounded (the API, as before): nine borrows at once over three pools, nine logins
        served, failed = self.borrow_all({}, barrier=threading.Barrier(9))
        self.assertEqual((len(served), failed, Login.peak), (9, [], 9))
        # bounded at 3: never more than three open at once, all nine served
        served, failed = self.borrow_all({pg_pool.SESSIONS_MAX_ENV: "3",
                                          pg_pool.FULL_WAIT_ENV: "20"})
        self.assertEqual((sorted(served), failed), (list(range(9)), []))
        self.assertLessEqual(Login.peak, 3)

    @unittest.skipUnless(hasattr(os, "fork"), "no fork")
    def test_a_forked_child_has_a_budget_of_its_own_even_with_the_lock_held(self):
        with mock.patch.dict(os.environ, {pg_pool.SESSIONS_MAX_ENV: "2"}):
            parent = pg_pool.session_budget()
            parent._held.append(lambda: Login())   # one open in the parent
            with pg_pool._BUDGET_LOCK:   # held by a parent thread across the fork
                pid = os.fork()
                if pid == 0:   # the child: a fresh, empty budget at once, never a hang
                    ok = pg_pool.session_budget() is not parent \
                        and pg_pool.session_budget().in_use() == 0
                    os._exit(0 if ok else 1)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)

    def test_a_budget_still_full_after_its_wait_is_a_refusal_at_the_limit(self):
        Login.open_now, Login.peak = [], 0
        pool = pg_pool.Pool("postgresql://made-up/full", max_size=4,
                            configure=lambda conn: None)
        self.addCleanup(pool.close)
        with mock.patch("psycopg.connect", side_effect=lambda *a, **k: Login()), \
                mock.patch.dict(os.environ, {pg_pool.SESSIONS_MAX_ENV: "1",
                                             pg_pool.FULL_WAIT_ENV: "0.3"}):
            with pool.connection():
                began = time.monotonic()
                with self.assertRaises(pg_pool.SessionsFull) as refused:
                    with pool.connection():
                        pass
            self.assertGreaterEqual(time.monotonic() - began, 0.25)   # waited first
            self.assertTrue(pg_pool.at_limit(refused.exception))   # callers see the limit
            with pool.connection() as conn:   # handed back: free again
                self.assertFalse(conn.closed)
        self.assertEqual(Login.peak, 1)


@unittest.skipIf(pg_pool is None, "psycopg not installed")
class ForkAndNameTests(unittest.TestCase):
    def test_a_forked_child_reaps_none_of_its_parents_connections(self):
        """A child's first pool starts its own reaper; it must reach only the child's pools
        (closing an inherited connection ends the parent's session on the shared socket)."""
        import subprocess
        import sys
        from pathlib import Path
        code = (
            "import os\n"
            "from operating.pg import pool\n"
            "from operating.search import core\n"
            "called = []\n"
            "class P:\n"
            "    def __init__(self, name): self.name = name\n"
            "    def reap(self, now): called.append(self.name)\n"
            "parent = P('parent'); pool.reap_idle(parent.reap)\n"
            "core._IDLE['db'] = [(object(), 0.0)]\n"
            "r, w = os.pipe()\n"
            "if os.fork() == 0:\n"
            "    child = P('child'); pool.reap_idle(child.reap)\n"
            "    pool.reap_now(10.0 ** 9)\n"
            "    os.write(w, (','.join(called) + ' ' + str(len(core._IDLE))).encode()); os._exit(0)\n"
            "os.wait()\n"
            "pool.reap_now(10.0 ** 9)\n"
            "print(os.read(r, 99).decode(), ','.join(called), len(core._IDLE))\n")
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run([sys.executable, "-c", code], cwd=root, text=True, timeout=60,
                             capture_output=True, env={**os.environ, "PYTHONPATH": str(root)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["child", "0", "parent", "1"])

    def test_connections_carry_the_units_name_else_their_pool_kind(self):
        seen = []
        with mock.patch("psycopg.connect", side_effect=lambda *a, **k: seen.append(
                k.get("application_name"))):
            for env in ({}, {"PGAPPNAME": "plx-api"}):
                with mock.patch.dict(os.environ, env):
                    os.environ.pop("PGAPPNAME", None) if not env else None
                    core.connect("postgresql://made-up/db")
                    pg_pool.connect("postgresql://made-up/db")
        self.assertEqual(seen, ["plx-search", "plx-workbench", "plx-api", "plx-api"])

    def test_store_checks_reuse_a_kept_connection_and_survive_the_role_limit(self):
        """``ontology_reader.whole`` / ``change_stamp`` (asked on every inbox and store
        open) log in once per process, not per check; once the role is at its limit
        (two APIs through a deploy) a check answers from the kept connection instead of
        ``unreachable`` (live 11-21: Home's approvals and replies 500 after 12 s); a lost
        kept connection is replaced; another process (a forked child) never borrows it."""
        from operating.pg import ontology_reader
        from operating.pg.ontology_load import ORDER

        class Row(Conn):
            def __init__(self):
                super().__init__()
                self.autocommit = False
                self.broken = False
                self.queries = 0

            def execute(self, sql, *args):
                self.queries += 1
                self.last = sql
                return self

            def fetchone(self):
                if "sq_data_version" in self.last:
                    return ("digest-1", 7)
                return (list(ORDER), {"content_digest": "digest-1"}, True)

            def commit(self):
                pass

            def rollback(self):
                pass

        logins = []
        full = psycopg.OperationalError('FATAL:  too many connections for role "plx_api"')

        def login(*a, **k):
            if logins and logins[-1] == "full":
                raise full
            conn = Row()
            logins.append(conn)
            return conn

        dsn = "postgresql://made-up/probe-test"
        ontology_reader._PROBES.clear()
        with mock.patch("psycopg.connect", side_effect=login), \
                mock.patch.dict(os.environ, {pg_pool.FULL_WAIT_ENV: "0"}):
            for _ in range(5):
                self.assertIsNone(ontology_reader.whole(dsn))
                self.assertEqual(ontology_reader.change_stamp(dsn), "digest-1@7")
            self.assertEqual(len(logins), 1)            # ten checks, one login
            self.assertGreaterEqual(logins[0].queries, 10)
            logins.append("full")                       # the role at its limit now
            self.assertIsNone(ontology_reader.whole(dsn))   # the kept one answers
            # the kept connection lost: a new login is needed and refused at the limit
            logins[0].closed = True
            self.assertEqual(ontology_reader.whole(dsn), "unreachable:OperationalError")
            logins.pop()                                # a slot free again
            self.assertIsNone(ontology_reader.whole(dsn))
            self.assertEqual(len(logins), 2)
            # another process (a fork has another pid) logs in for itself
            with mock.patch("os.getpid", return_value=os.getpid() + 1):
                self.assertIsNone(ontology_reader.whole(dsn))
            self.assertEqual(len(logins), 3)
        ontology_reader._PROBES.clear()

    def test_a_forked_child_gets_its_own_probe_lock_and_no_parent_pools(self):
        """Forked while another thread holds the store checks' lock, the child still checks
        (its own lock), and holds none of its parent's pools."""
        import subprocess
        import sys
        from pathlib import Path
        code = (
            "import os, threading, time\n"
            "from operating.pg import ontology_reader as o\n"
            "o._PROBES[(os.getpid(), 'x')] = object()\n"
            "held = threading.Event()\n"
            "def hold():\n"
            "    with o._PROBES_LOCK:\n"
            "        held.set(); time.sleep(5)\n"
            "threading.Thread(target=hold, daemon=True).start(); held.wait()\n"
            "r, w = os.pipe()\n"
            "if os.fork() == 0:\n"
            "    ok = o._PROBES_LOCK.acquire(timeout=1)\n"
            "    os.write(w, f'{ok} {len(o._PROBES)}'.encode()); os._exit(0)\n"
            "os.wait(); print(os.read(r, 99).decode(), len(o._PROBES))\n")
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run([sys.executable, "-c", code], cwd=root, text=True, timeout=60,
                             capture_output=True, env={**os.environ, "PYTHONPATH": str(root)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["True", "0", "1"])


if __name__ == "__main__":
    unittest.main()
