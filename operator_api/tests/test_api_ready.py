"""/api/ready, the probe a deploy waits on before it routes readers to a new API (ARCH-T6):
ready only once the warm-up process's first pass over the known readers is kept, the warm-up
started at once by a direct loopback probe (never by one through Caddy), and the body holds
the readiness and the code version only."""

from __future__ import annotations

from pathlib import Path
import re
import threading
import time
import types
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operator_api import backend as be
from operator_api import warm_process as wp
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend
from operator_api.page_store import code_version
from operator_api.serving import KNOWN_READERS
from tools.timing_policy import budget


class _Settles:
    def __init__(self, waited):
        self.waited = waited

    def __call__(self):
        return None

    def settle(self, seconds):
        self.waited.append(seconds)


class _Value:
    def __init__(self, typecode: str, value: int, lock: bool = False) -> None:
        self.value = value


class _Process:
    """The warm-up child: started, alive, and (``warm_after``) its first pass kept."""

    def __init__(self, target=None, args=(), name=None, daemon=None) -> None:
        self.args = args
        self.alive = False
        self.warm_after: float | None = 0.05

    def start(self) -> None:
        self.alive = True
        if self.warm_after is not None:
            warmed = self.args[-1]
            threading.Timer(self.warm_after, lambda: setattr(warmed, "value", 1)).start()

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout=None) -> None:
        pass

    def terminate(self) -> None:
        self.alive = False


class _Context:
    def Value(self, typecode, value, lock=False):  # noqa: N802 - multiprocessing's name
        return _Value(typecode, value)

    def Pipe(self):  # noqa: N802
        return mock.MagicMock(), mock.MagicMock()

    def Process(self, **kwargs):  # noqa: N802
        return _Process(**kwargs)


def warm_process() -> wp.WarmProcess:
    with mock.patch.object(wp.multiprocessing, "get_context", return_value=_Context()):
        return wp.WarmProcess(["serve"], {}, "dev", busy=lambda: False)


def client_for(process: wp.WarmProcess | None, host: str) -> TestClient:
    backend = MockBackend()
    backend.warm_process = process
    backend.ready = types.MethodType(be.WorkspaceBackend.ready, backend)
    backend.start_warm_now = types.MethodType(be.WorkspaceBackend.start_warm_now, backend)
    app = create_app(backend, DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                     secure_cookie=False)
    return TestClient(app, base_url="http://testserver", client=(host, 40000))


def until(check, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


class ReadyEndpoint(unittest.TestCase):
    def test_a_loopback_probe_starts_the_warm_up_and_is_ready_once_its_first_pass_is_kept(self):
        process = warm_process()
        self.addCleanup(process.stop)
        client = client_for(process, "127.0.0.1")
        first = client.get("/api/ready")
        # without the probe the child would wait up to START_WAIT_SECONDS for a reader
        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.json(), {"ready": False, "code_version": code_version()})
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertTrue(until(lambda: client.get("/api/ready").status_code == 200))
        self.assertEqual(client.get("/api/ready").json(),
                         {"ready": True, "code_version": code_version()})

    def test_a_probe_through_caddy_or_from_elsewhere_never_starts_the_warm_up(self):
        for host, headers in (("127.0.0.1", {"X-Forwarded-For": "100.64.0.9"}),
                              ("100.64.0.9", {})):
            process = warm_process()
            self.addCleanup(process.stop)
            client = client_for(process, host)
            for _ in range(5):
                got = client.get("/api/ready", headers=headers)
                self.assertEqual(got.status_code, 503)
                self.assertEqual(set(got.json()), {"ready", "code_version"})
                time.sleep(0.05)
            self.assertFalse(process.started.is_set(), host)

    def test_a_child_that_died_or_never_kept_its_pass_is_not_ready(self):
        process = warm_process()
        self.addCleanup(process.stop)
        process.process.warm_after = None            # up, its first pass never done
        client = client_for(process, "127.0.0.1")
        client.get("/api/ready")
        self.assertTrue(until(process.started.is_set))
        self.assertEqual(client.get("/api/ready").status_code, 503)
        process.warmed.value = 1
        self.assertEqual(client.get("/api/ready").status_code, 200)
        process.process.alive = False                # gone: pages would be read cold
        self.assertEqual(client.get("/api/ready").status_code, 503)

    def test_without_a_warm_up_process_serving_is_ready_and_needs_no_sign_in(self):
        got = client_for(None, "127.0.0.1").get("/api/ready")
        self.assertEqual((got.status_code, got.json()["ready"]), (200, True))

    def test_a_failing_readiness_check_is_not_ready(self):
        process = warm_process()
        self.addCleanup(process.stop)
        client = client_for(process, "100.64.0.9")
        with mock.patch.object(wp.WarmProcess, "ready", side_effect=RuntimeError("x")):
            self.assertEqual(client.get("/api/ready").status_code, 503)


