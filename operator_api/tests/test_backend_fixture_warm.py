"""The known readers read ahead at start and the warm-up yields to live reads, split from test_backend_fixture."""
from __future__ import annotations
import argparse
import contextlib
from concurrent.futures import Future
import io
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from datetime import date
from typing import Optional, Sequence
from urllib.parse import quote
from unittest import mock
from fastapi.testclient import TestClient
from operating import objective_runner, proactive_engine
from operating import proactive_engine_inbox
from operator_api import backend as be
from operator_api import backend_common
from operator_api import models as m
from operator_api.server import main as serve_main
from operator_api.workspace import case_console
from operator_api.workspace import object_workspace as objects
from operator_api.tests import case_fixtures
from operator_api.tests.test_objective_box import _on_record
from operating.pg import scratch_db


class KnownReaderWarmTests(unittest.TestCase):
    """The read-ahead at start follows only an unexpired, unrevoked real session, keeps no
    name, and its identity serves and audits nothing."""

    def test_only_live_sessions_are_followed_and_nothing_names_the_person(self):
        tmp = Path(tempfile.mkdtemp(prefix="app-known-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", data_dir=tmp)
        backend.warmer = be.Worker(lambda s: None)   # queued, never run here
        now = int(time.time())
        people = {"29e05178-0bd1-49b9-be6e-b01be1975f70": ("a" * 64, now + 3600),
                  "00000000-0000-4000-8000-000000000001": ("b" * 64, now + 3600),  # revoked
                  "00000000-0000-4000-8000-000000000002": ("c" * 64, now - 1)}     # expired
        for oid, (ref, exp) in people.items():
            session = SimpleNamespace(principals=frozenset({f"employee:{oid}"}),
                                      employee=SimpleNamespace(object_id=oid), email=None,
                                      session_ref=ref, expires_at=exp)
            backend.remember_reader(be.Reader(session, "dev", "Vince Example"))
        path = tmp / be.KNOWN_READERS_FILE
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("Vince", path.read_text())
        self.assertEqual(len(backend.known_readers()), 2)   # the expired one is gone
        backend.revoked = lambda ref: ref == "b" * 64
        made = []

        def identity_for(oid, exp):
            made.append((oid, exp))
            return SimpleNamespace(principals=frozenset({f"employee:{oid}"}), email=None)
        backend.identity_for = identity_for
        self.assertEqual(backend.warm_known(), 1)
        self.assertEqual(made, [("29e05178-0bd1-49b9-be6e-b01be1975f70", now + 3600)])
        # Today first on the read workers (Home, the briefing and its groups, the
        # suggestion cards, the late list, the upcoming meetings), then the pages and
        # last week's review on the warm worker
        self.assertEqual((backend.reads.jobs.qsize(), backend.warmer.jobs.qsize()), (5, 2))
        # signed out: never read ahead for again
        backend.forget_reader(be.Reader(SimpleNamespace(
            principals=frozenset(), employee=SimpleNamespace(
                object_id="29e05178-0bd1-49b9-be6e-b01be1975f70")), "dev", ""))
        self.assertEqual(len(backend.known_readers()), 1)

    def test_every_directory_employee_with_access_is_read_ahead_too(self):
        """Owner 11-13: a reader's first-ever Today is a kept page as well. Each active
        employee of the directory without a recent session is read ahead once (resolved as
        a sign-in is: one refused, for no access or the exclusion, reads nothing), with a
        short-lived identity; a person with a session is read once, not twice."""
        tmp = Path(tempfile.mkdtemp(prefix="app-directory-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", data_dir=tmp)
        backend.warmer = be.Worker(lambda s: None)   # queued, never run here
        now = int(time.time())
        seen = "29e05178-0bd1-49b9-be6e-b01be1975f70"
        fresh = "00000000-0000-4000-8000-00000000000a"
        refused = "00000000-0000-4000-8000-00000000000b"
        session = SimpleNamespace(principals=frozenset({f"employee:{seen}"}),
                                  employee=SimpleNamespace(object_id=seen), email=None,
                                  session_ref="a" * 64, expires_at=now + 3600)
        backend.remember_reader(be.Reader(session, "dev", ""))
        backend.revoked = lambda ref: False
        made = []

        def identity_for(oid, exp):
            if oid == refused:
                raise RuntimeError("employee_not_granted")
            made.append((oid, exp))
            return SimpleNamespace(principals=frozenset({f"employee:{oid}"}), email=None)
        backend.identity_for = identity_for
        backend.directory_for = lambda: [seen.upper(), refused, fresh]
        self.assertEqual(backend.warm_known(), 2)
        self.assertEqual([oid for oid, _ in made], [seen, fresh])
        self.assertLessEqual(made[1][1], now + backend_common.DIRECTORY_WARM_SECONDS + 5)
        # one who signed out is left out of the directory's read-ahead until they sign in
        backend._warmed.clear()
        made.clear()
        backend.forget_reader(be.Reader(SimpleNamespace(
            principals=frozenset(), employee=SimpleNamespace(object_id=fresh)), "dev", ""))
        backend.remember_reader(be.Reader(session, "dev", ""))   # another sign-in keeps it
        self.assertEqual(backend.warm_known(), 1)
        self.assertEqual([oid for oid, _ in made], [seen])
        self.assertEqual(len(backend.known_readers()), 1)
        # a directory that cannot be read: the sessions only, as before
        backend.directory_for = lambda: (_ for _ in ()).throw(OSError("no snapshot"))
        made.clear()
        backend._warmed.clear()
        self.assertEqual(backend.warm_known(), 1)
        self.assertEqual([oid for oid, _ in made], [seen])

    def test_who_used_the_app_this_week_is_read_ahead_first_and_the_rest_rotate(self):
        """The directory's window never leaves out someone who used the app in the last
        7 days (their first Home then opens a kept page and trail); one past 7 days or who
        signed out takes a turn in the window like anyone else."""
        tmp = Path(tempfile.mkdtemp(prefix="app-recent-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", data_dir=tmp)
        backend.warmer = be.Worker(lambda s: None)
        now = time.time()
        ids = [f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 91)]
        recent, old = ids[85], ids[86]
        (tmp / be.KNOWN_READERS_FILE).write_text(json.dumps({
            recent: {"seen": now - 3 * 86400, "ref": "a" * 64, "exp": int(now) - 100, "pages": []},
            old: {"seen": now - 9 * 86400, "ref": "b" * 64, "exp": int(now) - 100, "pages": []}}))
        self.assertEqual(backend.recent_users(), [recent])
        made = []
        backend.identity_for = lambda oid, exp: (made.append(oid), SimpleNamespace(
            principals=frozenset({f"employee:{oid}"}), email=None))[1]
        backend.directory_for = lambda: list(ids)
        backend._warm_directory(set())
        self.assertEqual(made[0], recent)
        self.assertEqual(len(made), backend_common.DIRECTORY_WARM_MOST)
        self.assertNotIn(old, made[:1])
        # kept past the session: a write does not drop an entry seen this week
        backend._write_known({})
        self.assertEqual(backend.recent_users(), [recent])

    def test_a_read_ahead_identity_serves_and_audits_nothing(self):
        from operating import session_identity as si
        from operating.employee_permissions import EmployeePrincipal
        from operator_api.workspace.session_journal import SessionJournal
        principal = EmployeePrincipal("11111111-1111-4111-8111-111111111111",
                                      "29e05178-0bd1-49b9-be6e-b01be1975f70")
        warm = si.SessionIdentity(
            tenant="plx", channel=si.CHANNEL_CONSOLE, author_kind=si.AUTHOR_HUMAN,
            proof=si.PROOF_WARM, employee=principal,
            principals=frozenset({principal.principal}), expires_at=int(time.time()) + 60,
            identities=frozenset({principal.object_id}))
        with self.assertRaises(si.SessionIdentityRefused):
            warm.describe()
        with self.assertRaises(si.SessionIdentityRefused):
            warm.intent_identity(warm.actor)
        tmp = Path(tempfile.mkdtemp(prefix="app-warm-journal-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaises(PermissionError):
            SessionJournal.for_state_dir(tmp).record(method="GET", path="/api/home",
                                                     status=200, session=warm)
        from fastapi.testclient import TestClient as Client
        from operator_api.app import create_app
        from operator_api.auth import DevAuth
        client = Client(create_app(be.WorkspaceBackend(lambda s: None, data_label="t"),
                                   DevAuth(lambda: warm), secure_cookie=False))
        client.post("/api/auth/dev-login", json={})
        self.assertEqual(client.get("/api/home").status_code, 401)


class WarmYieldsTests(unittest.TestCase):
    """The read-ahead never delays a person: it starts a page only after live reads went
    quiet, and a live request never waits on a page the read-ahead is reading."""

    def test_a_queued_live_request_delays_the_next_warm_page(self):
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        reader = SimpleNamespace(session=SimpleNamespace(principals=frozenset({"x"})))
        backend.reads.submit(reader, lambda view: None)   # queued, not yet run
        done = threading.Event()
        with mock.patch.object(be, "WARM_PAUSE_SECONDS", 0.0):
            waiter = threading.Thread(target=lambda: (backend.wait_idle(), done.set()))
            waiter.start()
            self.assertFalse(done.wait(1.5))              # the warm page waits
            backend.reads.jobs.get_nowait()               # the live request is served
            backend.reads.last_done = time.monotonic() - be.WARM_IDLE_SECONDS
            self.assertTrue(done.wait(2.0))
            waiter.join()

    def test_a_live_request_never_waits_on_read_ahead_work(self):
        cache = be.ReaderCache()
        inside, release, live_done = threading.Event(), threading.Event(), threading.Event()

        def warm():
            with cache.flight(("page", "PO-1")):
                inside.set()
                release.wait(5)
        warming = threading.Thread(target=warm, name=be.WARM_THREAD)
        warming.start()
        inside.wait(2)
        live = threading.Thread(target=lambda: (cache.flight(("page", "PO-1")).__enter__(),
                                                live_done.set()))
        live.start()
        self.assertTrue(live_done.wait(1.0))   # read itself, did not wait
        release.set()
        warming.join()
        live.join()

    def test_no_revocation_check_wired_means_no_read_ahead(self):
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        self.assertTrue(backend.revoked("a" * 64))   # fails closed


class WarmUpCheckWorkers(unittest.TestCase):
    def test_the_warm_up_decides_kept_pages_on_check_workers_of_its_own(self):
        """The warm-up process starts a check worker and a state worker as the API does:
        without them its kept pages' access checks fell back to its read workers, queued
        behind minutes-long screen reads past ACCESS_WAIT_SECONDS, and each kept page was
        refused as undecided and read again (live 11-20)."""
        from operating import parent_watch
        from operator_api import feedback, warm_process

        class Stop(Exception):
            pass
        started = []

        def start(backend, argv, number, access=False, checks=False, states=False):
            started.append((number, access, checks, states))
            if states:
                raise Stop()

        def main(argv, serve_with=None, fixtures=None, **_):
            return serve_with({"objects_for": None})
        with mock.patch.object(parent_watch, "die_with"), \
                mock.patch.object(parent_watch, "spawned_parent"), \
                mock.patch("logging.basicConfig"), \
                mock.patch.object(feedback, "attach_capture_directory"), \
                mock.patch.object(case_console, "main", side_effect=main), \
                mock.patch.object(be, "WorkspaceBackend", return_value=mock.MagicMock()), \
                mock.patch.object(warm_process, "read_workers", return_value=2), \
                mock.patch.object(be, "start_read_worker", side_effect=start), \
                self.assertRaises(Stop):
            warm_process.child(None, [], {}, "dev")
        self.assertEqual(started, [("warm-1", False, False, False),
                                   ("warm-check", True, True, False),
                                   ("warm-state", True, True, True)])


if __name__ == "__main__":
    unittest.main()
