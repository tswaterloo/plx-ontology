"""The proactive engine's worker pool, detector reserve and live check (split from
test_proactive_engine for its size cap)."""
from __future__ import annotations
from operating.copy_gate import say_date
import contextlib
from contextlib import closing
from decimal import Decimal
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from verify import live_verify as lv
from integrations.http import SourceHttpError
from ontology.core import Actor
from ontology import store as ostore
from ontology.store import StoreBuilder
from operating import po_status
from operating import alert_kinds as ak
from operating import proactive_engine as pe
from operating import proactive_engine_budget
from operating import proactive_engine_cards
from operating import proactive_engine_config
from operating import proactive_engine_inbox
from operating import proactive_engine_population
from operating.action_path import ActionPath
from operating.ontology_tools_investigate_follow_ups import DRAFT_ACTION
from operating.cases import CaseStore
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_cases import FakeResolver
from operator_api.workspace import object_workspace as objects
from operating.tests.test_proactive_engine import ROOT, admit, handle


def _pool_init(_spec) -> None:   # a worker without a session (spawned: by name)
    from operating import proactive_pool as pp
    pp._CALL = lambda _tool, _args: None


def _die_on_two(_call, item):
    if item == 2:
        os._exit(1)    # the worker process dies under this item
    return item * 10


def _sleep_for(_call, item):
    time.sleep(item)
    return item


# An entry script without a ``__main__`` guard (as an operator's one-off pass): each process
# that runs it appends its pid; it maps on the pool and prints the answers.
_UNGUARDED = """
import os, sys, json
sys.path.insert(0, {root!r})
with open({marks!r}, "a") as handle:
    handle.write(str(os.getpid()) + "\\n")
from unittest import mock
from operating import proactive_pool as pp
from operating.tests import test_proactive_engine_pool as t
pool = pp.ToolPool(None, 2)
with mock.patch.object(pp, "_init", t._pool_init):
    try:
        print(json.dumps(pool.map(t._die_on_two, [1, 3, 4])))
    finally:
        pool.close()
"""