class ScreensAgain(unittest.TestCase):
    def test_a_screen_still_reading_is_read_again_until_kept(self):
        """The Templates answer bank answers "still reading" while the company's documents
        are read, and such an answer is never kept: read ahead once, the first visit read
        it cold (8-14 s live). It is read again later until a real page comes back; the
        other screens are read once; a screen still reading at the limit is left."""
        backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(backend.stop)
        calls: list[str] = []
        done = threading.Event()

        def bank(reader, question=None):
            calls.append("bank")
            if len(calls) >= 3:
                done.set()
            return mock.Mock(reading=len(calls) < 3)

        def team(reader):
            calls.append("team")
            return mock.Mock(reading=False)
        backend.answer_bank = bank
        backend.team_view = team
        with mock.patch.object(be, "SCREENS_AGAIN_SECONDS", 0.05):
            backend._screens_ahead(mock.Mock(), ("team_view", ("answer_bank", None)))
            self.assertTrue(done.wait(10))
        self.assertEqual(calls, ["team", "bank", "bank", "bank"][:len(calls)])
        self.assertEqual(calls.count("team"), 1)
        calls.clear()
        with mock.patch.object(be, "SCREENS_AGAIN_SECONDS", 0.05):
            backend._screens_ahead(mock.Mock(), (("answer_bank", None),),
                                   until=be.time.monotonic())
        self.assertEqual(calls, ["bank"])   # past the limit: not read again


class WeeklyScreenReadWhole(unittest.TestCase):
    def test_the_weekly_screen_is_read_ahead_for_every_reader_every_round_with_no_cut_mark(self):
        import tempfile
        from pathlib import Path
        from operator_api import briefs
        backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(backend.stop)
        read = []
        backend.weekly_review = lambda reader, week, **k: read.append((reader.key, k))
        with tempfile.TemporaryDirectory() as tmp:
            backend.data_dir = Path(tmp)
            for key in ("r1", "r2", "r1"):
                self.assertEqual(backend._screens_ahead(
                    mock.Mock(key=key), (("weekly_review", None),)), 1)
            self.assertFalse(hasattr(briefs, "WeeklyCuts"))
            self.assertEqual(list(Path(tmp).iterdir()), [])   # no "cut" file is written
        self.assertEqual([k for k, _ in read], ["r1", "r2", "r1"])


class ScreensSideBySide(unittest.TestCase):
    """The warm-up process reads a reader's screens side by side on its workers, most
    visited first, and the first pass waits for every one of them."""

    def test_screens_run_on_every_worker_in_order_and_one_future_waits_for_all(self):
        backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(backend.stop)
        backend.screens_workers = 3
        lock, started, running, most = threading.Lock(), [], [0], [0]
        release = threading.Event()

        def screen(name):
            def read(reader, *a, **k):
                with lock:
                    started.append(name)
                    running[0] += 1
                    most[0] = max(most[0], running[0])
                release.wait(10)
                with lock:
                    running[0] -= 1
                return mock.Mock(reading=False)
            return read
        names = [(k if isinstance(k, str) else k[0]) for k in be.SCREENS_AHEAD]
        for name in names:
            setattr(backend, name, screen(name))
        with mock.patch("operator_api.page_sql.alerts_on", return_value=False):
            done = backend._screens_all(mock.Mock(key="r"))
            self.assertTrue(until(lambda: len(started) == 3))
            time.sleep(0.2)
            self.assertEqual(started, names[:3])        # the three most visited, at once
            self.assertFalse(done.done())
            release.set()
            self.assertEqual(done.result(10), len(names))
        self.assertEqual(sorted(started), sorted(names))   # every one, once
        self.assertEqual(started[0], "_home_served")
        self.assertEqual(most[0], 3)


