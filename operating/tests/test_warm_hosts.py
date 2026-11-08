"""Warm hosts (``agent_session.HostSpares.start_warm``): host processes started before any
reader is known, each bound to exactly one session's ticket and never reused, so no reader's
identity or state ever reaches another reader's session. Evidence label: isolated-fixture
(stub host processes; no store, no network)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from operating import agent_session as runner
from operating import host_peers, mcp_server

REPO = Path(__file__).resolve().parents[2]
TICKET_ENV = runner.ReaderHost.ticket_env

# The host's start as a warm host runs it: what it was started with, then the ticket it is
# bound to (``host_peers.await_ticket``), on stdout.
STUB = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "from operating import host_peers\n"
    "started_with = os.environ.get(%r)\n"
    "try:\n"
    "    got = host_peers.await_ticket(Path(os.environ[host_peers.TICKET_FILE_ENV]), seconds=20)\n"
    "except host_peers.TicketRefused as e:\n"
    "    got = 'refused:' + str(e)\n"
    "print(json.dumps({'started_with': started_with, 'ticket': got,\n"
    "                  'file_left': Path(os.environ[host_peers.TICKET_FILE_ENV]).exists()}),\n"
    "      flush=True)\n" % TICKET_ENV)


def reader(uid: str) -> runner.ReaderHost:
    return runner.ReaderHost(Path("/nonexistent-state"), Path("/nonexistent-config"),
                             {"uid": uid, "email": f"{uid}@example.test",
                              "entraTenantId": "t", "entraObjectId": f"oid-{uid}"},
                             "s" * 64, tenant="plx")


class WarmHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spares = runner.HostSpares(most=6)
        self.addCleanup(self.spares.close)
        self.command = [sys.executable, "-c", STUB]
        # a ticket given to the pool is never passed to a warm host
        self.env = {"PYTHONPATH": str(REPO), TICKET_ENV: "ticket-of-whoever-started-it"}

    def output(self, spare: runner.Spare) -> dict:
        line = spare.child.stdout.readline()
        spare.child.wait(10)
        return json.loads(line)

    def test_each_warm_host_serves_only_the_one_reader_bound_to_it(self):
        self.spares.start_warm("k", 2, self.command, self.env, str(REPO), TICKET_ENV)
        first, second = self.spares.take_warm("k"), self.spares.take_warm("k")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNot(first.child, second.child)
        self.assertIsNone(self.spares.take_warm("k"))   # each is handed out once
        self.assertIsNone(self.spares.take_warm("other-key"))
        first.bind("ticket-reader-A")
        second.bind("ticket-reader-B")
        a, b = self.output(first), self.output(second)
        self.assertEqual(a, {"started_with": None, "ticket": "ticket-reader-A",
                             "file_left": False})
        self.assertEqual(b, {"started_with": None, "ticket": "ticket-reader-B",
                             "file_left": False})
        for spare in (first, second):
            spare.release()
        # a bound host never returns to the pool; a refill starts new, unbound ones
        self.spares.start_warm("k", 1, self.command, self.env, str(REPO), TICKET_ENV)
        fresh = self.spares.take_warm("k")
        self.assertNotIn(fresh.child.pid, (first.child.pid, second.child.pid))
        fresh.bind("ticket-reader-C")
        self.assertEqual(self.output(fresh)["ticket"], "ticket-reader-C")
        fresh.release()

    def test_a_warm_host_with_no_usable_ticket_serves_no_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticket"
            ticks = iter([0.0, 0.0, 99.0])
            with self.assertRaises(host_peers.TicketRefused):
                host_peers.await_ticket(path, seconds=1, clock=lambda: next(ticks),
                                        sleep=lambda _s: None)
            path.write_text("t")
            os.chmod(path, 0o644)   # others may read it: refused, and removed
            with self.assertRaises(host_peers.TicketRefused):
                host_peers.await_ticket(path, seconds=1)
            self.assertFalse(path.exists())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)            # empty
            with self.assertRaises(host_peers.TicketRefused):
                host_peers.await_ticket(path, seconds=1)

    def test_the_mcp_start_up_takes_its_reader_only_from_the_bound_ticket(self):
        parser = argparse.ArgumentParser()
        args = argparse.Namespace(session_ticket_env=TICKET_ENV)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ticket"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(fd, b"bound-ticket")
            os.close(fd)
            env = {host_peers.TICKET_FILE_ENV: str(path)}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop(TICKET_ENV, None)
                mcp_server._bind_warm_ticket(parser, args)
                self.assertEqual(os.environ[TICKET_ENV], "bound-ticket")
            # a host started with its ticket ignores the file
            with mock.patch.dict(os.environ, {**env, TICKET_ENV: "own"}, clear=False):
                mcp_server._bind_warm_ticket(parser, args)
                self.assertEqual(os.environ[TICKET_ENV], "own")
            # no ticket in time: the start-up is refused, never served as anyone
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(host_peers, "TICKET_WAIT_SECONDS", 0.05), \
                    mock.patch("sys.stderr"):
                os.environ.pop(TICKET_ENV, None)
                with self.assertRaises(SystemExit):
                    mcp_server._bind_warm_ticket(parser, args)
                self.assertNotIn(TICKET_ENV, os.environ)

    def test_the_warm_key_names_no_reader_while_the_spare_key_does(self):
        a, b = reader("a"), reader("b")
        env = {"PYTHONPATH": str(REPO)}
        self.assertEqual(runner.warm_key(a, ["x"], None, {**env, TICKET_ENV: "1"}),
                         runner.warm_key(b, ["x"], None, {**env, TICKET_ENV: "2"}))
        self.assertNotEqual(runner.spare_key(a, ["x"], None, env),
                            runner.spare_key(b, ["x"], None, env))
        self.assertNotIn("oid-a", runner.warm_key(a, ["x"], None, env))

    def test_the_warm_count_is_bounded_and_fails_to_the_default(self):
        self.assertEqual(runner.warm_hosts({}), runner.WARM_HOSTS)
        self.assertEqual(runner.warm_hosts({runner.WARM_HOSTS_ENV: "2"}), 2)
        self.assertEqual(runner.warm_hosts({runner.WARM_HOSTS_ENV: "0"}), 0)
        self.assertEqual(runner.warm_hosts({runner.WARM_HOSTS_ENV: "99"}), 4)
        self.assertEqual(runner.warm_hosts({runner.WARM_HOSTS_ENV: "many"}),
                         runner.WARM_HOSTS)


class AskWorkerCountTests(unittest.TestCase):
    def test_ask_workers_go_to_four_only_where_memory_holds_them(self):
        from operator_api.backend import ask_worker_count
        self.assertEqual(ask_worker_count(4, 40.0), 4)
        self.assertEqual(ask_worker_count(4, 24.0), 2)    # 12 GB above the floor
        self.assertEqual(ask_worker_count(4, 10.0), 1)    # never none when asked for
        self.assertEqual(ask_worker_count(9, 200.0), 4)   # never past the most
        self.assertEqual(ask_worker_count(4, None), 4)    # unknown memory: as asked
        self.assertEqual(ask_worker_count(0, 40.0), 0)

    def test_workers_memory_refused_at_start_up_start_once_it_holds_them(self):
        import threading
        import time
        from types import SimpleNamespace
        from operator_api import backend as be

        free = [10.0]   # GB available, as the box reports it from moment to moment
        started: list[int] = []
        api = SimpleNamespace(ask_stopped=threading.Event(), ask_workers=["w0"])

        def start(n: int) -> None:
            started.append(n)
            free[0] -= be.ASK_WORKER_GB

        def wait_for(count: int) -> None:
            ends = time.monotonic() + 5
            while len(started) < count and time.monotonic() < ends:
                time.sleep(0.01)

        thread = be.top_up_ask_workers(api, start, 4, 1, available=lambda: free[0],
                                       every=0.01)
        time.sleep(0.1)
        self.assertEqual(started, [])      # under the floor: none more
        free[0] = 22.0                     # memory came back
        wait_for(1)
        time.sleep(0.1)
        # one more fits above the floor; the next waits until the first has its runtime
        self.assertEqual(started, [1])
        api.ask_workers.append("w1")
        free[0] = 40.0
        wait_for(2)
        time.sleep(0.1)
        self.assertEqual(started, [1, 2])
        api.ask_workers.append("w2")
        wait_for(3)
        thread.join(2)
        self.assertEqual(started, [1, 2, 3])          # never past the request
        self.assertFalse(thread.is_alive())
        # nothing to top up, unknown memory, or a stopped API: none started
        self.assertIsNone(be.top_up_ask_workers(api, start, 2, 2))
        self.assertIsNone(be.top_up_ask_workers(api, start, 9, be.ASK_WORKERS_MOST))
        stopped = SimpleNamespace(ask_stopped=threading.Event(), ask_workers=["w0"])
        late: list[int] = []
        unknown = be.top_up_ask_workers(stopped, late.append, 4, 1, available=lambda: None,
                                        every=0.01)
        time.sleep(0.1)
        stopped.ask_stopped.set()
        unknown.join(2)
        self.assertEqual(late, [])
        self.assertFalse(unknown.is_alive())


if __name__ == "__main__":
    unittest.main()
