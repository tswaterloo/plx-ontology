"""Capacity under several browsers (11-16 walk: 233 of ~1,560 calls refused busy).

One read per screen and reader at a time: a reader's requests for the same screen wait
for the read under way instead of each queuing its own on the shared read workers; another
reader (other principals) or another screen is never handed it. And the cheap routes never
wait behind the screen routes' threads blocked on the read workers."""
from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api import app as app_module
from operator_api import backend as be
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend
from operator_api.tests import test_last_good_page as lgp
from operator_api.tests.test_last_good_page import page, reader


class OneReadPerScreen(unittest.TestCase):
    """``_serving`` with no kept page (a first visit, or one the access check could not
    clear): concurrent requests of one reader for one screen run one read."""

    def setUp(self):
        lgp.LastGoodPage.setUp(self)
        # a store stamp, as served (with none nothing is current and nothing is joined)
        self.backend.inputs_stamp = lambda: "generation-1"

    def test_same_reader_and_screen_share_one_read(self):
        who = reader("grp-a")
        for n in (1, 10, 60):
            with self.subTest(requests=n):
                self.backend._good.clear()
                started, release, reads = threading.Event(), threading.Event(), []

                def read():
                    reads.append(1)
                    started.set()
                    release.wait(5)
                    return page(n)

                def call():
                    return self.backend._serving(who, ("late", 0, n), read)

                with ThreadPoolExecutor(max_workers=n) as pool:
                    futures = [pool.submit(call)]
                    self.assertTrue(started.wait(5))
                    futures += [pool.submit(call) for _ in range(n - 1)]
                    time.sleep(0.2)   # every other request is waiting now
                    release.set()
                    got = [f.result(timeout=10) for f in futures]
                self.assertEqual(len(reads), 1)            # one read, not n
                self.assertEqual(len(got), n)              # no request lost
                self.assertTrue(all(g.total == n for g in got))
                self.assertEqual(len({id(g) for g in got}), n)   # each its own copy
                self.assertEqual(self.backend.__dict__["_screen_reads"], {})

    def test_another_reader_or_screen_reads_its_own(self):
        a, b = reader("grp-a"), reader("grp-b")
        release, seen = threading.Event(), []

        def read_as(name):
            def read():
                seen.append(name)
                release.wait(5)
                return page(len(name))
            return read

        calls = [lambda: self.backend._serving(a, ("late", 0, 25), read_as("a")),
                 lambda: self.backend._serving(b, ("late", 0, 25), read_as("bb")),
                 lambda: self.backend._serving(a, ("home",), read_as("ccc"))]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(c) for c in calls]
            deadline = time.monotonic() + 5
            while len(seen) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            release.set()
            got = [f.result(timeout=10) for f in futures]
        self.assertEqual(sorted(seen), ["a", "bb", "ccc"])
        self.assertEqual([g.total for g in got], [1, 2, 3])   # never another's page

    def test_a_refused_read_refuses_every_waiter_and_the_next_request_reads_again(self):
        who = reader("grp-a")
        started, release, reads = threading.Event(), threading.Event(), []

        def busy_read():
            reads.append(1)
            started.set()
            release.wait(5)
            raise TimeoutError("workbench_busy")

        def call():
            try:
                return self.backend._serving(who, ("late", 0, 25), busy_read)
            except TimeoutError as error:
                return error

        joined = threading.Barrier(4, timeout=5)   # the leader and its 3 joiners
        screen_read = self.backend._screen_read

        def counted(*args, **kwargs):
            box, started_here = screen_read(*args, **kwargs)
            if not started_here:
                joined.wait()   # every joiner holds the leader's read
            return box, started_here

        self.backend._screen_read = counted
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(call)]
            self.assertTrue(started.wait(5))
            futures += [pool.submit(call) for _ in range(3)]
            joined.wait()   # no timing: released only once all three joined
            release.set()
            got = [f.result(timeout=10) for f in futures]
        self.assertTrue(all(isinstance(g, TimeoutError) for g in got))
        deadline = time.monotonic() + 10
        while self.backend._refreshing and time.monotonic() < deadline:
            time.sleep(0.01)   # the refusal's one background read (``_refused``) ends
        self.assertFalse(self.backend._refreshing)
        # the shared read and that one background read; not one per waiting request
        self.assertEqual(len(reads), 2)
        fresh = self.backend._serving(who, ("late", 0, 25), lambda: page(4))
        self.assertEqual(fresh.total, 4)    # nothing stuck: a new read answers

    def test_after_the_readers_own_write_the_next_request_reads_after_it(self):
        """Read your writes: a request after the reader approved (``reader_wrote``) is
        never handed a read of the screen begun before the approval."""
        for stamp in ("generation-1", None):
            with self.subTest(stamp=stamp):
                self.backend.inputs_stamp = lambda: stamp
                self.backend._good.clear()
                who = reader("grp-a")
                state = {"total": 1}   # 1: before the approval, 2: after
                started, release, reads = threading.Event(), threading.Event(), []

                def read():
                    seen = state["total"]
                    reads.append(seen)
                    started.set()
                    release.wait(5)
                    return page(seen)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.backend._serving, who, ("approvals",), read)
                    self.assertTrue(started.wait(5))
                    state["total"] = 2                 # the reader approves an item
                    self.backend.reader_wrote(who)      # as every non-GET request does
                    started.clear()
                    second = pool.submit(self.backend._serving, who, ("approvals",), read)
                    self.assertTrue(started.wait(5))    # its own read, not the first's
                    release.set()
                    self.assertEqual(first.result(10).total, 1)
                    self.assertEqual(second.result(10).total, 2)
                self.assertEqual(reads, [1, 2])

    def test_the_owner_viewing_as_a_reader_never_shares_the_readers_read(self):
        own = reader("grp-a")
        viewed = reader("grp-a")
        viewed.session = SimpleNamespace(view_as=True)
        release, seen = threading.Event(), []

        def read_as(name, total):
            def read():
                seen.append(name)
                release.wait(5)
                return page(total)
            return read

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.backend._serving, own, ("home",), read_as("own", 1))
            b = pool.submit(self.backend._serving, viewed, ("home",), read_as("view", 2))
            deadline = time.monotonic() + 5
            while len(seen) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            release.set()
            got = (a.result(10).total, b.result(10).total)
        self.assertEqual(sorted(seen), ["own", "view"])
        self.assertEqual(got, (1, 2))

    def test_a_joiner_whose_wait_runs_out_keeps_nothing(self):
        """A request that joined another's read and served the kept page meanwhile never
        keeps that read under its own (later) access state and inputs."""
        who, screen = reader("grp-a"), ("late", 0, 25)
        self.backend._serving(who, screen, lambda: page(7))   # a page kept
        key = (who.key, who.mode, screen)
        kept = self.backend._good.pop(key)                   # the leader sees none
        self.backend._snapshot = lambda *args: None          # the join path itself
        finishing, keeping = [], []
        finish, keep = self.backend._finish_later, self.backend._keep_good
        self.backend._finish_later = lambda *a, **k: finishing.append(a) or finish(*a, **k)
        self.backend._keep_good = lambda *a, **k: keeping.append(a) or keep(*a, **k)
        started, release = threading.Event(), threading.Event()

        def read():
            started.set()
            release.wait(5)
            return page(9)

        with mock.patch.object(be, "SERVE_FAST_SECONDS", 0.1), \
                ThreadPoolExecutor(max_workers=1) as pool:
            leader = pool.submit(self.backend._serving, who, screen, read)
            self.assertTrue(started.wait(5))
            with self.backend._lock:
                self.backend._good[key] = kept   # kept meanwhile: the joiner holds it
            joiner = self.backend._serving(who, screen, read)
            self.assertEqual(joiner.total, 7)     # the kept page, marked stale
            self.assertIsNotNone(joiner.stale_since)
            release.set()
            self.assertEqual(leader.result(10).total, 9)
        self.assertEqual(finishing, [])                       # the joiner keeps nothing
        self.assertEqual([a[1].total for a in keeping], [9])  # the leader's one keep