class FirstPass(unittest.TestCase):
    """The warm-up child marks itself warmed only after every known reader's Today reads
    and main screens ran, on the real ``WorkspaceBackend.warm`` queue."""

    def backend(self, gate: threading.Event) -> be.WorkspaceBackend:
        view = mock.MagicMock()
        backend = be.WorkspaceBackend(lambda session: view, data_label="t")
        self.addCleanup(backend.stop)
        backend.warm_enabled = True
        backend.yield_to_live = False
        backend.warmer = backend.reads
        done = lambda *a, **k: None   # noqa: E731
        backend._attention_cached = done
        backend._suggestions_cached = done
        backend._late_held = done
        backend._upcoming_cached = done
        backend._briefing_ahead = lambda reader: done
        backend._weekly_ahead = lambda reader: done
        backend._screens_ahead = lambda reader, *args, **kw: gate.wait(10)
        readers = []
        for key in ("r1", "r2"):
            reader = mock.Mock(key=key)
            reader.session.view_as = False
            reader.session.principals = [key]
            readers.append(reader)
        backend.warm_known = lambda **kw: sum(backend.warm(r, force=True) for r in readers)
        return backend

    def test_warmed_waits_for_every_reader_s_today_and_screens(self):
        gate = threading.Event()
        backend = self.backend(gate)
        warmed, stop = _Value("b", 0), threading.Event()
        self.addCleanup(stop.set)
        result: list[int] = []
        threading.Thread(target=lambda: result.append(wp.first_pass(backend, stop, warmed)),
                         daemon=True).start()
        time.sleep(0.3)
        self.assertEqual(warmed.value, 0)             # queued, none read yet
        threading.Thread(target=backend.reads.loop, daemon=True).start()
        time.sleep(0.5)
        self.assertEqual(warmed.value, 0)             # Today read, the screens still running
        gate.set()
        self.assertTrue(until(lambda: warmed.value == 1))
        # 2 readers x (5 Today reads + the screens job) + the 2 screens futures it returned
        self.assertEqual(result, [14])
        self.assertIsNone(backend.warm_pending)       # later read-aheads are not collected

    def test_a_stopped_warm_up_never_marks_itself_warmed(self):
        backend = self.backend(threading.Event())
        warmed, stop = _Value("b", 0), threading.Event()
        stop.set()
        self.assertEqual(wp.first_pass(backend, stop, warmed), 0)
        self.assertEqual(warmed.value, 0)

    def test_failed_reads_count_as_done(self):
        gate = threading.Event()
        gate.set()
        backend = self.backend(gate)
        backend._attention_cached = mock.Mock(side_effect=RuntimeError("store gone"))
        warmed, stop = _Value("b", 0), threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=backend.reads.loop, daemon=True).start()
        self.assertEqual(wp.first_pass(backend, stop, warmed), 14)
        self.assertEqual(warmed.value, 1)