class DetectorReserveTest(unittest.TestCase):
    def test_a_small_store_reserves_the_floor_a_large_one_or_none_the_full_reserve(self):
        """A rule over a fixture world is not counted at the live store's 1.5 GB (a capped
        test scope queued 63 rules one at a time); a large store and a store with no file
        (Postgres) keep the full reserve, and the reserve grows with the store between."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "small.sqlite"
            small.write_bytes(b"x" * 1000)
            self.assertEqual(pe.detector_reserve(small), proactive_engine_budget.DETECTOR_RESERVE_FLOOR_BYTES)
            middle = Path(tmp) / "middle.sqlite"
            with open(middle, "wb") as handle:
                handle.truncate(100_000_000)
            self.assertEqual(pe.detector_reserve(middle), 800_000_000)
            big = Path(tmp) / "big.sqlite"
            with open(big, "wb") as handle:
                handle.truncate(10_000_000_000)   # sparse
            self.assertEqual(pe.detector_reserve(big), pe.DETECTOR_RESERVE_BYTES)
            empty = Path(tmp) / "empty.sqlite"
            empty.write_bytes(b"")
            for path in (Path(tmp) / "absent.sqlite", empty, None):
                self.assertEqual(pe.detector_reserve(path), pe.DETECTOR_RESERVE_BYTES)


class PoolSizeTest(unittest.TestCase):

    def test_the_pool_size_is_the_hosts_and_an_unreadable_override_runs_serially(self):
        from operating import proactive_pool as pp
        self.assertEqual(pp.workers({pp.WORKERS_ENV: "3"}), 3)
        self.assertEqual(pp.workers({pp.WORKERS_ENV: "SYNTH"}), 0)   # never a guess
        self.assertEqual(pp.workers({pp.WORKERS_ENV: "-2"}), 0)
        self.assertTrue(1 <= pp.workers({}) <= pp.MAX_WORKERS)
        # never more than a quarter of the free memory (nor POOL_BYTES) at WORKER_BYTES each
        with mock.patch.object(pp, "_available_bytes", return_value=10 * pp.WORKER_BYTES), \
                mock.patch.object(pp, "_cgroup_cap", return_value=None), \
                mock.patch.object(pp.os, "cpu_count", return_value=64):
            self.assertEqual(pp.workers({}), 2)
        with mock.patch.object(pp, "_available_bytes", return_value=10 ** 13), \
                mock.patch.object(pp, "_cgroup_cap", return_value=None), \
                mock.patch.object(pp.os, "cpu_count", return_value=64):
            self.assertEqual(pp.workers({}) * pp.WORKER_BYTES <= pp.POOL_BYTES, True)
            # under a unit's memory cap: what it leaves beside the engine's own process
            with mock.patch.object(pp, "_cgroup_cap", return_value=12 * 2 ** 30):
                self.assertEqual(pp.workers({}), 2)
        self.assertIsNone(pp.from_host_config(None, Path("."), Path("."),
                                              {pp.WORKERS_ENV: "1"}))

    def test_a_dying_worker_fails_only_its_own_item(self):
        from operating import proactive_pool as pp
        pool = pp.ToolPool(None, 2)
        with mock.patch.object(pp, "_init", _pool_init):
            try:
                self.assertEqual(pool.map(_die_on_two, [1, 2, 3, 4, 5]),
                                 [10, pp.Failed(pp.WORKER_FAILED), 30, 40, 50])
                self.assertEqual(pool.map(_die_on_two, [7]), [70])   # the pool goes on
            finally:
                pool.close()

    def test_a_worker_never_reruns_the_entry_script_that_started_the_pool(self):
        # 2026-11-07: an unguarded entry script ran a whole engine pass in each spawned
        # worker before its first task; the margins read nothing for ~4,000 s
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            script, marks = Path(temp) / "entry.py", Path(temp) / "marks"
            script.write_text(_UNGUARDED.format(root=str(ROOT), marks=str(marks)), "utf-8")
            done = subprocess.run([sys.executable, str(script)], capture_output=True,
                                  text=True, timeout=300, cwd=temp,
                                  env={k: v for k, v in os.environ.items()
                                       if k.startswith(("PATH", "HOME", "LANG"))})
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]), [10, 30, 40])
            self.assertEqual(len(marks.read_text().split()), 1)   # the parent only

    def test_a_map_ends_at_a_window_that_shrinks_and_frees_its_workers(self):
        # the margins' head start: its map reads to the run's deadline until its rule's turn
        # opens a shorter window; it must end then, not when a running read next finishes,
        # and the next detector's map must not queue behind the abandoned reads
        import threading
        from operating import proactive_pool as pp
        pool = pp.ToolPool(None, 2)
        end = [time.monotonic() + 600]
        box: dict = {}
        with mock.patch.object(pp, "_init", _pool_init):
            try:
                self.assertEqual(pool.map(_sleep_for, [0]), [0])   # the workers are up
                runner = threading.Thread(target=lambda: box.setdefault(
                    "out", pool.map(_sleep_for, [120, 120, 120],
                                    left=lambda: end[0] - time.monotonic())))
                runner.start()
                time.sleep(1.5)
                started = time.monotonic()
                end[0] = started   # the window closes now
                runner.join(30)
                self.assertFalse(runner.is_alive())
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(box["out"], [pp.NOT_READ] * 3)
                started = time.monotonic()
                self.assertEqual(pool.map(_die_on_two, [7], left=lambda: 300), [70])
                self.assertLess(time.monotonic() - started, 100)   # fresh workers, no queue
            finally:
                pool.close()


class LiveCheckTest(unittest.TestCase):
    """live check ``case.proactive_engine`` over injected sources: its own
    command over the host config of ``case.customer_po_missing`` (FakeWorkbench plus a fake
    drive), the network refused. Evidence label: injected-transport."""

    SPEC_ID = "case.proactive_engine"

    def setUp(self) -> None:
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access attempted in an offline test")

        for target in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, target, refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.spec = json.loads((ROOT / "devloop" / "live-checks" / f"{self.SPEC_ID}.json")
                               .read_text("utf-8"))

    def world(self, *, fetch=None) -> tuple[dict, dict, object]:
        """The host config file (written to the temp dir), environ and host options of
        ``case.customer_po_missing``'s world (FakeWorkbench plus a fake drive)."""
        from integrations.tests import test_capture_sharepoint as tsp
        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from operating.tests import order_fixture as fixture
        from operating.tests.test_case_workspace_live import _customer_drive

        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        fake.tables = fixture.tables()
        fake.tables["public.app_sales_order"].append(
            fixture._so("so-m", "OR1000888001", "cust-1", "CONFIRMED", "28"))
        config = json.loads((ROOT / "examples" / "customer_po_missing_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(ROOT / "examples" / config[key])
        config["sharepoint"]["scope"] = str(ROOT / "examples" / config["sharepoint"]["scope"])
        (self.temp / "host.json").write_text(json.dumps(config), "utf-8")
        drive = _customer_drive()
        environ = {**tsp.ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY}
        return environ, {"sender": drive, "workbench_fetch": fetch or fake,
                         "sleep": lambda _s: None}, drive

    def run_spec(self, *, fetch=None) -> tuple[int, dict, str]:
        environ, options, drive = self.world(fetch=fetch)
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", str(self.temp)) for arg in self.spec["command"][3:]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pe.main(argv, environ=environ, graph_sender=drive, host_options=options)
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_a_host_cycle_runs_every_detector_into_its_inbox_within_its_budget(self):
        # The host's own post-build refresh (no proactive_engine.main): the coverage rule
        # and the tool-caller rules run as the capture principal, never not_configured
        from operating.host import Host, HostConfig, projection_path

        environ, options, drive = self.world()
        state = self.temp / "state"
        report = Host(state, HostConfig.from_file(self.temp / "host.json"),
                      environ=environ, **options).cycle()
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        self.assertEqual((report["state"], report["projection"]["state"]),
                         ("complete", "built"))
        hook = report["projection"]["proactive"]
        self.assertEqual(hook["state"], "ran", hook)
        for name in ("order_without_customer_po", "orders_at_risk", "card_rules"):
            self.assertEqual(hook["detectors"][name], "complete", name)
        store = projection_path(state)
        with contextlib.closing(sqlite3.connect(pe.inbox_path(store))) as conn:
            ran = dict(conn.execute("SELECT detector, status FROM detector_runs"))
            kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM gaps")}
        self.assertEqual(ran, hook["detectors"])
        self.assertTrue({"order_without_customer_po", "missing_in_record"} <= kinds, kinds)
        # a reader without any grant is shown nothing of what the capture principal found
        found = pe.read_inbox(pe.inbox_path(store), store, reader="plx:nobody",
                              may_read=lambda _key: False, held=frozenset(),
                              decide=admit)
        self.assertEqual((found["gaps"], found["scanned"]), ([], 0))
        # the budget spent: every rule is truncated for it, the cycle still complete
        with mock.patch.object(pe, "REFRESH_BUDGET_SECONDS", 0):
            spent = Host(state, HostConfig.from_file(self.temp / "host.json"),
                         environ=environ, **self.world()[1]).cycle()
        self.assertEqual(spent["state"], "complete")
        hook = spent["projection"]["proactive"]
        self.assertEqual(set(hook["detectors"].values()), {"truncated"}, hook)
        self.assertEqual(set(map(tuple, hook["detector_reasons"].values())),
                         {(pe.TIME_BUDGET,)})

    def test_the_spec_passes_on_the_whole_store_with_a_cited_gap_and_reads_blocked(self):
        lv.validate_spec(self.spec, stem=self.SPEC_ID)
        code, summary, text = self.run_spec()
        self.assertEqual(lv.check_expectations(self.spec, code, summary), [])
        self.assertGreaterEqual(summary["gaps_cited"], 1)
        self.assertFalse(summary["scope_truncated"])
        self.assertNotIn("OR1000888001", text)   # counts and enums only
        mine, other = summary["readers"]
        self.assertTrue(mine["investigator"])
        self.assertLessEqual(other["shown"], mine["shown"])
        self.assertLessEqual(other["scanned"], mine["scanned"])

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        code, summary, _text = self.run_spec(fetch=portal_down)
        self.assertTrue(lv.blocked_parts(summary))
        self.assertEqual(lv.check_expectations(
            self.spec, code, summary, self.spec["expect_blocked"]), [])






if __name__ == "__main__":
    unittest.main()