DANA = ("dana@example.com", "00000000-0000-0000-0000-00000000da00")


class Blocking(MockBackend):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release, self.inside, self._count = release, 0, threading.Lock()

    def late_list(self, reader, *, offset=0, limit=25):
        with self._count:
            self.inside += 1
        self.release.wait(20)
        return super().late_list(reader, offset=offset, limit=limit)


class CheapRoutesNeverQueue(unittest.TestCase):
    def test_more_screen_waits_than_anyio_default_and_a_cheap_route_still_answers(self):
        release = threading.Event()
        backend = Blocking(release)
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email=DANA[0], subject_oid=DANA[1])
        app = create_app(backend, DevAuth(lambda: session, mode="mock"), secure_cookie=False)
        waiting = 60   # past AnyIO's default 40 threads
        self.assertGreater(app_module.ROUTE_THREADS, waiting)
        with TestClient(app, base_url="http://testserver") as client:
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
            pool = ThreadPoolExecutor(max_workers=waiting)
            try:
                futures = [pool.submit(client.get, "/api/late") for _ in range(waiting)]
                deadline = time.monotonic() + 10
                while backend.inside < waiting and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(backend.inside, waiting)   # none queued for a thread
                began = time.monotonic()
                self.assertEqual(client.get("/api/auth/config").status_code, 200)
                self.assertLess(time.monotonic() - began, 5)
            finally:
                release.set()
                statuses = [f.result(timeout=30).status_code for f in futures]
                pool.shutdown()
        self.assertEqual(statuses, [200] * waiting)

if __name__ == "__main__":
    unittest.main()