class FirstPassActiveReaders(unittest.TestCase):
    """11-22 (two deploys ~7 min each, one failed): the first pass read every known reader's
    and up to 60 directory employees' Today and screens before the API was ready. Now it
    reads only the readers active within ``READY_ACTIVE_SECONDS`` (an app load or an opened
    page, from the known readers' file), most recent first; everyone else is read right after
    (``rest``), once the API is ready."""

    def setUp(self) -> None:
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="ready-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def backend(self, known: dict) -> tuple[be.WorkspaceBackend, list[str]]:
        view = mock.MagicMock()
        backend = be.WorkspaceBackend(lambda session: view, data_label="t", data_dir=self.tmp)
        self.addCleanup(backend.stop)
        backend._write_known(known)
        backend.warm_enabled = True
        backend.yield_to_live = False
        backend.warmer = backend.reads
        read: list[str] = []
        done = lambda *a, **k: None   # noqa: E731
        backend._attention_cached = done
        backend._suggestions_cached = done
        backend._late_held = done
        backend._upcoming_cached = lambda view, reader: read.append(reader.key)
        backend._briefing_ahead = lambda reader: done
        backend._weekly_ahead = lambda reader: done
        backend._screens_ahead = lambda reader, *args, **kw: None
        backend.revoked = lambda ref: False

        def identity(oid, exp):
            session = mock.Mock(view_as=False, principals=[oid])
            session.employee.object_id = oid
            return session
        backend.identity_for = identity
        backend.directory_for = lambda: [DIRECTORY]
        threading.Thread(target=backend.reads.loop, daemon=True).start()
        return backend, read

    def test_only_the_readers_active_lately_hold_readiness_and_everyone_follows(self):
        now = time.time()
        entry = lambda seen, **kw: {"seen": seen, "ref": "a" * 64,   # noqa: E731
                                    "exp": int(now) + 3600, "pages": [], **kw}
        backend, read = self.backend({
            ACTIVE: entry(now - 300), LATEST: entry(now - 20),
            EARLIER: entry(now - 3 * 3600),                       # a session, not active
            ENDED: {"seen": now - 600, "ref": "b" * 64, "exp": int(now) - 60, "pages": []},
            GONE: {"seen": now - 30, "signed_out": True}})
        warmed, stop = _Value("b", 0), threading.Event()
        self.addCleanup(stop.set)
        began = time.monotonic()
        self.assertGreater(wp.first_pass(backend, stop, warmed, budget=5.0, active=1800), 0)
        self.assertLess(time.monotonic() - began, 4.0)
        self.assertEqual(warmed.value, 1)
        # the active ones, latest first (a session ended 10 min ago is still active); never
        # the one who signed out, the earlier session or the directory
        self.assertEqual(read, [LATEST, ACTIVE, ENDED])
        self.assertTrue(wp.rest(backend, stop))
        self.assertTrue(until(lambda: len(read) == 8))
        time.sleep(0.2)
        self.assertEqual(sorted(read[3:]), sorted([LATEST, ACTIVE, EARLIER, ENDED, DIRECTORY]))
        self.assertNotIn(GONE, read)

    def test_every_active_reader_is_read_once_in_the_pass_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                now = time.time()
                oids = [f"11111111-2222-4333-8444-{i:012d}" for i in range(100, 100 + n)]
                backend, read = self.backend({
                    oid: {"seen": now - 10 - i, "ref": "a" * 64, "exp": int(now) + 3600,
                          "pages": []} for i, oid in enumerate(oids)})
                backend.directory_for = lambda: []
                warmed, stop = _Value("b", 0), threading.Event()
                self.addCleanup(stop.set)
                began = time.monotonic()
                wp.first_pass(backend, stop, warmed, budget=10.0, active=1800)
                self.assertLess(time.monotonic() - began, budget(5))
                # each once, latest first, none lost (the file keeps the latest
                # ``KNOWN_READERS``: the others are the directory's, read after)
                kept = oids[:min(n, KNOWN_READERS)]
                self.assertEqual(read, kept)
                wp.rest(backend, stop)
                self.assertTrue(until(lambda: len(read) == 2 * len(kept)))
                time.sleep(0.1)
                self.assertEqual(sorted(read[len(kept):]), sorted(kept))

    def test_nobody_active_is_ready_at_once_and_the_rest_reads_everyone(self):
        now = time.time()
        backend, read = self.backend({
            EARLIER: {"seen": now - 60 * 3600, "ref": "a" * 64, "exp": int(now) - 1,
                      "pages": []}})   # 11-22 live: one reader, last seen 60 h before
        warmed, stop = _Value("b", 0), threading.Event()
        self.addCleanup(stop.set)
        began = time.monotonic()
        self.assertEqual(wp.first_pass(backend, stop, warmed, budget=5.0, active=1800), 0)
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(warmed.value, 1)
        self.assertEqual(read, [])
        wp.rest(backend, stop)
        self.assertTrue(until(lambda: len(read) == 2))
        self.assertEqual(sorted(read), sorted([EARLIER, DIRECTORY]))

    def test_the_window_comes_from_the_environment_and_a_bad_value_is_the_default(self):
        for raw, want in (("", wp.READY_ACTIVE_SECONDS), ("600", 600.0), ("x", 1800.0),
                          ("-5", 0.0), ("nan", 1800.0)):
            with mock.patch.dict("os.environ", {"PLX_WARM_READY_ACTIVE_S": raw}):
                self.assertEqual(wp.ready_active_seconds(), want, raw)


class WarmProcessMemory(unittest.TestCase):
    """11-22: the warm-up process held 1.3 GB resident and 6.2 GB swapped 30 min after start
    (the API itself 2.2 GB), and the standby's unit peaked at 12.3 GB. Its read workers keep a
    few readers' workspaces (``WARM_WORKSPACES``), and past ``WARM_MEMORY_MB`` with its reads
    done it ends and the API starts a fresh one: what it read is on disk."""

    def test_the_child_ends_only_past_the_limit_with_its_pass_kept_and_nothing_reading(self):
        from operator_api.serving import Worker
        backend = types.SimpleNamespace(reads=Worker(lambda s: None))
        for name in ("access_worker", "check_worker", "state_worker"):
            setattr(backend, name, None)
        warmed, stop = _Value("b", 0), threading.Event()
        self.addCleanup(stop.set)
        used = {"mb": 5000}
        ended: list[bool] = []
        run = threading.Thread(target=lambda: ended.append(wp.recycle_when_large(
            backend, stop, warmed, limit_mb=3072, every=0.02, memory=lambda: used["mb"])),
            daemon=True)
        backend.reads.running = 1          # a read under way
        run.start()
        time.sleep(0.2)
        self.assertEqual(ended, [])        # not warmed yet
        warmed.value = 1
        time.sleep(0.2)
        self.assertEqual(ended, [])        # still reading
        backend.reads.running = 0
        used["mb"] = 2000
        time.sleep(0.2)
        self.assertEqual(ended, [])        # under the limit
        with self.assertLogs("operator_api.warm", level="WARNING") as logs:
            used["mb"] = 3100
            self.assertTrue(until(lambda: ended == [True]))
        self.assertIn("at 3100 MB (limit 3072 MB)", logs.output[0])
        stop.set()
        self.assertFalse(wp.recycle_when_large(backend, stop, warmed, limit_mb=1, every=0.01,
                                               memory=lambda: 9999))
        backend.reads.jobs.put((0, 0, None))   # a queued job: not idle
        self.assertFalse(wp.idle(backend))

    def test_a_child_that_ended_for_its_memory_is_replaced_and_the_api_stays_ready(self):
        made: list["_Child"] = []

        class _Child(_Process):
            def __init__(self, **kw) -> None:
                super().__init__(**kw)
                self.exitcode = None
                self.done = threading.Event()
                made.append(self)

            def join(self, timeout=None) -> None:
                self.done.wait(timeout)

            def end(self, code: int) -> None:
                self.exitcode, self.alive = code, False
                self.done.set()

        context = _Context()
        context.Process = lambda **kw: _Child(**kw)
        with mock.patch.object(wp.multiprocessing, "get_context", return_value=context):
            process = wp.WarmProcess(["serve"], {}, "dev", busy=lambda: False)
        floor = mock.patch.object(wp, "MIN_CHILD_SECONDS", 0.0)
        floor.start()
        self.addCleanup(floor.stop)
        process.start_now()
        self.assertTrue(until(process.ready))
        made[0].end(wp.RECYCLE_EXIT)
        self.assertTrue(until(lambda: len(made) == 2 and made[1].alive))
        self.assertTrue(process.ready())             # the pass is kept: still ready
        self.assertEqual(process.respawns, 1)
        self.assertIs(made[1].args[-1], made[0].args[-1])   # one ``warmed`` for both
        made[1].end(1)                               # a crash: not replaced
        time.sleep(0.3)
        self.assertEqual(len(made), 2)
        self.assertFalse(process.ready())
        self.assertTrue(until(process.ended.is_set))

    def test_a_child_that_ends_for_its_memory_right_after_its_start_is_not_replaced(self):
        made: list = []

        class _Child(_Process):
            def __init__(self, **kw) -> None:
                super().__init__(**kw)
                self.exitcode = wp.RECYCLE_EXIT if made else None
                made.append(self)

            def join(self, timeout=None) -> None:   # ends at once, for its memory
                self.exitcode, self.alive = wp.RECYCLE_EXIT, False

        context = _Context()
        context.Process = lambda **kw: _Child(**kw)
        with mock.patch.object(wp.multiprocessing, "get_context", return_value=context):
            process = wp.WarmProcess(["serve"], {}, "dev", busy=lambda: False)
        with self.assertLogs("operator_api.warm", level="ERROR") as logs:
            process.start_now()
            self.assertTrue(until(process.ended.is_set))
        self.assertEqual(len(made), 1)               # never a loop of fresh children
        self.assertIn("not started again", logs.output[0])

    def test_own_memory_is_resident_plus_swapped(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix="status", delete=False) as handle:
            handle.write("Name:\tpython3\nVmRSS:\t 1363148 kB\nVmSwap:\t 6347776 kB\n")
        self.addCleanup(__import__("os").unlink, handle.name)
        self.assertEqual(wp.own_memory_mb(handle.name), (1363148 + 6347776) // 1024)
        self.assertIsNone(wp.own_memory_mb(handle.name + ".gone"))
        for raw, want in (("", wp.WARM_MEMORY_MB), ("4096", 4096), ("1", 512), ("x", 3072)):
            with mock.patch.dict("os.environ", {"PLX_WARM_MEMORY_MB": raw}):
                self.assertEqual(wp.memory_limit_mb(), want, raw)


ACTIVE = "11111111-2222-4333-8444-000000000001"
LATEST = "11111111-2222-4333-8444-000000000002"
EARLIER = "11111111-2222-4333-8444-000000000003"
ENDED = "11111111-2222-4333-8444-000000000004"
GONE = "11111111-2222-4333-8444-000000000005"
DIRECTORY = "11111111-2222-4333-8444-000000000006"


class FirstPassBounded(unittest.TestCase):
    """11-15 (both deploys rolled back): the standby's main screens were still running when
    the deploy's 600 s ran out, so /api/ready stayed 503. A read that never ends, or known
    readers that never resolve, cut the first pass at its budget: the API says it is ready,
    names what was still running, and the deploy's hold opens on it."""

    backend = FirstPass.backend

    def test_a_stuck_read_is_cut_at_the_budget_and_the_standby_becomes_ready(self):
        gate = threading.Event()   # the screens never end in this test
        self.addCleanup(gate.set)
        backend = self.backend(gate)
        warmed, stop = _Value("b", 0), threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=backend.reads.loop, daemon=True).start()
        holder = types.SimpleNamespace(ready=lambda: warmed.value == 1)
        result: list[int] = []
        began = time.monotonic()
        with self.assertLogs("operator_api.warm", level="INFO") as logs:
            threading.Thread(target=lambda: result.append(
                wp.first_pass(backend, stop, warmed, budget=1.0)), daemon=True).start()
            from operator_api.server import hold_until_ready
            self.assertTrue(hold_until_ready(holder, poll=0.02, limit=8))
        self.assertLess(time.monotonic() - began, 6)
        self.assertEqual(warmed.value, 1)
        self.assertTrue(until(lambda: result == [12]))   # 10 Today reads + 2 screens jobs
        cut = [m for m in logs.output if "cut at" in m]
        self.assertEqual(len(cut), 1)
        self.assertIn("2 reads still running (screens ahead x2)", cut[0])
        self.assertFalse(gate.is_set())   # the screens go on in the background

    def test_known_readers_that_never_resolve_are_cut_too(self):
        backend = self.backend(threading.Event())
        hung = threading.Event()
        self.addCleanup(hung.set)
        backend.warm_known = lambda **kw: hung.wait(30)
        warmed, stop = _Value("b", 0), threading.Event()
        self.addCleanup(stop.set)
        with self.assertLogs("operator_api.warm", level="WARNING") as logs:
            self.assertEqual(wp.first_pass(backend, stop, warmed, budget=0.5), 0)
        self.assertEqual(warmed.value, 1)
        self.assertIn("known readers still being resolved", logs.output[0])
        self.assertIsNone(backend.warm_pending)   # later read-aheads are not collected

    def test_a_stop_while_cut_short_never_marks_warmed(self):
        backend = self.backend(threading.Event())
        hung = threading.Event()
        self.addCleanup(hung.set)
        backend.warm_known = lambda **kw: hung.wait(30)
        warmed, stop = _Value("b", 0), threading.Event()
        threading.Timer(0.2, stop.set).start()
        self.assertEqual(wp.first_pass(backend, stop, warmed, budget=5), 0)
        self.assertEqual(warmed.value, 0)

    def test_the_budget_fits_inside_the_deploy_s_hold(self):
        from operator_api.server import HOLD_MAX_SECONDS
        # the API's own start-up (runtime, section pool, policies) comes before the pass
        self.assertLessEqual(wp.FIRST_PASS_SECONDS + 120, HOLD_MAX_SECONDS)


def _writer(directory: str, name: str, count: int, start) -> None:
    """One API process's writers (spawned): ``count`` work-log events (each also under a
    ``once`` key both processes write) and ``count`` feedback entries, as fast as it can."""
    from pathlib import Path
    from integrations.people_exclusion import PeopleExclusion
    from operator_api import feedback as fb
    from operator_api import work_log as wl
    guard = PeopleExclusion(["11111111-2222-4333-8444-555555555555"])
    log = wl.WorkLog(Path(directory) / "work-log", guard=lambda: guard)
    notes = fb.FeedbackStore(Path(directory) / "feedback")
    reader = type("R", (), {"key": f"plx:staff:{name}", "display_name": name, "email": None,
                            "session": type("S", (), {"identities": frozenset(),
                                                      "subject_oid": None})()})()
    start.wait(30)
    for n in range(count):
        log.record(reader, kind="ask", verb="asked", title=f"{name} {n}", thread=f"ask:{name}",
                   source="ask")
        log.record(reader, kind="ask", verb="asked", title=f"shared {n}", thread="ask:shared",
                   source="ask", once=f"shared-{n}")
        notes.add({"kind": "general", "reader": reader.key, "note": f"{name} {n}"})


class TwoApisShareTheStateDirectory(unittest.TestCase):
    """During a warm switch two API processes run on one state directory for a while: the
    work log and the feedback (SQLite, WAL, a 30 s busy wait) take both writers' rows,
    none lost or doubled, and a ``once`` key both write is kept once."""

    def test_concurrent_writers_lose_and_double_nothing(self):
        import multiprocessing
        import sqlite3
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp(prefix="two-apis-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        count = 150
        procs = [context.Process(target=_writer, args=(tmp, name, count, start))
                 for name in ("old", "new")]
        for proc in procs:
            proc.start()
        start.set()
        for proc in procs:
            proc.join(120)
            self.assertEqual(proc.exitcode, 0)
        events = sqlite3.connect(Path(tmp, "work-log", "work-log.sqlite"))
        titles = [r[0] for r in events.execute("SELECT title FROM event")]
        for name in ("old", "new"):
            self.assertEqual(sorted(t for t in titles if t.startswith(name + " ")),
                             sorted(f"{name} {n}" for n in range(count)))
        shared = [t for t in titles if t.startswith("shared ")]
        self.assertEqual(sorted(shared), sorted(f"shared {n}" for n in range(count)))
        notes = sqlite3.connect(Path(tmp, "feedback", "feedback.sqlite"))
        self.assertEqual(notes.execute("SELECT count(*), count(DISTINCT id) FROM feedback")
                         .fetchone(), (2 * count, 2 * count))


class HoldUntilWarm(unittest.TestCase):
    """An API a deploy starts held (its hold file) opens its port or socket only once ready:
    the warm-up is started at once and the port waits for it; a hold file a deploy left
    behind, or a warm-up that never finishes, never keeps the port shut for good."""

    def test_only_the_named_api_is_held_and_never_by_a_stale_file(self):
        import os
        import tempfile
        from pathlib import Path
        from operator_api import server
        self.assertEqual(server.hold_name(8780, None), "8780")
        self.assertEqual(server.hold_name(8780, "/run/plx-api-standby/api.sock"),
                         "plx-api-standby")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLX_API_HOLD_DIR": tmp}):
            self.assertFalse(server.held("plx-api-standby"))
            hold = Path(tmp, "hold-plx-api-standby")
            hold.write_text("")
            self.assertTrue(server.held("plx-api-standby"))
            self.assertFalse(server.held("8780"))
            old = time.time() - server.HOLD_MAX_AGE_SECONDS - 1
            os.utime(hold, (old, old))                 # an interrupted deploy's leftover
            self.assertFalse(server.held("plx-api-standby"))
        self.assertEqual(server.HOLD_DIR, "/run/plx-api")

    def test_the_excluded_names_are_read_at_the_start_not_by_the_first_reader(self):
        from operator_api import server
        called = []
        backend = types.SimpleNamespace(
            memory=types.SimpleNamespace(names=lambda: called.append("names")), ready=lambda: True)
        self.assertTrue(server.hold_until_ready(backend, poll=0.01))
        self.assertEqual(called, ["names"])
        # a names object that can be waited on is waited for (the first answer is final)
        waited = []
        names = types.SimpleNamespace(settle=lambda seconds: waited.append(seconds))
        names.__call__ = lambda: None
        settling = types.SimpleNamespace(memory=types.SimpleNamespace(names=_Settles(waited)), ready=lambda: True)
        self.assertTrue(server.hold_until_ready(settling, poll=0.01))
        self.assertEqual(waited, [server.NAMES_WAIT_SECONDS])
        # a names read that raises never keeps the port shut
        def boom():
            raise RuntimeError("graph down")
        broken = types.SimpleNamespace(memory=types.SimpleNamespace(names=boom), ready=lambda: True)
        self.assertTrue(server.hold_until_ready(broken, poll=0.01))

    def test_the_port_waits_for_ready_and_opens_at_the_limit_or_a_stop(self):
        from operator_api import server
        calls = []

        class Backend:
            def start_warm_now(self):
                calls.append("start")

            def ready(self):
                calls.append("ready?")
                return calls.count("ready?") >= 3
        self.assertTrue(server.hold_until_ready(Backend(), poll=0.01))
        self.assertEqual(calls, ["start", "ready?", "ready?", "ready?"])
        never = types.SimpleNamespace(start_warm_now=lambda: None, ready=lambda: False)
        started = time.monotonic()
        self.assertFalse(server.hold_until_ready(never, poll=0.01, limit=0.2))
        self.assertLess(time.monotonic() - started, budget(5))
        # the deploy waits past the hold (``plx-release`` READY_S)
        tool = (Path(server.__file__).resolve().parents[1] / "deploy" / "release"
                / "plx-release").read_text()
        ready_s = int(re.search(r"READY_S=\$\{PLX_API_READY_S:-(\d+)\}", tool).group(1))
        self.assertGreater(ready_s, server.HOLD_MAX_SECONDS + 30)
        stop = threading.Event()
        stop.set()
        self.assertFalse(server.hold_until_ready(never, stop=stop, poll=0.01))

    def test_the_standby_socket_never_serves_the_mock_or_a_fixed_identity(self):
        import contextlib
        import io
        from operator_api import server
        for extra in (["--mock"],
                      ["--dev", "--dev-employee-oid", "11111111-2222-4333-8444-555555555555"],
                      ["--dev", "--dev-fixture-principal", "staff:demo"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit) as refused:
                server.main([*extra, "--unix-socket", "/tmp/never-served.sock"])
            self.assertEqual(refused.exception.code, 2)
            self.assertIn("--unix-socket serves readers", err.getvalue())


class KnownReadersAcrossProcesses(unittest.TestCase):
    def test_a_sign_out_waits_for_the_other_api_s_write_of_the_file(self):
        """Two APIs share the known readers' file during a deploy: each read-modify-write
        holds the file's lock, so one never writes back what the other just removed."""
        import fcntl
        import os
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp(prefix="known-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        backend = be.WorkspaceBackend(lambda s: None, data_label="t", data_dir=Path(tmp))
        self.addCleanup(backend.stop)
        oid = "11111111-2222-4333-8444-555555555555"
        backend._write_known({oid: {"seen": time.time(), "ref": "a" * 64,
                                    "exp": int(time.time()) + 3600, "pages": []}})
        reader = mock.Mock(key="r1")
        reader.session.employee.object_id = oid
        path = backend._known_path()
        other = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(other, fcntl.LOCK_EX)               # the other API, mid-write
        done = threading.Thread(target=backend.forget_reader, args=(reader,))
        done.start()
        time.sleep(0.3)
        self.assertTrue(done.is_alive())
        self.assertIn(oid, backend.known_readers())
        os.close(other)
        done.join(5)
        self.assertFalse(done.is_alive())
        self.assertEqual(backend.known_readers(), {})



class WeeklyReviewAfterFirstPass(unittest.TestCase):
    def test_the_weekly_review_is_read_ahead_only_outside_the_first_pass(self):
        backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(backend.stop)
        backend.warm_enabled = True
        backend.yield_to_live = True
        backend.warmer = backend.reads
        done = lambda *a, **k: None   # noqa: E731
        for name in ("_attention_cached", "_suggestions_cached", "_late_held",
                     "_upcoming_cached"):
            setattr(backend, name, done)
        backend._briefing_ahead = lambda reader: done
        asked = []
        backend._weekly_ahead = lambda reader: asked.append(reader.key) or done
        reader = mock.Mock(key="r1")
        reader.session.view_as = False
        reader.session.principals = ["r1"]
        backend.warm_pending = []
        backend.warm(reader, force=True)
        self.assertEqual(asked, [])
        backend.warm_pending = None
        backend.warm(reader, force=True)
        self.assertEqual(asked, ["r1"])

if __name__ == "__main__":
    unittest.main()
