"""The host's ingest mode (``operating.host`` with ``ingest``; ``operating.ingest_forever``
its capture-CLI reads) at the host's own boundary, with fake capture CLIs and no network.

One writer per state directory: an ingest cycle is
a host ledger cycle under the host's one cycle lock, rebuilds the host's projection and is
checked by the host's ``check``. An unset people exclusion blocks every source; one
failing source does not stop the cycle; each source's cursor carries from one cycle to
the next; a killed runner's capture child is stopped before the next cycle reads.
"""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock
import uuid

from integrations.people_exclusion import ENV_FILE, ENV_IDS
from operating import host as host_module
from operating import ingest_forever as forever
from operating.ingest_forever import SourceSpec, summarize_mail

ROOT = Path(__file__).resolve().parents[2]

# One fake capture CLI: a mail-shaped report; it keeps a cursor in its own store under
# the state directory and resumes from it the next time (fewer requests).
FAKE = textwrap.dedent("""
    import json, os, sys, time
    state, name, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    store = os.path.join(state, "fake-" + name)
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "ran"), "a") as handle:
        handle.write("1\\n")
    with open(os.path.join(store, "starts"), "a") as handle:   # when (one clock)
        handle.write("%r\\n" % time.monotonic())
    if mode == "fail":
        print(json.dumps({"error": "SourceUnavailable", "error_class": "upstream",
                          "http_status": 503,
                          "message": "mailbox alice@example.com unreachable"}),
              file=sys.stderr)
        sys.exit(1)
    if mode == "crash":   # an uncaught exception: a traceback, no JSON line
        import sqlite3
        raise sqlite3.IntegrityError("UNIQUE constraint failed: alice@example.com")
    if mode == "stdout_refusal":   # integrations.mc_sources: the code on stdout
        print(json.dumps({"ok": False, "error_class": "CaptureError",
                          "error": "invalid_handles"}, indent=1))
        sys.exit(1)
    if mode == "stderr_refusal":   # integrations.bc_read: the code on stderr
        print(json.dumps({"error": "CaptureError", "error_code": "binding_mismatch",
                          "message": "capture store belongs to another tenant's audience"}),
              file=sys.stderr)
        sys.exit(1)
    if mode == "hang":
        with open(os.path.join(store, "pid"), "w") as handle:
            handle.write(str(os.getpid()))
        time.sleep(60)
    if mode == "barrier":   # completes only when another barrier source runs meanwhile
        mine = os.path.join(state, "barrier-" + name)
        open(mine, "w").close()
        deadline = time.time() + 3
        while len([f for f in os.listdir(state) if f.startswith("barrier-")]) < 2:
            if time.time() > deadline:
                os.remove(mine)
                print(json.dumps({"error_code": "ran_alone"}), file=sys.stderr)
                sys.exit(1)
            time.sleep(0.02)
        time.sleep(0.2)   # the other one sees this one too before it leaves
        os.remove(mine)
    if mode == "slow":   # holds the cycle until the test drops <state>/release
        open(os.path.join(store, "started"), "w").close()
        # bounded, but past every wait of the tests that release it (a loaded host
        # took over 30 s, so the source ended on its own before the test released it)
        deadline = time.time() + 300
        while not os.path.exists(os.path.join(state, "release")) and time.time() < deadline:
            time.sleep(0.05)
    if mode == "gated":   # finishes once the test drops <state>/gate-<name> (bounded)
        deadline = time.time() + 300
        while not os.path.exists(os.path.join(state, "gate-" + name)) \
                and time.time() < deadline:
            time.sleep(0.02)
    if mode in ("busy", "long"):   # marks itself running for one or three seconds
        marker = os.path.join(store, "running")
        open(marker, "w").close()
        time.sleep(1.0 if mode == "busy" else 3.0)
        os.remove(marker)
    if mode == "probe":   # whether the hung source's child still runs when this one starts
        pid = int(open(os.path.join(state, "fake-hung", "pid")).read())
        try:
            os.kill(pid, 0)
            alive = True
            if os.path.exists("/proc/%d/stat" % pid):
                alive = open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[0] \
                    not in ("Z", "X")
        except ProcessLookupError:
            alive = False
        with open(os.path.join(store, "orphan_alive"), "w") as handle:
            handle.write(str(alive))
    cursor = os.path.join(store, "cursor")
    resumed = os.path.exists(cursor)
    with open(cursor, "w") as handle:
        handle.write("delta")
    requests = 1 if resumed else 3
    print(json.dumps({"runs": [{"mailboxes": [{"folders": [
        {"complete": True, "resumed_from": "delta_url" if resumed else None}]}]}],
        "requests_sent": requests, "blocked_upstream": [],
        "summary": {"messages": 0 if resumed else 4, "folders_complete": 1,
                    "note": "alice@example.com"}}))
""")


def fake_table(script: Path, *names: str, projected: dict | None = None,
               writes: dict | None = None) -> dict[str, SourceSpec]:
    """Fake capture CLIs; ``projected``: name -> the source family whose capture store in
    the state directory the duplicate check reads for it (default none); ``writes``:
    name -> its ``SourceSpec.writes``."""
    def argv(name: str):
        return lambda state, options: [sys.executable, str(script), str(state), name,
                                       options.get("mode", "ok")]
    return {name: SourceSpec(name, "fake-source", argv(name), summarize_mail,
                             {"mode": "str", "timeout_seconds": "seconds"},
                             budget_field=None, projected=(projected or {}).get(name),
                             writes=(writes or {}).get(name))
            for name in names}


def alive(pid: int) -> bool:
    """A running process (a zombie no parent reaped yet is not one)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_file = Path(f"/proc/{pid}/stat")
    try:
        return stat_file.read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        return True


# A host ``run`` in ingest mode over the fake table, in its own process: the test kills it
# mid-cycle (SIGKILL or SIGTERM).
DRIVER = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from operating import host
    from operating.tests.test_ingest_forever import fake_table
    script, state, config, ids = sys.argv[1:5]
    env = {"PATH": os.environ.get("PATH", ""), "PLX_EXCLUDED_PEOPLE_OIDS": ids}
    sys.exit(host.main(["run", "--state-dir", state, "--config", config], environ=env,
                       capture_sources=fake_table(Path(script), "hung"),
                       disk_free=lambda path: 1 << 50))
""")


def ROOMY(_path) -> int:
    """The free-space measure every test host gets: never the machine's real disk (a
    machine short of space must not turn a test into a ``low_disk`` stop)."""
    return 1 << 50


def _refuse_network(*_args, **_kwargs):
    raise AssertionError("network access in a test")


class IngestModeTest(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = mock.patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.script = self.tmp / "fake_capture.py"
        self.script.write_text(FAKE, encoding="utf-8")
        self.state = self.tmp / "state"
        (self.tmp / "mirror-audience.json").write_text(json.dumps({
            "schema": "plx-source-mirror-audience/v1", "tenant": "plx", "audiences": {}}),
            encoding="utf-8")
        # Generated at run time: no real object ID in the repository.
        self.env = {"PATH": os.environ.get("PATH", ""), ENV_IDS: str(uuid.uuid4())}

    def config(self, sections: dict, *, ingest: dict | None = None, **top) -> Path:
        path = self.tmp / f"host-{uuid.uuid4().hex[:6]}.json"
        path.write_text(json.dumps({
            "schema": host_module.CONFIG_SCHEMA, "mirror_audience": "mirror-audience.json",
            "ingest": {"mode": forever.MODE, "grace_seconds": 1} if ingest is None
            else ingest, **sections, **top}), encoding="utf-8")
        return path

    def main(self, *argv: str, table: dict | None, env: dict | None = None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_module.main(list(argv), environ=self.env if env is None else env,
                                    disk_free=ROOMY,
                                    capture_sources=table)
        return code, out.getvalue(), err.getvalue()

    def run_cycle(self, config: Path, table: dict, env: dict | None = None):
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(config), table=table, env=env)
        self.assertEqual(code, 0, err)
        return json.loads(out), out

    def check(self, config: Path, table: dict, cycles: int = 2, env: dict | None = None):
        code, out, err = self.main("check", "--state-dir", str(self.state), "--config",
                                   str(config), "--cycles", str(cycles), table=table, env=env)
        self.assertEqual(code, 0, err)
        return json.loads(out), out

    def ran(self, name: str) -> bool:
        return (self.state / f"fake-{name}" / "ran").exists()

    def ledger(self, query: str) -> list[tuple]:
        conn = sqlite3.connect(self.state / host_module.HOST_DIR / host_module.LEDGER_FILE)
        try:
            return conn.execute(query).fetchall()
        finally:
            conn.close()

    def test_unset_exclusion_blocks_every_source(self) -> None:
        table = fake_table(self.script, "mail", "teams")
        config = self.config({"mail": {}, "teams": {}})
        for env in ({"PATH": self.env["PATH"]}, {**self.env, ENV_IDS: ""},
                    {**self.env, ENV_IDS: "not-an-object-id"}):
            report, _ = self.run_cycle(config, table, env)
            self.assertEqual(report["runner"], host_module.INGEST_RUNNER)
            self.assertEqual({n: o["state"] for n, o in report["sources"].items()},
                             {"mail": "blocked", "teams": "blocked"})
            for outcome in report["sources"].values():
                (block,) = outcome["capture"]["blocked"]
                self.assertIn(block["error_code"], {"excluded_people_oids_absent",
                                                    "excluded_people_oids_invalid"})
                self.assertEqual(block["later_ask"], "excluded-people-oids")
                self.assertIn(block["cause"], forever.CAUSES)
                self.assertEqual(outcome["outage"]["error_code"], block["error_code"])
            self.assertFalse(self.ran("mail") or self.ran("teams"))
        # Each refusal is a host ledger cycle; the host's check reports it blocked.
        self.assertEqual(self.ledger("SELECT COUNT(*) FROM cycle")[0][0], 3)
        self.state = self.tmp / "check-state"
        report, _ = self.check(config, table, env={"PATH": self.env["PATH"], ENV_FILE: ""})
        self.assertEqual(report["checks"]["cycles_run"], 2)
        self.assertEqual({b["error_code"] for b in report["blocked_upstream"]},
                         {"excluded_people_oids_absent", "no_completed_source_to_resume"})
        self.assertFalse(self.ran("mail"))

    def test_failing_source_does_not_stop_the_cycle(self) -> None:
        table = fake_table(self.script, "broken", "hung", "mail", "crashed")
        config = self.config({"broken": {"mode": "fail"},
                              "hung": {"mode": "hang", "timeout_seconds": 1}, "mail": {},
                              "crashed": {"mode": "crash"}})
        report, text = self.run_cycle(config, table)
        states = {n: o["state"] for n, o in report["sources"].items()}
        self.assertEqual(states, {"broken": "failed", "hung": "failed", "mail": "captured",
                                  "crashed": "failed"}, report["sources"]["mail"])
        # A child dead of an uncaught exception is named by its class, never its message.
        crashed = report["sources"]["crashed"]["capture"]
        self.assertEqual((crashed["error_code"], crashed["exit_code"]),
                         ("uncaught:IntegrityError", 1))
        self.assertEqual(report["state"], "failed")   # as the host's own failed source
        broken = report["sources"]["broken"]
        self.assertEqual((broken["capture"]["error_code"], broken["capture"]["http_status"],
                          broken["capture"]["exit_code"]), ("SourceUnavailable", 503, 1))
        self.assertEqual(broken["outage"], {"http_status": 503,
                                            "error_code": "SourceUnavailable"})
        self.assertEqual(report["sources"]["hung"]["capture"]["error_code"], "child_timeout")
        # The hung child was killed, not left behind; no message or address is printed.
        pid = int((self.state / "fake-hung" / "pid").read_text())
        self.assertFalse(alive(pid))
        self.assertNotIn("@", text)
        self.assertNotIn("unreachable", text)
        self.assertNotIn("UNIQUE constraint", text)
        self.assertEqual(self.ledger("SELECT source, generation, error FROM source_state "
                                     "ORDER BY source"),
                         [("broken", 0, "SourceUnavailable"),
                          ("crashed", 0, "uncaught:IntegrityError"),
                          ("hung", 0, "child_timeout"), ("mail", 1, None)])
        self.assertEqual(json.loads(self.ledger("SELECT parts FROM source_blocked WHERE "
                                                "source='broken'")[0][0])[0]["source"],
                         "fake-source")
        # A credential-shaped variable reaches only the source that declares it.
        table["mail"] = SourceSpec("mail", "fake-source", table["mail"].argv,
                                   summarize_mail, table["mail"].fields,
                                   env=("FAKE_SOURCE_TOKEN",), budget_field=None)
        report, _ = self.run_cycle(config, table)
        (block,) = report["sources"]["mail"]["capture"]["blocked"]
        self.assertEqual((block["error_code"], block["later_ask"], block["missing_settings"]),
                         ("credentials_absent", "credentials", ["FAKE_SOURCE_TOKEN"]))
        self.assertEqual(report["sources"]["mail"]["state"], "blocked")
        child = forever.child_env({"FAKE_SOURCE_TOKEN": "x", "OTHER_SECRET": "y", "PATH": "p",
                                   "PLX_INGEST_CPU_TOTAL": "8"},
                                  table["mail"], self.state)
        self.assertEqual(child, {"FAKE_SOURCE_TOKEN": "x", "PATH": "p",
                                 "PLX_INGEST_CPU_BUDGET": "4", "PLX_INGEST_DRAIN": "1",
                                 "PLX_WORKBENCH_STATE_DIR": str(self.state)})

    def test_cursors_persist_across_cycles_under_the_host_check(self) -> None:
        table = fake_table(self.script, "mail", "teams")
        config = self.config({"mail": {}, "teams": {}})
        first = self.run_cycle(config, table)[0]
        second = self.run_cycle(config, table)[0]
        self.assertEqual((first["cycle"], second["cycle"]), (1, 2))
        for name in ("mail", "teams"):
            before, after = first["sources"][name], second["sources"][name]
            self.assertEqual((before["state"], after["state"]), ("captured", "captured"))
            self.assertEqual((before["capture"]["resumed_from_cursor"],
                              after["capture"]["resumed_from_cursor"]), (False, True))
            self.assertLess(after["counts"]["requests"], before["counts"]["requests"])
        self.assertEqual(self.ledger("SELECT source, generation FROM source_state ORDER BY "
                                     "source"), [("mail", 2), ("teams", 2)])
        status = host_module.Host(self.state, disk_free=ROOMY).status()
        self.assertEqual([s["source"] for s in status["sources"]], ["mail", "teams"])
        # The host's check on the same fakes: cycle 2 resumed every completed source.
        self.state = self.tmp / "check-state"
        report, text = self.check(config, table)
        self.assertEqual(report["schema"], host_module.CHECK_SCHEMA)
        self.assertNotIn("blocked_upstream", report)
        checks = report["checks"]
        self.assertEqual({k: checks[k] for k in (
            "cycles_run", "resume_checked", "completed_not_resumed", "requests_grew",
            "sources_without_status", "blocked_without_cause", "untyped_status", "leaked",
            "cycles_overlapping", "duplicate_items", "duplicates_unchecked",
            "state_persistent", "mail_folders_restarted", "sharepoint_roots_restarted")},
            {"cycles_run": 2, "resume_checked": 2, "completed_not_resumed": 0,
             "requests_grew": 0, "sources_without_status": 0, "blocked_without_cause": 0,
             "untyped_status": 0, "leaked": 0, "cycles_overlapping": 0,
             "duplicate_items": 0, "duplicates_unchecked": 0, "state_persistent": False,
             "mail_folders_restarted": None, "sharepoint_roots_restarted": None})
        self.assertEqual(checks["cycles_by_runner"], {"ingest": 2})
        self.assertNotIn(self.env[ENV_IDS], text)
        self.assertNotIn(str(self.state), text)
        # Not vacuous: a capture that re-reads from scratch, or sends more requests on its
        # second cycle, fails it.
        for broken, counter in (("resumed = False", "completed_not_resumed"),
                                ("requests = 5 if resumed else 3", "requests_grew")):
            source = FAKE.replace({"completed_not_resumed": "resumed = os.path.exists(cursor)",
                                   "requests_grew": "requests = 1 if resumed else 3"}[counter],
                                  broken)
            script = self.tmp / f"{counter}.py"
            script.write_text(source, encoding="utf-8")
            self.state = self.tmp / f"{counter}-state"
            report, _ = self.check(config, fake_table(script, "mail", "teams"))
            self.assertEqual(report["checks"][counter], 2)

    def test_ingest_cycle_is_a_host_ledger_cycle_with_its_projection(self) -> None:
        """Each ingest cycle is a cycle of the host's ledger (its lock, its cycle row, its
        source generations) and rebuilds the host's projection; the check counts every
        item a projected capture store holds twice."""
        from integrations import bc_read
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_bc_capture import BOOKS, ENV, Clock, FakeBc

        store = host_module.state_capture_paths(self.state)["bc"]
        store.parent.mkdir(parents=True)
        bc_read.capture_probe(ENV, sender=FakeBc({"purchaseReceipts": 3}), db=store,
                              page_size=2, passes=1, books=BOOKS, monotonic=Clock().monotonic,
                              retry=BoundedBackoff(sleep=lambda s: None),
                              entity_sets=("purchaseReceipts",))
        table = fake_table(self.script, "bc", "mail", projected={"bc": "bc"})
        config = self.config({"bc": {}, "mail": {}})
        report, _ = self.check(config, table)
        self.assertNotIn("blocked_upstream", report)
        self.assertEqual([(c["cycle"], c["runner"], c["projection"]["state"],
                           c["projection"]["generation"],
                           c["projection"]["previous_generation"])
                          for c in report["cycles"]],
                         [(1, "ingest", "built", 1, None), (2, "ingest", "built", 2, 1)])
        self.assertTrue(report["checks"]["projection_chained"])
        self.assertEqual([(cycle, state, json.loads(body)["runner"]) for cycle, state, body
                          in self.ledger("SELECT cycle_id, state, report FROM cycle "
                                         "ORDER BY cycle_id")],
                         [(1, "complete", "ingest"), (2, "complete", "ingest")])
        self.assertEqual(self.ledger("SELECT source, generation, served_from, complete FROM "
                                     "source_state ORDER BY source"),
                         [("bc", 2, "live", 1), ("mail", 2, "live", 1)])
        self.assertEqual(self.ledger("SELECT generation, previous_generation FROM projection "
                                     "ORDER BY generation"), [(1, None), (2, 1)])
        self.assertTrue(host_module.projection_path(self.state).is_file())
        self.assertGreater(report["cycles"][-1]["projection"]["objects"], 0)
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        status = host_module.Host(self.state, disk_free=ROOMY).status()
        self.assertEqual((status["projection"]["generation"], status["last_cycle"]["state"]),
                         (2, "complete"))
        duplicates = report["duplicates"]
        self.assertEqual((duplicates["bc"]["state"], duplicates["bc"]["duplicate_items"]),
                         ("checked", 0))
        self.assertGreater(duplicates["bc"]["items_checked"], 0)
        self.assertEqual(duplicates["mail"]["state"], "not_projected")
        self.assertEqual({k: report["checks"][k] for k in (
            "duplicate_items", "duplicates_unchecked", "cycles_overlapping")},
            {"duplicate_items": 0, "duplicates_unchecked": 0, "cycles_overlapping": 0})
        # Not vacuous: one record stored again with unchanged content is one duplicate item.
        conn = sqlite3.connect(store)
        try:
            with conn:
                row = conn.execute("SELECT * FROM versions WHERE superseded_at IS NULL "
                                   "ORDER BY capture_id LIMIT 1").fetchone()
                names = [d[0] for d in conn.execute("SELECT * FROM versions").description]
                copy = dict(zip(names, row), capture_id=None, superseded_at=row[
                    names.index("recorded_at")], superseded_by=row[0])
                conn.execute(f"INSERT INTO versions ({','.join(copy)}) VALUES "
                             f"({','.join('?' * len(copy))})", list(copy.values()))
        finally:
            conn.close()
        report, _ = self.check(config, table)
        self.assertEqual((report["checks"]["duplicate_items"],
                          report["duplicates"]["bc"]["duplicate_items"]), (1, 1))

    def wait_for_child(self, driver: subprocess.Popen) -> tuple[int, Path]:
        pid_file = self.state / "fake-hung" / "pid"
        children = self.state / host_module.HOST_DIR / host_module.CHILDREN_FILE
        deadline = time.monotonic() + 30
        while not (pid_file.is_file() and pid_file.read_text()
                   and children.is_file() and json.loads(children.read_text())["children"]):
            self.assertIsNone(driver.poll(), "the runner exited before starting its child")
            self.assertLess(time.monotonic(), deadline, "the runner never started its child")
            time.sleep(0.05)
        orphan = int(pid_file.read_text())
        self.addCleanup(lambda: alive(orphan) and os.kill(orphan, signal.SIGKILL))
        return orphan, children

    def driver(self, config: Path) -> subprocess.Popen:
        driver = subprocess.Popen([sys.executable, "-c", DRIVER, str(self.script),
                                   str(self.state), str(config), self.env[ENV_IDS]],
                                  cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: driver.poll() is None and driver.kill())
        return driver

    def test_killed_runner_child_is_stopped_before_the_next_cycle(self) -> None:
        """An ingest ``run`` SIGKILLed mid-cycle leaves its capture child running (its own
        session); the next cycle, whichever mode takes the lock, stops that child's process
        group before any source runs, and marks the dead cycle interrupted."""
        hang = self.config({"hung": {"mode": "hang", "timeout_seconds": 60}})
        driver = self.driver(hang)
        orphan, children = self.wait_for_child(driver)
        driver.send_signal(signal.SIGKILL)
        driver.wait(timeout=30)
        self.assertTrue(alive(orphan))   # the runner's death alone does not stop it
        self.assertEqual(stat.S_IMODE(children.stat().st_mode), 0o600)
        # An unreadable child record fails closed: no source runs over a child it cannot
        # account for.
        kept = children.read_bytes()
        children.write_text("{", encoding="utf-8")
        probe = self.config({"probe": {"mode": "probe"}})
        table = fake_table(self.script, "probe")
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(probe), table=table)
        self.assertEqual((code, out, json.loads(err)),
                         (2, "", {"error": "cycle_children_unreadable"}))
        self.assertFalse(self.ran("probe"))
        self.assertTrue(alive(orphan))
        children.write_bytes(kept)
        report, _ = self.run_cycle(probe, table)
        self.assertEqual(report["recovered_children"],
                         {"recorded": 1, "stopped": 1, "gone": 0, "not_ours": 0})
        self.assertEqual((self.state / "fake-probe" / "orphan_alive").read_text(), "False")
        self.assertFalse(alive(orphan))
        self.assertEqual(json.loads(children.read_text())["children"], [])
        self.assertEqual(self.ledger("SELECT cycle_id, state FROM cycle ORDER BY cycle_id"),
                         [(1, "interrupted"), (2, "complete")])

    def test_terminated_runner_stops_its_child_on_the_way_out(self) -> None:
        hang = self.config({"hung": {"mode": "hang", "timeout_seconds": 60}})
        driver = self.driver(hang)
        orphan, children = self.wait_for_child(driver)
        driver.send_signal(signal.SIGTERM)
        self.assertEqual(driver.wait(timeout=30), 128 + signal.SIGTERM)
        self.assertFalse(alive(orphan))
        self.assertEqual(json.loads(children.read_text())["children"], [])

    def test_sources_at_cardinality(self) -> None:
        """1, 10 and 60 configured sources: one ledger row and one outcome each, none lost
        or doubled, every one resumed in the second cycle."""
        for n in (1, 10, 60):
            with self.subTest(sources=n):
                self.state = self.tmp / f"state-{n}"
                names = [f"s{i:02d}" for i in range(n)]
                table = fake_table(self.script, *names)
                config = self.config({name: {} for name in names})
                started = time.monotonic()
                report, _ = self.check(config, table)
                self.assertLess(time.monotonic() - started, 30 + n)
                self.assertEqual([sorted(c["sources"]) for c in report["cycles"]],
                                 [names, names])
                self.assertEqual({k: report["checks"][k] for k in (
                    "sources_configured", "resume_checked", "completed_not_resumed",
                    "sources_without_status", "cycles_overlapping")},
                    {"sources_configured": n, "resume_checked": n, "completed_not_resumed": 0,
                     "sources_without_status": 0, "cycles_overlapping": 0})
                self.assertEqual(self.ledger("SELECT COUNT(*), MIN(generation), "
                                             "MAX(generation) FROM source_state"),
                                 [(n, 2, 2)])

    def test_config_fails_closed(self) -> None:
        table = fake_table(self.script, "mail")
        for document, ingest, error in (
                ({"mail": {"timeout_seconds": 0}}, None, "host_config_invalid:mail"),
                ({"mail": {}}, {"mode": "loop"}, "host_config_invalid:ingest"),
                ({"mail": {}}, {"mode": forever.MODE, "grace_seconds": 0},
                 "host_config_invalid:ingest"),
                ({"mail": {}}, {"mode": forever.MODE, "cpu_budget": 0},
                 "host_config_invalid:ingest"),
                ({}, None, "host_config_invalid:ingest")):
            code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                       str(self.config(document, ingest=ingest)), table=table)
            self.assertEqual((code, out, json.loads(err)), (2, "", {"error": error}),
                             document)
        self.assertFalse(self.ran("mail"))
        # A capture CLI section is no source of the host's own readers.
        plain = self.tmp / "plain.json"
        plain.write_text(json.dumps({"schema": host_module.CONFIG_SCHEMA,
                                     "mirror_audience": "mirror-audience.json",
                                     "sharepoint_lists": {}}), encoding="utf-8")
        # (A cycle-mode config ignores it with a warning: no source is configured from it.)
        held = host_module.HostConfig.from_file(plain)
        self.assertEqual((held.sources, held.config_warnings), ({}, ["sharepoint_lists"]))
        # Serving refuses a state directory a reboot throws away (its cursors with it).
        code, out, err = self.main("serve", "--no-console", "--max-cycles", "1",
                                   "--state-dir", str(self.state), "--config",
                                   str(self.config({"mail": {}})), table=table)
        self.assertEqual((code, json.loads(err)), (2, {"error": "state_dir_ephemeral"}))
        self.assertFalse(self.ran("mail"))
        # On a kept directory serve runs its cycles through the same host cycle.
        with mock.patch.object(forever, "ephemeral", return_value=False):
            code, out, err = self.main("serve", "--no-console", "--max-cycles", "1",
                                       "--state-dir", str(self.state), "--config",
                                       str(self.config({"mail": {}})), table=table)
        self.assertEqual(code, 0, err)
        # Ingest mode serves continuously: one line per refresh, then the run's summary.
        decoder, documents, at = json.JSONDecoder(), [], 0
        while out[at:].strip():
            document, at = decoder.raw_decode(out, len(out) - len(out[at:].lstrip()))
            documents.append(document)
        refresh, summary = documents
        self.assertEqual((refresh["runner"], refresh["state"], refresh["continuous"]),
                         ("ingest", "complete", True))
        self.assertEqual((summary["schema"], summary["reason"], summary["refreshes"]),
                         (host_module.CONTINUOUS_SCHEMA, "max_refreshes", 1))

    def test_forward_config_keys_warn_and_a_start_loop_is_recorded(self) -> None:
        """A key this checkout does not know (a newer checkout's ``ingest`` setting, a
        source option, a top-level section) is ignored with a warning, never a refusal
        that restarts the unit forever (2026-11-07: ``ingest.cpu_budget`` held captures in
        a 23 h restart loop). A start that is still refused is recorded, so ops_health
        names the loop; the next good start clears it."""
        from operating import ops_health
        table = fake_table(self.script, "mail")
        forward = self.config({"mail": {"unknown_key": 1}, "nosuch": {}},
                              ingest={"mode": forever.MODE, "grace_seconds": 1,
                                      "future_knob": 4})
        code, out, err = self.main("run", "--state-dir", str(self.state), "--config",
                                   str(forward), table=table)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(err.splitlines()[0]),
                         {"warning": "host_config_keys_ignored",
                          "keys": ["ingest.future_knob", "mail.unknown_key", "nosuch"]})
        self.assertTrue(self.ran("mail"))
        # the unknown option never reaches the capture CLI
        self.assertNotIn("unknown_key", host_module.HostConfig.from_file(
            forward, capture_sources=table).sources["mail"])
        # a refused serve start, again and again: the restart loop is a breach
        bad = self.config({"mail": {}}, ingest={"mode": "loop"})
        for _ in range(3):
            code, _out, err = self.main("serve", "--no-console", "--max-cycles", "1",
                                        "--state-dir", str(self.state), "--config",
                                        str(bad), table=table)
            self.assertEqual((code, json.loads(err)), (2, {"error": "host_config_invalid:ingest"}))
        report = ops_health.collect(self.state, environ={}, include_api=False)
        start = next(c for c in report["checks"] if c["name"] == "ingest_start")
        self.assertEqual((start["state"], start["value"]), ("breach", 3))
        self.assertIn("host_config_invalid:ingest", start["detail"])
        self.assertTrue(ops_health.capture_alarm(report)["start_loop"])
        # a good start clears it and records each source's normal turn
        with mock.patch.object(forever, "ephemeral", return_value=False):
            code, _out, err = self.main("serve", "--no-console", "--max-cycles", "1",
                                        "--state-dir", str(self.state), "--config",
                                        str(forward), table=table)
        self.assertEqual(code, 0, err)
        held = ops_health.serve_health(self.state)
        self.assertEqual(held["failures"], [])
        self.assertEqual(set(held["turn_seconds"]), {"mail"})
        self.assertIn("ingest.future_knob", held["config_warnings"])
        report = ops_health.collect(self.state, environ={}, include_api=False)
        self.assertEqual(next(c for c in report["checks"] if c["name"] == "ingest_start")
                         ["state"], "ok")
        # a setting that did not take effect (a typo of ingest.builds) is a Home alarm
        ignored = next(c for c in report["checks"] if c["name"] == "config_keys_ignored")
        self.assertEqual(ignored["state"], "breach")
        alarm = ops_health.capture_alarm(report)
        self.assertIn("capture settings not applied: ingest.future_knob", alarm["line"])
        self.assertFalse(alarm["start_loop"])

    def test_live_check_config_on_the_real_table(self) -> None:
        """The live check's own config on the real capture table, without credentials:
        every source is blocked with a typed cause, no capture runs, nothing leaks."""
        config = ROOT / "examples" / "ingest_forever.live-check.json"
        loaded = host_module.HostConfig.from_file(config)
        self.assertEqual(sorted(loaded.sources), sorted(forever.SOURCES))
        report, text = self.check(config, None)
        states = {n: o["state"] for n, o in report["cycles"][-1]["sources"].items()}
        self.assertEqual(states, dict.fromkeys(forever.SOURCES, "blocked"))
        self.assertTrue(report["blocked_upstream"])
        self.assertTrue(all(forever.is_typed(b) for b in report["blocked_upstream"]))
        self.assertEqual({k: report["checks"][k] for k in (
            "sources_without_status", "blocked_without_cause", "untyped_status", "leaked",
            "cycles_overlapping")}, dict.fromkeys((
                "sources_without_status", "blocked_without_cause", "untyped_status",
                "leaked", "cycles_overlapping"), 0))
        self.assertEqual({c["projection"]["state"] for c in report["cycles"]},
                         {"no_captures"})
        self.assertFalse(report["sharepoint_coverage"]["complete"])
        self.assertNotIn(self.env[ENV_IDS], text)
        # Only the host ledger and the state label every host binds: nothing captured.
        self.assertEqual(sorted(p.name for p in self.state.iterdir()),
                         ["host", "session-state-label.json"])

    def test_real_bc_report_resumes_with_receipt_lines_read_in_full(self) -> None:
        """Two cycles of the real BC capture (injected transport) over one store: the
        receipt headers resume from their watermark; their lines, read in full every pass
        by design (bc_read: Undo Receipt edits posted receipts), are not a resume gap."""
        from integrations import bc_read
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_bc_capture import BOOKS, ENV, Clock, FakeBc

        fake, clock = FakeBc({"purchaseReceipts": 3}), Clock()
        db = self.tmp / "bc.sqlite"
        runs = [forever._judge(forever._base(forever.SOURCES["bc"]),
                               forever.summarize_bc(bc_read.capture_probe(
                                   ENV, sender=fake, db=db, page_size=2, passes=1,
                                   books=BOOKS, monotonic=clock.monotonic,
                                   retry=BoundedBackoff(sleep=lambda s: None),
                                   entity_sets=("purchaseReceipts",))))
                for _ in range(2)]
        self.assertEqual([r["status"] for r in runs], ["complete", "complete"])
        self.assertEqual([r["resumed_from_cursor"] for r in runs], [False, True])
        self.assertGreater(runs[1]["units_full_by_design"], 0)

class CpuBudgetTest(unittest.TestCase):
    """One host budget for the mail and SharePoint children's text extraction and OCR
    (audit 2026-11-06: 20 extraction workers beside the rest of the host on 16 cores)."""

    def test_each_extracting_child_gets_its_share_and_caps_its_pools(self) -> None:
        from integrations import cpu_budget
        spec = lambda name: SourceSpec(name, "fake", lambda *_: [], summarize_mail, ())
        state = Path("/nonexistent")
        # configured: the total is split between the extracting children only
        settings = forever.ingest_settings({"mode": forever.MODE, "cpu_budget": 8})
        environ = forever.budget_environ(settings, {"PATH": "p"})
        for name, expected in (("mail", "4"), ("sharepoint", "4"), ("teams", None)):
            env = forever.child_env(environ, spec(name), state)
            self.assertEqual(env.get(cpu_budget.BUDGET_ENV), expected, name)
            self.assertNotIn(cpu_budget.TOTAL_ENV, env)
        # absent: the host's load as the child starts (16 cores: 14 quiet, 8 busy)
        settings = forever.ingest_settings({"mode": forever.MODE})
        self.assertIsNone(settings["cpu_budget"])
        for load, expected in ((2.0, "7"), (40.0, "4")):
            with mock.patch.object(os, "getloadavg", return_value=(load, 0, 0)), \
                    mock.patch.object(os, "cpu_count", return_value=16):
                env = forever.child_env(forever.budget_environ(settings, {}), spec("mail"),
                                        state)
            self.assertEqual(env[cpu_budget.BUDGET_ENV], expected, load)
        # an operator's own per-child value stands
        env = forever.child_env({cpu_budget.BUDGET_ENV: "2"}, spec("mail"), state)
        self.assertEqual(env[cpu_budget.BUDGET_ENV], "2")
        # a budget that is not 1 to 256 whole cores is refused in the config
        for bad in (0, 257, "8", 2.5, True):
            with self.assertRaises(forever.IngestError):
                forever.ingest_settings({"mode": forever.MODE, "cpu_budget": bad})
        # the child: its pools at most its share, a malformed share reads as one core,
        # none given keeps its own sizing
        self.assertEqual(cpu_budget.cap(14, {cpu_budget.BUDGET_ENV: "4"}), 4)
        self.assertEqual(cpu_budget.cap(6, {cpu_budget.BUDGET_ENV: "4"}, part=0.5), 2)
        self.assertEqual(cpu_budget.cap(3, {cpu_budget.BUDGET_ENV: "16"}), 3)
        for bad in ("x", "0", "-3", "1e9"):
            self.assertEqual(cpu_budget.cap(14, {cpu_budget.BUDGET_ENV: bad}), 1, bad)
        self.assertEqual(cpu_budget.cap(14, {}), 14)


class ConcurrentIngestTest(unittest.TestCase):
    """Sources read concurrently (2026-10-27: one cycle of seven sources one after another
    took about 2.5 h before the projection showed anything). A cycle runs its children
    at once; the continuous mode (``serve``) records each source as it finishes (never
    building a projection), re-runs a fast source while a slow one reads, and stops on its
    stop file or low disk; one failing source never stops the others."""

    setUp, config, main, run_cycle, ran, ledger = (
        IngestModeTest.setUp, IngestModeTest.config, IngestModeTest.main,
        IngestModeTest.run_cycle, IngestModeTest.ran, IngestModeTest.ledger)

    def host(self, config: Path, table: dict, env: dict | None = None) -> host_module.Host:
        return host_module.Host(self.state, host_module.HostConfig.from_file(
            config, capture_sources=table), environ=self.env if env is None else env,
            disk_free=ROOMY)

    def until(self, condition, what: str, seconds: float = 120) -> None:
        deadline = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.05)

    def runs(self, name: str) -> int:
        path = self.state / f"fake-{name}" / "ran"
        return len(path.read_text().splitlines()) if path.is_file() else 0

    def starts(self, name: str) -> list[float]:
        """When each run of a fake source started (``time.monotonic``, one clock for
        every process of the host)."""
        path = self.state / f"fake-{name}" / "starts"
        return [float(t) for t in path.read_text().split()] if path.is_file() else []

    def test_a_cycle_runs_its_sources_at_once_within_max_parallel(self) -> None:
        table = fake_table(self.script, "one", "two")
        sections = {"one": {"mode": "barrier"}, "two": {"mode": "barrier"}}
        report, _ = self.run_cycle(self.config(sections), table)
        self.assertEqual({n: o["state"] for n, o in report["sources"].items()},
                         {"one": "captured", "two": "captured"})   # they overlapped
        self.assertEqual(json.loads((self.state / host_module.HOST_DIR /
                                     host_module.CHILDREN_FILE).read_text())["children"], [])
        # Not vacuous: one child at a time, each barrier source runs alone and fails.
        self.state = self.tmp / "serial"
        report, _ = self.run_cycle(self.config(sections, ingest={
            "mode": forever.MODE, "grace_seconds": 1, "max_parallel": 1}), table)
        self.assertEqual({n: o["capture"]["error_code"] for n, o in report["sources"].items()},
                         {"one": "ran_alone", "two": "ran_alone"})
        for bad in (0, forever.MAX_PARALLEL + 1, "2", True):
            code, _, err = self.main("run", "--state-dir", str(self.state), "--config", str(
                self.config(sections, ingest={"mode": forever.MODE, "max_parallel": bad})),
                table=table)
            self.assertEqual((code, json.loads(err)), (2, {"error": "host_config_invalid:ingest"}))

    def test_continuous_refreshes_each_finisher_and_keeps_each_cadence(self) -> None:
        from integrations import bc_read
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_bc_capture import BOOKS, ENV, Clock, FakeBc

        store = host_module.state_capture_paths(self.state)["bc"]
        store.parent.mkdir(parents=True)
        bc_read.capture_probe(ENV, sender=FakeBc({"purchaseReceipts": 3}), db=store,
                              page_size=2, passes=1, books=BOOKS, monotonic=Clock().monotonic,
                              retry=BoundedBackoff(sleep=lambda s: None),
                              entity_sets=("purchaseReceipts",))
        table = fake_table(self.script, "bc", "slow", "broken", projected={"bc": "bc"})
        # The slow source ends when the test releases it, never by its timeout: at load
        # 70-150 the fast source's three runs and their projections took past 60 s, the
        # slow one was cut and refreshed as failed (generation 0), batch 1114072152.
        config = self.config({"bc": {"min_interval_seconds": 1},
                              "slow": {"mode": "slow", "timeout_seconds": 1800},
                              "broken": {"mode": "fail", "min_interval_seconds": 1}})
        host, stop_file, reports = self.host(config, table), self.tmp / "stop", []
        summary: list[dict] = []
        runner = threading.Thread(target=lambda: summary.append(host.ingest_continuous(
            stop_file=stop_file, on_refresh=reports.append)))
        runner.start()
        self.addCleanup(lambda: (stop_file.touch(), runner.join(60)))
        # The slow source still reads while the fast one is recorded and runs again.
        self.until(lambda: any("bc" in r["sources"] for r in reports),
                   "no refresh after the first finisher")
        self.until(lambda: self.runs("bc") >= 3, "the fast source waited for the slow one")
        self.assertTrue((self.state / "fake-slow" / "started").is_file())
        self.assertNotIn("slow", {n for r in reports for n in r["sources"]})
        with self.assertRaises(host_module.HostError) as held:   # one writer meanwhile
            self.host(config, table).cycle()
        self.assertEqual(held.exception.code, "host_cycle_running")
        (self.state / "release").touch()
        self.until(lambda: any("slow" in r["sources"] for r in reports), "slow never refreshed")
        slow = [r["sources"]["slow"] for r in reports if "slow" in r["sources"]]
        self.assertEqual([o["state"] for o in slow], ["captured"], slow)
        # the failing source's backoff is seen over three of its runs (waited for, never
        # raced against the clock)
        self.until(lambda: sum("broken" in r["sources"] for r in reports) >= 3,
                   "the failing source stopped running", seconds=60)
        stop_file.touch()
        runner.join(60)
        self.assertFalse(runner.is_alive())
        (done,) = summary
        self.assertEqual((done["reason"], done["refreshes"], done["stopped_sources"]),
                         ("stop_file", len(reports), []))
        # The failing source kept failing on its own (backing off), never stopping the rest.
        broken = [r["sources"]["broken"] for r in reports if "broken" in r["sources"]]
        self.assertTrue(broken)
        self.assertEqual({(o["state"], o["capture"]["error_code"]) for o in broken},
                         {("failed", "SourceUnavailable")})
        # It backed off: the cadence (1 s) doubled per failure in a row, so each run starts
        # at least 2 s, then 4 s, 8 s ... after the one before, while the healthy source
        # kept its 1 s cadence (it ran 3 times while the slow one read, above). A bound
        # the scheduler guarantees at any host load, where comparing run counts raced
        # the wall clock (3 vs 3 at load 19).
        starts = self.starts("broken")
        self.assertGreaterEqual(len(starts), 3)
        for failures, (before, after) in enumerate(zip(starts, starts[1:]), 1):
            self.assertGreaterEqual(after - before, 2 ** failures, (failures, starts))
        # Each refresh is one ledger cycle; none built a generation, none overlapped another.
        self.assertEqual({(r["projection"]["state"], tuple(r["projection"]["reasons"]))
                          for r in reports}, {("skipped", ("builds_off",))})
        self.assertFalse(host_module.projection_path(self.state).exists())
        self.assertEqual(host_module.cycle_overlaps(host)["cycles_overlapping"], 0)
        rows = dict(self.ledger("SELECT source, generation FROM source_state"))
        self.assertEqual(rows["slow"], 1)
        self.assertEqual(rows["bc"], sum("bc" in r["sources"] for r in reports))
        self.assertEqual(rows["broken"], 0)

    def test_stop_file_disk_guard_and_exclusion_fail_closed(self) -> None:
        table = fake_table(self.script, "fast", "hung")
        config = self.config({"fast": {}, "hung": {"mode": "hang", "timeout_seconds": 60}})
        stop_file = self.tmp / "stop"
        # A stop file already there: nothing runs.
        stop_file.touch()
        done = self.host(config, table).ingest_continuous(stop_file=stop_file)
        self.assertEqual((done["reason"], done["refreshes"]), ("stop_file", 0))
        self.assertFalse(self.ran("fast") or self.ran("hung"))
        stop_file.unlink()
        # Less than the free space the guard keeps, or none measured: nothing runs.
        for free in (lambda path: host_module.MIN_FREE_BYTES - 1,
                     mock.Mock(side_effect=OSError("statvfs"))):
            host = self.host(config, table)
            host.disk_free = free
            done = host.ingest_continuous(stop_file=stop_file)
            self.assertEqual((done["reason"], done["refreshes"]), ("low_disk", 0))
            self.assertFalse(self.ran("fast"))
        # serve (what the unit runs) exits 0 on low disk, so systemd never restarts it: it
        # records why it ended, for the alarm and the ingest keeper; a start clears that.
        health = self.state / host_module.HOST_DIR / host_module.SERVE_HEALTH_FILE
        host_module.record_serve_start(self.state, now=host_module._utc())
        host = self.host(config, table)
        host.disk_free = lambda path: host_module.MIN_FREE_BYTES - 1
        host.serve(interval_seconds=1, stop_file=stop_file)
        stop = json.loads(health.read_text())["stop"]
        self.assertEqual((stop["reason"], stop["free_bytes"]),
                         ("low_disk", host_module.MIN_FREE_BYTES - 1))
        self.assertIn("started_at", json.loads(health.read_text()))
        host_module.record_serve_start(self.state, now=host_module._utc(), error="x")
        self.assertIn("stop", json.loads(health.read_text()))   # a refused start: still off
        host_module.record_serve_start(self.state, now=host_module._utc())
        self.assertNotIn("stop", json.loads(health.read_text()))
        # An unloaded people exclusion blocks each source before its child starts.
        done = self.host(config, table, env={"PATH": self.env["PATH"]}).ingest_continuous(
            stop_file=stop_file, max_refreshes=1)
        self.assertEqual(done["reason"], "max_refreshes")
        self.assertFalse(self.ran("fast") or self.ran("hung"))
        self.assertEqual(self.ledger("SELECT source, error FROM source_state ORDER BY source"),
                         [("fast", "excluded_people_oids_absent"),
                          ("hung", "excluded_people_oids_absent")])
        # A stop file mid-run stops the running child (its group killed) and never records
        # it as a failure; the source that finished is recorded.
        host, summary = self.host(config, table), []
        runner = threading.Thread(target=lambda: summary.append(
            host.ingest_continuous(stop_file=stop_file)))
        runner.start()
        pid_file = self.state / "fake-hung" / "pid"
        self.until(lambda: pid_file.is_file() and pid_file.read_text()
                   and self.ran("fast"), "the children never started")
        orphan = int(pid_file.read_text())
        self.addCleanup(lambda: alive(orphan) and os.kill(orphan, signal.SIGKILL))
        stop_file.touch()
        runner.join(30)
        self.assertFalse(runner.is_alive())
        self.assertFalse(alive(orphan))
        (done,) = summary
        self.assertEqual((done["reason"], done["stopped_sources"]), ("stop_file", ["hung"]))
        self.assertEqual(self.ledger("SELECT source, error FROM source_state ORDER BY source"),
                         [("fast", None), ("hung", "excluded_people_oids_absent")])
        self.assertEqual(json.loads((self.state / host_module.HOST_DIR /
                                     host_module.CHILDREN_FILE).read_text())["children"], [])

    def test_build_disk_guard_wal_checkpoint_and_sqlite_error_code(self) -> None:
        """Live incident 2026-10-27: an 11 GB mirror WAL filled the disk and every build
        failed as a bare ``OperationalError``. The host skips a build that lacks room
        (``disk_low``, the previous generation served), names the SQLite cause in the
        reason and checkpoints every store after the build, reporting WAL sizes."""
        from integrations import bc_read
        from integrations.capture_store import wal_bytes
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_bc_capture import BOOKS, ENV, Clock, FakeBc

        store = host_module.state_capture_paths(self.state)["bc"]
        store.parent.mkdir(parents=True)
        bc_read.capture_probe(ENV, sender=FakeBc({"purchaseReceipts": 3}), db=store,
                              page_size=2, passes=1, books=BOOKS, monotonic=Clock().monotonic,
                              retry=BoundedBackoff(sleep=lambda s: None),
                              entity_sets=("purchaseReceipts",))
        table = fake_table(self.script, "bc", projected={"bc": "bc"})
        config = self.config({"bc": {}})
        host = self.host(config, table)
        # A capture writer left frames in the store's WAL; another connection is open.
        held = sqlite3.connect(store)
        self.addCleanup(held.close)
        with held:
            held.execute("CREATE TABLE IF NOT EXISTS wal_probe (x BLOB)")
            held.executemany("INSERT INTO wal_probe VALUES (randomblob(4096))",
                             [()] * 30)
        self.assertGreater(wal_bytes(store), 0)
        first = host.cycle()
        self.assertEqual(first["projection"]["state"], "built", first)
        wal = first["wal"]
        self.assertEqual((wal["by_store"]["bc"]["state"], wal["by_store"]["bc"]["wal_bytes"]),
                         ("truncated", 0), wal)
        self.assertGreater(wal["by_store"]["bc"]["wal_bytes_before"], 0)
        self.assertEqual(wal["stores"], wal["truncated"] + wal["busy"] + wal["failed"])
        self.assertEqual(wal_bytes(store), 0)
        self.assertNotIn(str(self.state), json.dumps(wal))   # names, never paths
        served = host_module.projection_path(self.state)
        size = served.stat().st_size
        no_clone = mock.patch("ontology.project_incremental.can_clone", return_value=False)
        no_clone.start()
        self.addCleanup(no_clone.stop)
        # The estimate, not a flat 2x: an incremental build on a file system that cannot
        # clone needs a copy of the served generation plus the margin. Below that, or
        # unmeasured: no build, the previous generation stays served, the cycle does not fail.
        self.assertEqual(host.build_disk()["needed_bytes"],
                         size + host_module.BUILD_DISK_MARGIN_BYTES)
        for free in (lambda path: size + host_module.BUILD_DISK_MARGIN_BYTES - 1,
                     mock.Mock(side_effect=OSError("statvfs"))):
            host.disk_free = free
            with mock.patch.object(host_module, "build_projection") as build:
                skipped = host.cycle()
            build.assert_not_called()
            self.assertEqual((skipped["state"], skipped["projection"]["state"],
                              skipped["projection"]["reasons"],
                              skipped["projection"]["generation"]),
                             ("complete", "skipped", ["disk_low"], first["cycle"]), skipped)
            self.assertTrue(served.samefile(host_module.projection_path(self.state)))
        # A file system that clones lays the seed down for free: the margin is the need.
        with mock.patch("ontology.project_incremental.can_clone", return_value=True):
            self.assertEqual(host.build_disk()["needed_bytes"],
                             host_module.BUILD_DISK_MARGIN_BYTES)
        # Free space that falls below the margin while it builds stops the build: skipped,
        # nothing of it left, the previous generation served.
        falling = iter([size + host_module.BUILD_DISK_MARGIN_BYTES] * 2)
        host.disk_free = lambda path: next(falling, host_module.BUILD_DISK_MARGIN_BYTES - 1)
        stopped = host.cycle()
        self.assertEqual((stopped["projection"]["state"], stopped["projection"]["reasons"],
                          stopped["projection"]["generation"]),
                         ("skipped", ["disk_low"], first["cycle"]), stopped)
        self.assertTrue(served.samefile(host_module.projection_path(self.state)))
        generations = list((served.parent / "generations").iterdir())
        self.assertTrue(generations)
        self.assertTrue(all((g / "units.json").is_file() for g in generations), generations)
        # Room again: the SQLite cause is in the reason, never only the class name.
        host.disk_free = lambda path: size + host_module.BUILD_DISK_MARGIN_BYTES
        full = sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(host_module, "build_projection", side_effect=full):
            failed = host.cycle()
        self.assertEqual((failed["projection"]["state"], failed["projection"]["reasons"],
                          failed["projection"]["generation"]),
                         ("failed", ["OperationalError:disk_full"], first["cycle"]))

    def test_a_build_where_most_units_changed_goes_cold_beside_the_served_generation(
            self) -> None:
        """Live 2026-10-27: serve skipped a refresh (``disk_low``) because the incremental
        estimate (a copy of the served store plus twice an 87% changed share) was twice
        what a cold build needs, though the cold build fitted. The host's guard now
        builds cold where only that fits, reports both estimates either way, and skips
        only when neither fits (the served generation stays served)."""
        from ontology.schema import load_schema
        from ontology.tests import synthetic_captures

        schema = load_schema()
        relations = sorted({b["relation"] for t in schema.object_types.values()
                            for b in t.get("bindings", [])})
        most = [r for r in relations if r != "messages"]

        def sources(changed=()):
            return synthetic_captures.sources(schema, 8, changed)
        host = self.host(self.config({"bc": {}}),
                         fake_table(self.script, "bc", projected={"bc": "bc"}))
        # Synthetic captures carry no document evidence the post-projection steps read.
        for patcher in (mock.patch("ontology.project_incremental.can_clone", return_value=False),
                        mock.patch("operating.build_steps.post_project", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        first = host_module.build_projection(self.state, sources(), space=host._space_guard)
        served = host_module.projection_path(self.state)
        inode = served.stat().st_ino
        margin = host_module.BUILD_DISK_MARGIN_BYTES
        host.disk_free = lambda path: 0
        with self.assertRaises(host_module.DiskLow) as low:
            host_module.build_projection(self.state, sources(most), space=host._space_guard)
        estimate = low.exception.disk["estimate"]
        cold, partial = (estimate["estimates"][m]["needed_bytes"]
                         for m in ("cold", "incremental"))
        self.assertEqual((estimate["mode"], estimate["preferred"]), ("cold", "cold"))
        self.assertGreater(estimate["estimates"]["incremental"]["changed_share"], 0.5)
        self.assertGreater(partial, cold)
        for free, built in ((cold + margin - 1, None), (cold + margin, "cold")):
            host.disk_free = lambda path, free=free: free
            if built is None:
                with self.assertRaises(host_module.DiskLow) as low:
                    host_module.build_projection(self.state, sources(most),
                                                 space=host._space_guard)
                self.assertEqual(sorted(low.exception.disk["estimate"]["estimates"]),
                                 ["cold", "incremental"])
                self.assertEqual(served.stat().st_ino, inode)   # still served
                continue
            report = host_module.build_projection(self.state, sources(most),
                                                  space=host._space_guard)
            need = report["build"]["need"]
            self.assertEqual((report["build"]["mode"], need["mode"],
                              report["build"]["units_rebuilt"]),
                             ("cold", "cold", report["build"]["units"]))
            self.assertEqual((need["estimates"]["cold"]["needed_bytes"],
                              need["estimates"]["incremental"]["needed_bytes"]),
                             (cold, partial))
            self.assertNotEqual(report["content_digest"], first["content_digest"])
            self.assertNotEqual(served.stat().st_ino, inode)

    def test_a_build_known_cold_that_cannot_fit_skips_before_fingerprinting(self) -> None:
        """Live 2026-11-06: a cold build fingerprinted for 4.4 hours, then skipped
        (``disk_low``): the check before it used the least any mode writes, and the cold
        estimate came only once the changed units were known. A build known to be cold (the
        served generation was built under another schema) is checked against the cold
        estimate first: skipped at once, nothing fingerprinted or copied, the wait recorded;
        the next cycles only measure free space until it is there. A schema the base can be
        rewritten under unit by unit, or most units changed where an incremental build
        needs less, checks and waits for the smallest mode that would run."""
        from integrations import bc_read
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_bc_capture import BOOKS, ENV, Clock, FakeBc
        from ontology import project_incremental as inc

        store = host_module.state_capture_paths(self.state)["bc"]
        store.parent.mkdir(parents=True)
        bc_read.capture_probe(ENV, sender=FakeBc({"purchaseReceipts": 3}), db=store,
                              page_size=2, passes=1, books=BOOKS, monotonic=Clock().monotonic,
                              retry=BoundedBackoff(sleep=lambda s: None),
                              entity_sets=("purchaseReceipts",))
        host = self.host(self.config({"bc": {}}),
                         fake_table(self.script, "bc", projected={"bc": "bc"}))
        no_clone = mock.patch("ontology.project_incremental.can_clone", return_value=False)
        no_clone.start()
        self.addCleanup(no_clone.stop)
        first = host.cycle()
        self.assertEqual(first["projection"]["state"], "built", first)
        state = self.state / host_module.PROJECTION_DIR
        cold = inc.space_need(state)["needed_bytes"]
        least = inc.space_need(state, dirty=())["needed_bytes"]
        margin = host_module.BUILD_DISK_MARGIN_BYTES
        self.assertGreater(cold, least)
        # Room for the least an incremental build writes, not for a cold one.
        host.disk_free = lambda path: cold + margin - 1
        other = mock.patch("ontology.project_incremental._schema_digest",
                           return_value="sha256:" + "0" * 64)
        # another schema the base can be rewritten under: an incremental build can run,
        # so the check is the least it writes
        with other, mock.patch("ontology.project_incremental._rewritable", return_value=True):
            self.assertEqual(host.build_plan(first["cycle"])["needed_bytes"], least + margin)
        # most units changed (measured) where an incremental build needs less than cold:
        # that one is checked
        partial = inc.space_need(state, share=0.6)["needed_bytes"]
        need = inc.planned_need(state, changed_share=0.6)
        self.assertEqual((need["needed_bytes"], need["predicted"]),
                         (min(partial, cold), "changed_share"))
        self.assertEqual(host_module.smallest_need(
            {"needed_bytes": cold + margin + 5, "estimate": {
                "needed_bytes": cold, "estimates": {"cold": {"needed_bytes": cold},
                                                    "incremental": {"needed_bytes": 7}}}}),
            7 + margin + 5)
        cannot = mock.patch("ontology.project_incremental._rewritable", return_value=False)
        with other, cannot, mock.patch.object(host_module, "build_projection") as build:
            skipped = host.cycle()
        build.assert_not_called()   # nothing fingerprinted
        projection = skipped["projection"]
        self.assertEqual((projection["state"], projection["reasons"],
                          projection["generation"]),
                         ("skipped", ["disk_low"], first["cycle"]), projection)
        self.assertEqual((projection["disk"]["estimate"]["mode"],
                          projection["disk"]["estimate"]["predicted"]),
                         ("cold", "schema_changed"))
        self.assertEqual(projection["waits_for_bytes"], cold + margin)
        self.assertEqual(host.disk_wait, cold + margin)
        wait = host_module.build_wait(self.state)
        self.assertEqual((wait["needed_bytes"], wait["free_bytes"], wait["mode"],
                          wait["predicted"]), (cold + margin, cold + margin - 1, "cold",
                                               "schema_changed"))
        self.assertEqual(host.status()["build_wait"]["needed_bytes"], cold + margin)
        # The Postgres load keeps that room free (the next cold build's estimate).
        self.assertGreaterEqual(host_module.build_reserve(self.state)["reserve_bytes"],
                                cold + margin)
        # Waiting: the next cycle records its sources and only measures free space.
        with mock.patch.object(host_module.Host, "build_plan") as plan:
            waited = host.cycle()
        plan.assert_not_called()
        self.assertEqual((waited["projection"]["reasons"],
                          waited["projection"]["waits_for_bytes"]),
                         (["disk_low"], cold + margin))
        self.assertEqual(host_module.build_wait(self.state)["mode"], "cold")
        # Room again and the served schema: the build goes on, and nothing waits after it.
        host.disk_free = lambda path: cold + margin
        built = host.cycle()
        self.assertEqual(built["projection"]["state"], "built", built)
        self.assertIsNone(host.disk_wait)
        self.assertIsNone(host_module.build_wait(self.state))

    def test_continuous_sources_at_cardinality(self) -> None:
        """1, 10 and 60 sources, 7 at a time: each run once and refreshed exactly once."""
        for n in (1, 10, 60):
            with self.subTest(sources=n):
                state = self.tmp / f"cardinality-{n}"
                names = [f"s{i:02d}" for i in range(n)]
                table = fake_table(self.script, *names)
                refreshed: list[str] = []
                started = time.monotonic()
                done = forever.Continuous(
                    table, {name: {"min_interval_seconds": 7200} for name in names}, state,
                    self.env, grace=1, parallel=forever.max_parallel({}, n),
                    refresh=lambda batch, reason: refreshed.extend(
                        name for name, o in batch.items() if o["state"] == "captured"),
                    should_stop=lambda: "done" if len(refreshed) >= n else None).run()
                self.assertLess(time.monotonic() - started, 10 + n)
                self.assertEqual(sorted(refreshed), names)
                self.assertEqual((done["reason"], done["runs"]), ("done", dict.fromkeys(names, 1)))


class NoBuildTest(unittest.TestCase):
    """The continuous ingest never builds a projection generation (docs/no-builds.md): the
    retired settings that once built (``builds`` ``on``, ``build_inputs`` ``snapshot``) or
    anything else in them build nothing and never stop capture (a refused key held the
    ingest in a restart loop for 23 h, 2026-11-07); no capture waits for a refresh, and the
    stores' WAL is still cut back between refreshes."""

    setUp, config = IngestModeTest.setUp, IngestModeTest.config
    host, until = ConcurrentIngestTest.host, ConcurrentIngestTest.until

    def test_every_retired_setting_records_captures_and_never_builds(self) -> None:
        table = fake_table(self.script, "fast")
        for extra, warned in (({}, []), ({"builds": "off"}, []),
                              ({"build_inputs": "live"}, []),
                              ({"builds": "on"}, ["ingest.builds_retired"]),
                              ({"builds": "sometimes"}, ["ingest.builds_retired"]),
                              ({"build_inputs": "snapshot"}, ["ingest.build_inputs_retired"]),
                              ({"builds": "on", "build_inputs": "snapshot"},
                               ["ingest.build_inputs_retired", "ingest.builds_retired"])):
            with self.subTest(**{k: str(v) for k, v in extra.items()}):
                ingest = {"mode": forever.MODE, "grace_seconds": 1, **extra}
                warnings: list[str] = []
                settings = forever.ingest_settings(ingest, warnings)
                self.assertEqual(warnings, warned)
                self.assertNotIn("builds", settings)
                self.state = self.tmp / f"state-{len(list(self.tmp.iterdir()))}"
                host = self.host(self.config({"fast": {"min_interval_seconds": 1}},
                                             ingest=ingest), table)
                reports: list[dict] = []
                with mock.patch.object(host_module, "build_projection",
                                       side_effect=AssertionError("built")), \
                        mock.patch.object(host_module.Host, "_project",
                                          side_effect=AssertionError("built")):
                    done = host.ingest_continuous(stop_file=self.tmp / "stop",
                                                  max_refreshes=2, on_refresh=reports.append)
                self.assertEqual(done["refreshes"], 2)
                self.assertEqual({(r["projection"]["state"], tuple(r["projection"]["reasons"]))
                                  for r in reports}, {("skipped", ("builds_off",))})
                self.assertEqual({n for r in reports for n in r["sources"]}, {"fast"})
                self.assertFalse(any("build_exclusion" in r for r in reports))
        # A known key with a bad value is still refused.
        with self.assertRaises(forever.IngestError):
            forever.ingest_settings({"mode": forever.MODE, "grace_seconds": 0})

    def test_a_capture_of_a_served_store_runs_on_while_others_are_refreshed(self) -> None:
        """No build exclusion: the Portal mirror capture (it writes a store every build
        once read) keeps reading while the sources that finished meanwhile are refreshed,
        and is never held back; each refresh's report says nothing of a build wait."""
        table = fake_table(self.script, "mirror", "fast", writes={
            "mirror": lambda state: (forever.portal_mirror(state),)})
        sections = {"mirror": {"mode": "busy", "min_interval_seconds": 0},
                    "fast": {"min_interval_seconds": 0.2}}
        overlapped: list[bool] = []

        def refresh(batch, reason):
            overlapped.append((self.state / "fake-mirror" / "running").exists())
        done = forever.Continuous(
            table, sections, self.state, self.env, grace=1, parallel=2, refresh=refresh,
            should_stop=lambda: "done" if len(overlapped) >= 6 else None).run()
        self.assertEqual(done["reason"], "done")
        self.assertTrue(any(overlapped), overlapped)
        self.assertGreater(done["runs"]["mirror"], 1)   # it ran again between refreshes

    def test_the_host_cuts_back_each_stores_wal_between_refreshes(self) -> None:
        from integrations.capture_store import wal_bytes

        mirror = forever.portal_mirror(self.state)
        mirror.parent.mkdir(parents=True)
        writer = sqlite3.connect(mirror)   # a capture left WAL frames; held open
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        with writer:
            writer.execute("CREATE TABLE t (x BLOB)")
            writer.executemany("INSERT INTO t VALUES (randomblob(4096))", [()] * 30)
        self.assertGreater(wal_bytes(mirror), 0)
        table = fake_table(self.script, "fast")
        config = self.config({"fast": {"min_interval_seconds": 7200}})
        reports: list[dict] = []
        self.host(config, table).ingest_continuous(
            stop_file=self.tmp / "stop", max_refreshes=1, on_refresh=reports.append)
        (report,) = reports
        self.assertGreater(report["wal"]["wal_bytes_before"], 0)
        self.assertEqual(wal_bytes(mirror), 0)
        self.assertNotIn(str(self.state), json.dumps(report["wal"]))   # names, never paths


class LiveCycleRepairTest(unittest.TestCase):
    """Reproduced 2026-10-26 on the shared state (ingest mode, every cycle since 17:56Z):
    BC and MC failed with a bare ``CaptureError`` and mail and SharePoint with
    ``no_units_reported``. The BC child bound the host's BC store to its own service
    audience (``binding_mismatch``); the MC child's reader check refused a scope past one
    resolve batch (``invalid_handles``); the mail and SharePoint children ran as their CLI
    default principal, which the host capture audience does not grant, so every mailbox
    and library was ``not_visible`` before any read."""

    # the host harness of IngestModeTest, without re-running its tests
    setUp, config, main, run_cycle = (IngestModeTest.setUp, IngestModeTest.config,
                                      IngestModeTest.main, IngestModeTest.run_cycle)

    def test_a_child_refusal_reaches_the_ledger_with_its_code(self) -> None:
        table = fake_table(self.script, "mc", "bc")
        config = self.config({"mc": {"mode": "stdout_refusal"},
                              "bc": {"mode": "stderr_refusal"}})
        report, text = self.run_cycle(config, table)
        codes = {n: (o["state"], o["capture"]["error_code"])
                 for n, o in report["sources"].items()}
        self.assertEqual(codes, {"mc": ("failed", "invalid_handles"),
                                 "bc": ("failed", "binding_mismatch")})
        self.assertNotIn("tenant's audience", text)   # the message never

    def test_children_run_as_the_host_capture_audience_and_principal(self) -> None:
        options = {"live_audience": "/a.json", "principal": "svc:workbench-host"}
        for name in ("mail", "sharepoint", "bc"):
            argv = forever.SOURCES[name].argv(self.state, options)
            self.assertEqual(argv[argv.index("--principal") + 1], "svc:workbench-host", name)
            self.assertEqual(argv[argv.index("--live-audience") + 1], "/a.json", name)
        # an own-audience source keeps its CLI principal
        self.assertNotIn("principal", forever.SOURCES["teams"].fields)
        self.assertNotIn("--principal", forever.SOURCES["teams"].argv(
            self.state, {"live_audience": "/a.json"}))
        # BC without the host's audience is refused at config time, never run unbound
        with self.assertRaises(forever.IngestError):
            forever.section_options(forever.SOURCES["bc"], {}, self.state, {})

    def test_the_list_capture_gets_its_budget_and_its_cli_takes_it(self) -> None:
        # 11-16: with only a hard timeout, a slow pass was killed at 2,100 s mid-list and
        # recorded failed; a budget lets it stop itself between pages, resumable
        import argparse
        from integrations import capture_sharepoint_lists as csl
        spec = forever.SOURCES["sharepoint_lists"]
        audience = {"live_audience": str(ROOT / "examples" / "ingest_all.capture-audience.json")}
        options = forever.section_options(spec, {"budget_seconds": 1500}, self.state,
                                          audience)
        argv = spec.argv(self.state, options)
        self.assertEqual(argv[argv.index("--budget-seconds") + 1], "1500")
        parser = argparse.ArgumentParser()
        csl.add_cli_arguments(parser)
        parsed = parser.parse_args(argv[argv.index("sharepoint-list-capture") + 1:])
        self.assertEqual(parsed.budget_seconds, 1500.0)
        self.assertNotIn("--budget-seconds", spec.argv(self.state, forever.section_options(
            spec, {}, self.state, audience)))
        with self.assertRaises(forever.IngestError):
            forever.section_options(spec, {"budget_seconds": "soon"}, self.state,
                                    audience)

    def test_mail_backlog_passes_are_their_own_child_never_the_mail_capture(self) -> None:
        # 2026-11-13: the backlog passes ran inside the mail child, so a 2 h 22 min
        # deferred pass held fresh mail back. They are the ``mail_backlog`` source now:
        # the mail child never gets a pass flag, whatever its section says.
        spec, backlog = forever.SOURCES["mail"], forever.SOURCES["mail_backlog"]
        audience = str(ROOT / "examples" / "ingest_all.capture-audience.json")
        warnings: list[str] = []
        legacy = forever.section_options(spec, {
            "budget_seconds": 600, "deferred_attachments_seconds": 1800,
            "image_ocr_seconds": 300}, self.state, {"live_audience": audience}, warnings,
            "mail")
        argv = spec.argv(self.state, legacy)
        self.assertFalse([a for a in argv if "deferred" in a or "image-ocr" in a
                          or "apply-fetched" in a], argv)
        self.assertEqual(sorted(warnings), ["mail.deferred_attachments_seconds",
                                            "mail.image_ocr_seconds"])
        options = forever.section_options(backlog, {
            "budget_seconds": 600, "deferred_attachments_seconds": 1800,
            "deferred_workers": 16, "deferred_extract_workers": 4,
            "deferred_min_free_gb": 30, "image_ocr_seconds": 300,
            "image_ocr_workers": 8}, self.state, {"live_audience": audience})
        argv = backlog.argv(self.state, options)
        self.assertEqual(argv[1:4], ["-m", "integrations.capture_mail", "backlog"])
        self.assertIn("--host-child", argv)
        for flag, value in (("--deferred-attachments-seconds", "1800"),
                            ("--deferred-workers", "16"), ("--deferred-extract-workers", "4"),
                            ("--deferred-min-free-gb", "30"), ("--budget-seconds", "600"),
                            ("--image-ocr-seconds", "300"), ("--image-ocr-workers", "8")):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertEqual((backlog.yields_to, backlog.writes(self.state)),
                         (("mail",), spec.writes(self.state)))
        # A bad value is refused when the config loads, never by every child; the
        # backlog's budget is required (its hard deadline).
        for bad in ({"deferred_workers": 17}, {"image_ocr_workers": 0},
                    {"deferred_min_free_gb": -1}, {"deferred_min_free_gb": True},
                    {"deferred_attachments_seconds": 0}, {"deferred_workers": True},
                    {"image_ocr_seconds": "60"}):
            with self.assertRaises(forever.IngestError, msg=bad):
                forever.section_options(backlog, {"budget_seconds": 60, **bad}, self.state,
                                        {"live_audience": audience})
        with self.assertRaises(forever.IngestError):
            forever.section_options(backlog, {"image_ocr_seconds": 60}, self.state,
                                    {"live_audience": audience})
        # A config written before the split keeps its passes, moved off the capture.
        moved: list[str] = []
        document = forever.split_mail_backlog({"mail": {
            "scope": "s.json", "budget_seconds": 900, "min_interval_seconds": 60,
            "image_ocr_seconds": 120, "apply_fetched_seconds": 240,
            "apply_fetched_dir": "/x"}}, forever.SOURCES, moved)
        self.assertEqual(document["mail"], {"scope": "s.json", "budget_seconds": 900,
                                            "min_interval_seconds": 60})
        self.assertEqual(document["mail_backlog"], {
            "image_ocr_seconds": 120, "apply_fetched_seconds": 240, "apply_fetched_dir": "/x",
            "budget_seconds": 360, "min_interval_seconds": 60})
        self.assertEqual(moved, ["mail.backlog_keys_moved"])
        given = {"mail": {"image_ocr_seconds": 1}, "mail_backlog": {"budget_seconds": 9}}
        self.assertIs(forever.split_mail_backlog(given, forever.SOURCES, []), given)

    def test_the_backlog_report_is_one_unit_per_pass(self) -> None:
        judge = lambda report: forever._judge(forever._base(forever.SOURCES["mail_backlog"]),
                                              forever.summarize_backlog(report))
        done = judge({"ok": True, "passes": {"image_ocr": {"read": 3},
                                             "apply_fetched": {"applied": 2}}})
        self.assertEqual((done["status"], done["units"]), ("complete", 2))
        cut = judge({"ok": True, "passes": {"image_ocr": {"budget_spent": 1}}})
        self.assertEqual((cut["status"], cut["reasons"]), ("partial", ["budget_spent"]))
        drained = judge({"ok": True, "drained": True,
                         "passes": {"deferred_attachments": {"budget_spent": 1,
                                                             "abandoned": 2}}})
        self.assertEqual(drained["reasons"], ["drained"])
        refused = judge({"ok": True, "passes": {"image_ocr": {
            "error": "people_exclusion_unavailable"}}})
        self.assertEqual((refused["status"], refused["blocked"][0]["later_ask"]),
                         ("blocked", "excluded-people-oids"))

    def test_a_bc_hand_list_missing_capture_sets_is_a_config_warning(self) -> None:
        """CT-D-04: the live config listed BC sets by hand and three capture sets
        (projects, plxCustomerShipTos, contactsInformation) never ran, silently. Each set
        the CLI captures by default that the list leaves out is a config warning (the Home
        ``config_keys_ignored`` breach); no list, or a full one, warns of nothing."""
        from integrations.bc_read import CAPTURE_ENTITY_SETS, PARTY_CONTACT_SET

        spec = forever.SOURCES["bc"]
        audience = {"live_audience": str(ROOT / "examples" / "ingest_all.capture-audience.json")}
        left_out = ["projects", "plxCustomerShipTos", PARTY_CONTACT_SET]
        listed = [s for s in CAPTURE_ENTITY_SETS if s not in left_out]
        warnings: list[str] = []
        options = forever.section_options(spec, {"entity_sets": listed}, self.state,
                                          audience, warnings, "bc")
        self.assertEqual(warnings, [f"bc.entity_sets:unlisted:{s}" for s in
                                    ("projects", "plxCustomerShipTos", PARTY_CONTACT_SET)])
        # the list is still passed as given: a warning, never a silent widening
        argv = spec.argv(self.state, options)
        self.assertNotIn("projects", argv)
        for section in ({}, {"entity_sets": list(CAPTURE_ENTITY_SETS)},
                        {"entity_sets": list(reversed(CAPTURE_ENTITY_SETS))}):
            warnings = []
            forever.section_options(spec, section, self.state, audience, warnings, "bc")
            self.assertEqual(warnings, [], section)
        # an empty list is refused, never a capture of nothing
        with self.assertRaises(forever.IngestError):
            forever.section_options(spec, {"entity_sets": []}, self.state, audience, [], "bc")
        # the committed all-sources config lists every set: no warning from it
        config = host_module.HostConfig.from_file(ROOT / "examples" / "ingest_all_sources.json",
                                                  capture_sources=forever.SOURCES)
        self.assertEqual([w for w in config.config_warnings if ":unlisted:" in w], [])

    def test_named_originals_is_a_flag_the_sharepoint_child_gets_only_when_true(self) -> None:
        spec = forever.SOURCES["sharepoint"]
        base = {"live_audience": "/a.json"}
        self.assertNotIn("--named-originals", spec.argv(self.state, base))
        options = forever.section_options(spec, {"named_originals": True}, self.state,
                                          {"live_audience": str(ROOT / "examples" /
                                                                "ingest_all.capture-audience.json")})
        self.assertIn("--named-originals", spec.argv(self.state, options))
        self.assertNotIn("--named-originals", spec.argv(self.state, {**base,
                                                                     "named_originals": False}))
        for value in ("true", 1, None, ["x"]):   # only a JSON boolean
            with self.assertRaises(forever.IngestError):
                forever.section_options(spec, {"named_originals": value}, self.state, {})

    def test_the_host_audience_grants_the_host_principal_and_no_cli_default(self) -> None:
        from integrations import capture_mail, capture_sharepoint
        from integrations.live_source_guard import LiveSourceAudience
        from ontology.core import Actor

        audience = LiveSourceAudience.from_file(
            ROOT / "examples" / "ingest_all.capture-audience.json")
        resources = ("mail:buyer@example.com", "sharepoint:b!drive:/folder",
                     "bc:production:c0000000-0000-4000-8000-000000000001:purchaseorders")
        for principal, granted in (("svc:workbench-host", True),
                                   (capture_mail.CAPTURE_PRINCIPAL, False),
                                   (capture_sharepoint.CAPTURE_PRINCIPAL, False)):
            actor = Actor(audience.tenant, frozenset({principal}), "restricted")
            self.assertEqual([audience.allows(actor, r) for r in resources],
                             [granted] * len(resources), principal)

    def test_refused_mailboxes_and_libraries_are_units_with_their_reason(self) -> None:
        mail = forever.summarize_mail({"runs": [{"mailboxes": [
            {"index": 0, "status": "not_visible", "complete": False, "folders": []},
            {"index": 1, "status": "people_excluded", "complete": False, "folders": []},
            {"index": 2, "status": "complete", "complete": True,
             "folders": [{"complete": True, "resumed_from": "delta_url"}]}]}]})
        self.assertEqual([(u["complete"], u["reason"]) for u in mail["units"]],
                         [(True, None), (False, "not_visible")])
        judged = forever._judge(forever._base(forever.SOURCES["mail"]), forever.summarize_mail(
            {"runs": [{"mailboxes": [{"status": "not_visible", "complete": False,
                                      "folders": []}] * 60}]}))
        self.assertEqual((judged["status"], judged["units"], judged["reasons"]),
                         ("partial", 60, ["not_visible"]))
        sharepoint = forever._judge(forever._base(forever.SOURCES["sharepoint"]),
                                    forever.summarize_sharepoint({"runs": [{"targets": [
            {"status": "library_unclassified:not_visible", "complete": False, "roots": []}]}]}))
        self.assertEqual((sharepoint["status"], sharepoint["reasons"]),
                         ("partial", ["library_unclassified:not_visible"]))

    def test_mc_failures_name_their_error_and_unreadable_chats_are_no_gap(self) -> None:
        # Live 2026-10-27: MC refused the key (401 invalid_api_key) and the host said only
        # "source_error"; the reason now names the code. A chat hosted in another tenant
        # (not_readable) is counted beside the excluded ones, never a failed unit.
        mc = forever._judge(forever._base(forever.SOURCES["mc"]), forever.summarize_mc(
            {"passes": [{"units": [
                {"collection": c, "state": "in_progress", "reason": "source_error",
                 "error": "auth_refused"} for c in ("tasks", "buckets", "knowledge")]}]}))
        self.assertEqual((mc["status"], mc["reasons"]),
                         ("partial", ["source_error:auth_refused"]))
        teams = forever.summarize_teams({"runs": [{"conversations": [
            {"status": "complete", "reasons": [], "resumed_from": None},
            {"status": "not_readable", "reasons": ["not_readable:other_tenant"]},
            {"status": "people_excluded", "reasons": []}]}]})
        self.assertEqual(([u["complete"] for u in teams["units"]], teams["excluded"]),
                         ([True], 2))
        judged = forever._judge(forever._base(forever.SOURCES["teams"]), teams)
        self.assertEqual((judged["status"], judged["reasons"]), ("complete", []))

    def test_a_teams_chat_listing_cut_at_its_cap_makes_the_source_partial_and_named(
            self) -> None:
        """Every conversation read whole, but a member's chat listing (discovery's or the
        census's) stopped at the page cap: chats went undiscovered, so Teams is partial
        with ``cap:chat_listing_page_cap`` in the host's reasons, never complete."""
        whole = {"status": "complete", "reasons": [], "resumed_from": None}
        for discovery, census in (({"reasons": ["cap:chat_listing_page_cap"],
                                    "chat_listings_capped": 2}, None),
                                   ({"reasons": [], "chat_listings_capped": 0},
                                    {"reasons": ["cap:chat_listing_page_cap"],
                                     "chat_listings_capped": 1})):
            with self.subTest(census=census is not None):
                report = {"runs": [{"conversations": [whole], "discovery": discovery}],
                          **({"census": census} if census else {})}
                judged = forever._judge(forever._base(forever.SOURCES["teams"]),
                                        forever.summarize_teams(report))
                self.assertEqual((judged["status"], judged["reasons"]),
                                 ("partial", ["cap:chat_listing_page_cap"]))
        judged = forever._judge(forever._base(forever.SOURCES["teams"]), forever.summarize_teams(
            {"runs": [{"conversations": [whole], "discovery": {"chat_listings_capped": 0}}],
             "census": {"chat_listings_capped": 0}}))
        self.assertEqual((judged["status"], judged["units"]), ("complete", 1))

    def test_meetings_reads_with_the_admin_application_and_counts_withheld_as_excluded(
            self) -> None:
        spec = forever.SOURCES["meetings"]
        env = {"AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
               "PLX_MEETINGS_GRAPH_CREDENTIALS": "AZURE", "BC_CLIENT_SECRET": "b",
               "PLX_MEETINGS_TRANSCRIPTS_GRANTED": "1", "MC_MCP_API_KEY": "m", "PATH": "p"}
        child = forever.child_env(env, spec, self.state)
        # the transcript grant reaches the child (it was dropped: 0 lookups ever)
        self.assertEqual(set(child) - {"PLX_WORKBENCH_STATE_DIR", "PLX_INGEST_DRAIN"},
                         {"AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                          "PLX_MEETINGS_GRAPH_CREDENTIALS", "PLX_MEETINGS_TRANSCRIPTS_GRANTED",
                          "PATH"})
        summary = forever.summarize_meetings({"runs": [{
            "counts": {"mailboxes_excluded": 1, "events_withheld": 4, "tasks_withheld": 2},
            "mailboxes": [{"status": "complete", "reasons": [], "resumed_from": "cursor"}],
            "plans": [{"status": "incomplete", "reasons": ["cap:task_reads"]}],
            "transcripts": {"status": "blocked",
                            "reasons": ["blocked:missing_permission:OnlineMeetings.Read.All"]}
        }], "blocked_upstream": [{"part": "meetings.online_meetings", "http_status": 403,
                                  "error_code": "missing_permission",
                                  "source": "microsoft-graph",
                                  "cause": "host_misconfiguration"}]})
        self.assertEqual(([u["complete"] for u in summary["units"]], summary["excluded"]),
                         ([True, False, False], 7))
        self.assertEqual(summary["blocked"][0]["part"], "meetings.online_meetings")

    def test_committed_all_sources_config_covers_every_source(self) -> None:
        from integrations.bc_read import CAPTURE_ENTITY_SETS

        path = ROOT / "examples" / "ingest_all_sources.json"
        config = host_module.HostConfig.from_file(path, capture_sources=forever.SOURCES)
        self.assertEqual(set(config.sources), set(forever.SOURCES))
        audience = str((path.parent / "ingest_all.capture-audience.json").resolve())
        for name in ("mail", "sharepoint", "bc"):
            self.assertEqual(config.sources[name]["principal"], "svc:workbench-host", name)
            self.assertEqual(config.sources[name]["live_audience"], audience, name)
        # every supported BC set; purchaseOrders brings Page 50 Your_Reference with it
        self.assertEqual(config.sources["bc"]["entity_sets"], list(CAPTURE_ENTITY_SETS))
        # Portal: no hand list; the mirror syncs every relation the live catalog serves.
        self.assertNotIn("relations", config.sources["portal"])
        with tempfile.TemporaryDirectory() as state:
            argv = forever.SOURCES["portal"].argv(Path(state), config.sources["portal"])
        self.assertIn("sync", argv)
        self.assertNotIn("--relation", argv)
        # the summary reads the runs only: the child never spends its timeout counting
        self.assertIn("--no-coverage", argv[argv.index("sync"):])
        # GA-F12: every relation of up to 70,000 rows (the manufacturing and assembly
        # orders, their lines and statuses among them) is read whole each cycle
        self.assertEqual(argv[argv.index("--full-below-rows") + 1], "70000")
        scope = json.loads(Path(config.sources["mail"]["scope"]).read_text())
        self.assertTrue(scope["all_mailboxes"]["enabled"])
        # SharePoint (business rule everything, 2026-10-27): every document library of every
        # site is discovered and none is left out, no named folder narrows a library, and
        # a cut root stays a gap rather than a smaller scope.
        from integrations.capture_sharepoint_scope import CaptureScope
        sharepoint = CaptureScope.from_file(config.sources["sharepoint"]["scope"])
        self.assertIsNotNone(sharepoint.discover)
        self.assertEqual((sharepoint.discover.exclude, list(sharepoint.targets)), ([], []))
        self.assertEqual((sharepoint.discover.max_depth, sharepoint.discover.max_folders),
                         (20, 5000))   # the largest walk bounds the scope accepts
        # Teams: the census reaches every chat (the largest cap; past it, it rotates).
        teams = json.loads(Path(config.sources["teams"]["scope"]).read_text())
        self.assertEqual(teams["census"]["max_chats"], 100000)

class DrainAndFreshnessTest(unittest.TestCase):
    """2026-11-13/14: mail capture went stale three times in one night. A post-capture
    pass inside the mail child held fresh mail back 2 h 22 min; restarts killed every
    capture in flight, so deploys waited for a gap a back-to-back mail source never left;
    and nothing in the host kept mail within a freshness target. The scheduler now drains
    (never kills) on a stop, lets a heavy pass give way to the source it yields to, and
    starts sources by staleness against their target with a slot kept for them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="drain-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.events: list[tuple[str, str, float]] = []
        self.lock = threading.Lock()

    def note(self, name: str, what: str) -> None:
        with self.lock:
            self.events.append((name, what, time.monotonic()))

    def fake_run(self, spec, options, state, environ, **kwargs):
        """A capture child: ``quick`` ends at once; ``hold`` reads until it is drained
        (a clean end, its report kept) or cancelled (killed)."""
        self.note(spec.name, "start")
        try:
            if options.get("mode") == "hold":
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if kwargs.get("cancel") is not None and kwargs["cancel"].is_set():
                        return {**forever._base(spec), "status": "failed",
                                "error_code": forever.STOPPED}
                    if kwargs.get("drain") is not None and kwargs["drain"].is_set():
                        time.sleep(0.05)   # its next safe point
                        return {**forever._base(spec), "status": "partial", "units": 1,
                                "reasons": ["time_budget"], "drained": True}
                    time.sleep(0.01)
            return {**forever._base(spec), "status": "complete", "units": 1,
                    "units_complete": 1}
        finally:
            self.note(spec.name, "end")

    def table(self, *names: str, yields: dict | None = None) -> dict:
        return {n: SourceSpec(n, "fake-source", lambda state, options: [], summarize_mail,
                              {"mode": "str"}, budget_field=None,
                              yields_to=(yields or {}).get(n, ()))
                for n in names}

    def run_until(self, table, sections, done, *, parallel: int, seconds: float = 20):
        batches: list[dict] = []
        stop_at = time.monotonic() + seconds

        def should_stop():
            if done(self.events):
                return "stopped"
            return "timeout" if time.monotonic() > stop_at else None
        with mock.patch.object(forever, "_guarded_run", self.fake_run):
            summary = forever.Continuous(
                table, sections, self.tmp, {ENV_IDS: str(uuid.uuid4())}, grace=1,
                parallel=parallel, should_stop=should_stop, drain_seconds=5,
                refresh=lambda batch, reason: batches.append(dict(batch))).run()
        self.assertEqual(summary["reason"], "stopped", (summary, self.events))
        return summary, batches

    def starts(self, name: str) -> list[float]:
        return [t for n, what, t in self.events if n == name and what == "start"]

    def test_a_stop_drains_the_running_children_and_records_what_they_captured(self) -> None:
        # Old: a stop killed every child (STOPPED, nothing recorded) - so a restart lost
        # the run in flight and deploys waited for a gap that never came.
        table = self.table("mail")
        summary, batches = self.run_until(table, {"mail": {"mode": "hold"}},
                                          lambda ev: bool(ev), parallel=1)
        self.assertEqual(summary["stopped_sources"], [])
        recorded = [b["mail"] for b in batches if "mail" in b]
        self.assertEqual([o["state"] for o in recorded], ["partial"])
        self.assertTrue(recorded[0]["capture"]["drained"])

    def test_the_mail_backlog_gives_way_to_mail_and_never_runs_beside_it(self) -> None:
        table = self.table("mail", "mail_backlog", yields={"mail_backlog": ("mail",)})
        sections = {"mail": {"mode": "quick", "min_interval_seconds": 0.4},
                    "mail_backlog": {"mode": "hold", "min_interval_seconds": 0}}
        summary, _ = self.run_until(table, sections,
                                    lambda ev: len([e for e in ev if e[:2] == ("mail", "end")])
                                    >= 3, parallel=4)
        self.assertGreaterEqual(summary["preempted"].get("mail_backlog", 0), 1, self.events)
        # One writer: never both at once (mail waits for the drained backlog to end).
        live: set[str] = set()
        for name, what, _t in self.events:
            if what == "start":
                live.add(name)
                self.assertLessEqual(live, {"mail"} if name == "mail" else {"mail_backlog"},
                                     self.events)
            else:
                live.discard(name)
        # The backlog still ran in the gaps, and mail never waited long for it.
        self.assertTrue(self.starts("mail_backlog"))
        mail = self.starts("mail")
        self.assertTrue(all(b - a < 3.0 for a, b in zip(mail, mail[1:])), mail)

    def test_fresh_sources_start_first_and_keep_a_slot(self) -> None:
        # Old: sources started in the order they fell due; two heavy walks took both
        # slots and mail waited for one of them to end.
        table = self.table("walk1", "walk2", "mail")
        sections = {"walk1": {"mode": "hold"}, "walk2": {"mode": "hold"},
                    "mail": {"mode": "quick", "min_interval_seconds": 0.1}}
        self.run_until(table, sections,
                       lambda ev: len([e for e in ev if e[:2] == ("mail", "start")]) >= 3,
                       parallel=2, seconds=10)
        firsts = [n for n, what, _t in self.events if what == "start"][:2]
        self.assertEqual(firsts[0], "mail", self.events)
        # the second slot is mail's between its runs: walk2 never took it
        self.assertEqual(self.starts("walk2"), [], self.events)

    def test_a_maintenance_gap_drains_the_captures_and_the_run_goes_on(self) -> None:
        # 11-15: the daily mail exclusion purge needs the cycle lock, which the continuous
        # ingest holds for its whole run: it was refused every day (host_cycle_running).
        # Now a gap request drains the running capture (recorded), nothing starts during
        # the gap, and the same run starts the captures again after it: no restart.
        events = self.events
        state = {"want": False, "gaps": 0}

        class Gap:
            def wanted(gap_self) -> bool:
                return state["want"]

            def give(gap_self) -> bool:
                live = {n for n, what, _t in events if what == "start"} - {
                    n for n, what, _t in events if what == "end"}
                self.note("gap", "start")
                self.assertFalse(live, events)   # nothing runs in the gap
                time.sleep(0.2)
                self.note("gap", "end")
                state["want"], state["gaps"] = False, state["gaps"] + 1
                return True
        table = self.table("mail", "walk")
        sections = {"mail": {"mode": "quick", "min_interval_seconds": 0.1},
                    "walk": {"mode": "hold", "min_interval_seconds": 0}}

        def should_stop():
            if not state["gaps"] and len(self.starts("mail")) >= 2:
                state["want"] = True
            if state["gaps"] and self.starts("walk") and \
                    self.starts("walk")[-1] > self.gap_end():
                return "stopped"
            return "timeout" if time.monotonic() > stop_at else None
        stop_at = time.monotonic() + 20
        batches: list[dict] = []
        with mock.patch.object(forever, "_guarded_run", self.fake_run):
            summary = forever.Continuous(
                table, sections, self.tmp, {ENV_IDS: str(uuid.uuid4())}, grace=1,
                parallel=2, should_stop=should_stop, drain_seconds=5, gap=Gap(),
                refresh=lambda batch, reason: batches.append(dict(batch))).run()
        self.assertEqual((summary["reason"], summary["gaps"]), ("stopped", 1), self.events)
        # the walk was drained for the gap and its run recorded, not killed
        walks = [b["walk"] for b in batches if "walk" in b]
        self.assertTrue(walks and walks[0]["capture"]["drained"], batches)
        self.assertEqual(summary["stopped_sources"], [])
        # nothing started inside the gap; both ran again after it
        g0 = [t for n, w, t in self.events if (n, w) == ("gap", "start")][0]
        g1 = self.gap_end()
        self.assertFalse([n for n, w, t in self.events
                          if n != "gap" and w == "start" and g0 <= t <= g1])
        self.assertTrue([t for t in self.starts("mail") if t > g1], self.events)

    def test_a_gap_that_ends_without_the_lock_ends_the_run(self) -> None:
        # Give() returns False when a stop came in the gap and the
        # cycle lock was not taken back; the run ignored it and, had the stop cleared, would
        # have started captures beside the step holding the lock. Now the run ends there.
        state = {"want": False, "gaps": 0, "in_gap": False}

        class Gap:
            def wanted(gap_self) -> bool:
                return state["want"]

            def give(gap_self) -> bool:
                self.note("gap", "start")
                state["in_gap"] = True
                state["want"], state["gaps"] = False, state["gaps"] + 1
                self.note("gap", "end")
                return False   # the stop seen inside, lock not held
        table = self.table("mail")
        sections = {"mail": {"mode": "quick", "min_interval_seconds": 0.1}}

        def should_stop():
            if not state["gaps"] and len(self.starts("mail")) >= 2:
                state["want"] = True
            # the stop is seen only inside the gap (a stop file removed at once)
            return "timeout" if time.monotonic() > stop_at else None
        stop_at = time.monotonic() + 5
        with mock.patch.object(forever, "_guarded_run", self.fake_run):
            summary = forever.Continuous(
                table, sections, self.tmp, {ENV_IDS: str(uuid.uuid4())}, grace=1,
                parallel=1, should_stop=should_stop, drain_seconds=5, gap=Gap(),
                refresh=lambda batch, reason: None).run()
        self.assertEqual((summary["reason"], summary["gaps"]),
                         (forever.GAP_LOCK_NOT_HELD, 1), self.events)
        self.assertFalse([t for t in self.starts("mail") if t > self.gap_end()], self.events)

    def gap_end(self) -> float:
        ends = [t for n, w, t in self.events if (n, w) == ("gap", "end")]
        return ends[0] if ends else float("inf")

    def test_the_hosts_cycle_gap_lets_a_waiting_step_take_the_lock_and_takes_it_back(self):
        import fcntl
        from integrations import capture_mail, cycle_gap

        state = self.tmp / "state"
        host_dir = state / "host"
        host_dir.mkdir(parents=True)
        held = open(host_dir / "cycle.lock", "a+")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the continuous ingest's lock
        gap = host_module.CycleGap(host_dir, held, should_stop=lambda: None, poll=0.02)
        self.assertFalse(gap.wanted())
        ran: list[bool] = []

        class Store:
            def close(self) -> None:
                pass
        out: list[str] = []
        codes: list[int] = []

        def step():
            with mock.patch("builtins.print", side_effect=out.append):
                codes.append(capture_mail._locked(
                    str(state), lambda store: ran.append(True) or {"n": 1},
                    open_store=lambda s: Store(), exists=lambda s: True,
                    wait_seconds=10, purpose="test_purge", poll=0.02))
        worker = threading.Thread(target=step)
        worker.start()
        deadline = time.monotonic() + 10
        while not gap.wanted():
            self.assertLess(time.monotonic(), deadline, "no gap request")
            time.sleep(0.02)
        self.assertEqual(cycle_gap.live(host_dir), ["test_purge"])
        self.assertFalse(ran)   # it waits while the host holds the lock
        self.assertTrue(gap.give())
        worker.join(10)
        self.assertEqual((codes, ran, json.loads(out[0])["n"]), ([0], [True], 1))
        self.assertEqual(cycle_gap.live(host_dir), [])   # withdrawn
        with open(host_dir / "cycle.lock", "a+") as other:   # the host holds it again
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # without a gap answered in time the step is refused as before
        out.clear()
        with mock.patch("builtins.print", side_effect=out.append):
            code = capture_mail._locked(str(state), lambda store: {"n": 1},
                                        open_store=lambda s: Store(), exists=lambda s: True,
                                        wait_seconds=0.1, poll=0.02)
        self.assertEqual((code, json.loads(out[0])["error"]), (2, "host_cycle_running"))
        self.assertEqual(cycle_gap.live(host_dir), [])

    def test_a_stuck_or_dead_gap_request_never_holds_the_captures_off(self) -> None:
        import fcntl
        from integrations import cycle_gap

        host_dir = self.tmp / "host"
        host_dir.mkdir()
        held = open(host_dir / "cycle.lock", "a+")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # a request of a process that ended, or of a reused pid, is not live
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (host_dir / f"gap.{dead.pid}.request").write_text(json.dumps({"pid": dead.pid}))
        (host_dir / f"gap.{os.getpid()}.request").write_text(
            json.dumps({"pid": os.getpid(), "started": "1", "purpose": "reused"}))
        self.assertEqual(cycle_gap.live(host_dir), [])
        # a live request never withdrawn: the gap ends at its bound, then requests wait
        path = cycle_gap.request(host_dir, purpose="stuck")
        clock = [0.0]

        def wait(seconds: float) -> None:
            clock[0] += seconds
        gap = host_module.CycleGap(host_dir, held, should_stop=lambda: None, wait=wait,
                                   poll=1.0, max_seconds=30, monotonic=lambda: clock[0])
        self.assertTrue(gap.wanted())
        self.assertTrue(gap.give())
        self.assertEqual(clock[0], 30.0)
        self.assertFalse(gap.wanted())   # quiet for another bound
        clock[0] += 31
        self.assertTrue(gap.wanted())
        cycle_gap.withdraw(path)
        self.assertFalse(gap.wanted())
        # a stop during the gap returns at once, without the lock
        cycle_gap.request(host_dir, purpose="again")
        stop = host_module.CycleGap(host_dir, held, should_stop=lambda: "stopped",
                                    wait=wait, monotonic=lambda: clock[0])
        self.assertFalse(stop.give())

    def test_a_source_past_its_target_drains_a_heavy_run_for_its_slot(self) -> None:
        table = self.table("walk", "mail")
        sections = {"walk": {"mode": "hold"},
                    "mail": {"mode": "quick", "min_interval_seconds": 0.1,
                             "freshness_target_seconds": 0.3}}
        summary, _ = self.run_until(
            table, sections,
            lambda ev: len([e for e in ev if e[:2] == ("mail", "start")]) >= 2, parallel=1)
        self.assertEqual(summary["preempted"], {"walk": 1}, self.events)
        order = [n for n, what, _t in self.events if what == "start"]
        self.assertEqual(order[:3], ["mail", "walk", "mail"], self.events)

    def test_run_source_drains_a_child_and_kills_one_past_its_bound(self) -> None:
        script = self.tmp / "child.py"
        script.write_text(textwrap.dedent("""
            import json, signal, sys, time
            sys.path.insert(0, sys.argv[2])
            from integrations import drain
            if sys.argv[1] == "ignore":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            else:
                assert drain.install()   # the host's environment asks for it
            open(sys.argv[3], "w").close()   # ready: the handler is in place
            deadline = time.time() + 30
            while not drain.requested() and time.time() < deadline:
                time.sleep(0.02)
            if not drain.requested():   # its own 30 s loop ran out
                open(sys.argv[3] + ".outlived", "w").close()
            print(json.dumps({"drained": drain.requested(), "runs": [{"mailboxes": [
                {"folders": [{"complete": False, "reason": "time_budget"}]}]}]}))
        """), encoding="utf-8")

        ready = self.tmp / "ready"

        def spec(mode):
            return SourceSpec("mail", "fake-source", lambda state, options: [
                sys.executable, str(script), mode, str(ROOT), str(ready)], summarize_mail,
                {}, budget_field=None)

        def when_ready(event):   # drain once the child is in its loop, at any host load
            deadline = time.monotonic() + 60
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            event.set()
        env = {"PATH": os.environ.get("PATH", "")}
        for mode, bound in (("drain", 10.0), ("ignore", 1.0)):
            event = threading.Event()
            ready.unlink(missing_ok=True)
            outlived = Path(str(ready) + ".outlived")
            outlived.unlink(missing_ok=True)
            threading.Thread(target=when_ready, args=(event,), daemon=True).start()
            result = forever.run_source(spec(mode), {"timeout_seconds": 60}, self.tmp, env,
                                        grace=1, drain=event, drain_seconds=bound)
            if mode == "drain":
                self.assertEqual((result["status"], result.get("drained")),
                                 ("partial", True), result)
            else:   # still running at the bound: killed, never recorded
                self.assertEqual((result["error_code"], result.get("drain_overran")),
                                 (forever.STOPPED, True), result)
            # never the child's own 30 s loop: drained or killed before it ran out
            self.assertFalse(outlived.exists(), mode)

    def test_the_hosts_first_sigterm_drains_and_a_second_exits(self) -> None:
        event = threading.Event()
        with self.assertRaises(SystemExit) as ended:
            with host_module._terminated_exits(event):
                os.kill(os.getpid(), signal.SIGTERM)
                deadline = time.monotonic() + 5
                while not event.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(event.is_set())
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
        self.assertEqual(ended.exception.code, 128 + signal.SIGTERM)

    def test_an_exclusion_stop_is_its_own_state_and_alarms_at_once(self) -> None:
        # 2026-11-10..12: mail and Teams failed closed two days on ExclusionUnavailable,
        # one untyped failure among others. It is a typed block now, recorded as such,
        # and the first one alarms.
        from datetime import datetime, timezone
        from operating import ops_health
        spec = forever.SOURCES["teams"]
        crashed = {**forever._base(spec), "status": "failed",
                   "error_code": "ExclusionUnavailable"}
        out = forever.surface_exclusion(spec, crashed)
        self.assertEqual((out["status"], out["exclusion_unavailable"],
                          out["blocked"][0]["error_code"], out["blocked"][0]["later_ask"]),
                         ("blocked", True, forever.EXCLUSION_UNAVAILABLE, "excluded-people-oids"))
        guard = forever.surface_exclusion(spec, {**forever._base(spec), "status": "blocked",
                                                 "blocked": [{"part": "mail.people_exclusion",
                                                              "error_code":
                                                              "excluded_people_unresolved"}]})
        self.assertTrue(guard["exclusion_unavailable"])
        plain = {**forever._base(spec), "status": "failed", "error_code": "child_timeout"}
        self.assertNotIn("exclusion_unavailable", forever.surface_exclusion(spec, plain))
        runs = forever.SourceRuns(self.tmp, now=lambda: "2030-01-02T11:50:00Z")
        runs.record("teams", out)
        row = json.loads((self.tmp / "host" / forever.RUNS_FILE).read_text())["sources"]["teams"]
        self.assertEqual((row["exclusion_unavailable_runs"],
                          row["exclusion_unavailable_since"]), (1, "2030-01-02T11:50:00Z"))
        ledger = self.tmp / "host" / "host.sqlite"
        with closing(sqlite3.connect(ledger)) as db:
            db.execute("CREATE TABLE source_state (source TEXT, configured INTEGER)")
            db.execute("INSERT INTO source_state VALUES ('teams', 1)")
            db.commit()
        checks = ops_health.capture_run_checks(
            self.tmp, datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
            ops_health.DEFAULT_TARGETS)
        (alarm,) = [c for c in checks if c["name"] == "capture_exclusion:teams"]
        self.assertEqual(alarm["state"], "breach")
        self.assertIn("people exclusion unavailable", alarm["detail"])
        runs.record("teams", {**forever._base(spec), "status": "complete"})
        checks = ops_health.capture_run_checks(
            self.tmp, datetime(2030, 1, 2, 12, 5, tzinfo=timezone.utc),
            ops_health.DEFAULT_TARGETS)
        self.assertFalse([c for c in checks if c["name"].startswith("capture_exclusion:")])

    def test_a_one_shot_cycle_never_runs_the_backlog_beside_mail(self) -> None:
        # Review 7909fdfa28: read_sources (host run/check, the daily live check) started
        # every source at once, so the backlog wrote the mail store beside the capture.
        table = self.table("mail", "mail_backlog", "teams", yields={"mail_backlog": ("mail",)})
        sections = {"mail": {}, "mail_backlog": {}, "teams": {}}

        def run(spec, options, state, environ, **kwargs):
            self.note(spec.name, "start")
            if spec.name == "mail":
                time.sleep(0.3)
            self.note(spec.name, "end")
            return {**forever._base(spec), "status": "complete", "units": 1,
                    "units_complete": 1}
        with mock.patch.object(forever, "_guarded_run", run):
            out = forever.read_sources(table, sections, self.tmp,
                                       {ENV_IDS: str(uuid.uuid4())}, grace=1, parallel=3)
        self.assertEqual(set(out), {"mail", "mail_backlog", "teams"})
        order = [(n, w) for n, w, _t in self.events]
        self.assertLess(order.index(("mail", "end")), order.index(("mail_backlog", "start")),
                        order)

    def test_drain_seconds_is_an_ingest_setting(self) -> None:
        self.assertEqual(forever.ingest_settings({"mode": forever.MODE})["drain_seconds"],
                         forever.DEFAULT_DRAIN_SECONDS)
        self.assertEqual(forever.ingest_settings({"mode": forever.MODE,
                                                  "drain_seconds": 90})["drain_seconds"], 90)
        for bad in (0, forever.MAX_DRAIN_SECONDS + 1, "60", True):
            with self.assertRaises(forever.IngestError, msg=bad):
                forever.ingest_settings({"mode": forever.MODE, "drain_seconds": bad})
        env = forever.child_env({"PATH": "/bin"}, forever.SOURCES["mail"], self.tmp)
        self.assertEqual(env["PLX_INGEST_DRAIN"], "1")


class GraphNotificationWakeTest(unittest.TestCase):
    """AUD-05 / ARCH-REVIEW L2: a Graph change notification (through Event Hubs) wakes the
    mail or Teams delta capture early. Off unless ``ingest.graph_notifications``: without
    it no wake is read and every source keeps its cadence."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wake-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.events: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def fake_run(self, spec, options, state, environ, **kwargs):
        self.events.append((spec.name, "start"))
        if spec.name in self.fail:
            return {**forever._base(spec), "status": "failed", "error_code": "http_503"}
        return {**forever._base(spec), "status": "complete", "units": 1, "units_complete": 1}

    def run_with(self, wakes, *, wake_after_first_mail: bool, seconds: float = 4.0):
        from integrations import graph_notifications as gn
        names = ("mail", "teams")
        table = {n: SourceSpec(n, "fake-source", lambda state, options: [], summarize_mail,
                               {}, budget_field=None) for n in names}
        sections = {n: {"min_interval_seconds": 7200} for n in names}
        stop_at = time.monotonic() + seconds
        woke: list[bool] = []

        def should_stop():
            if wake_after_first_mail and not woke and ("mail", "start") in self.events:
                time.sleep(0.02)   # the notification comes after the run started
                gn.wake(self.tmp, "mail")
                woke.append(True)
            if self.events.count(("mail", "start")) >= 2:
                return "done"
            return "timeout" if time.monotonic() > stop_at else None
        with mock.patch.object(forever, "_guarded_run", self.fake_run), \
                mock.patch.object(gn, "WAKE_MIN_GAP_SECONDS", 0.2):
            return forever.Continuous(
                table, sections, self.tmp, {ENV_IDS: str(uuid.uuid4())}, grace=1,
                parallel=2, should_stop=should_stop, refresh=lambda batch, reason: None,
                wakes=wakes).run()

    def test_a_wake_starts_mail_again_soon_and_leaves_teams_on_its_cadence(self) -> None:
        from integrations.graph_notifications import Wakes
        summary = self.run_with(Wakes(self.tmp), wake_after_first_mail=True)
        self.assertEqual(summary["reason"], "done", (summary, self.events))
        self.assertEqual(summary["runs"], {"mail": 2, "teams": 1})
        self.assertEqual(summary["woken"], {"mail": 1})

    def test_unconfigured_reads_no_wake_and_changes_nothing(self) -> None:
        summary = self.run_with(None, wake_after_first_mail=True, seconds=1.5)
        self.assertEqual(summary["reason"], "timeout")
        self.assertEqual(summary["runs"], {"mail": 1, "teams": 1})
        self.assertNotIn("woken", summary)
        self.assertFalse(forever.ingest_settings({"mode": forever.MODE})["graph_notifications"])
        # a mistyped optional setting never refuses the whole host config (11-07 loop)
        for text, want in (("true", True), ("False", False), (" TRUE ", True)):
            self.assertIs(forever.ingest_settings(
                {"mode": forever.MODE, "graph_notifications": text})["graph_notifications"], want)
        for bad in ("yes", 1, None, [], {}):
            warned: list[str] = []
            got = forever.ingest_settings({"mode": forever.MODE, "graph_notifications": bad},
                                          warned)
            self.assertIs(got["graph_notifications"], False, bad)
            self.assertEqual(warned, ["ingest.graph_notifications_invalid"], bad)
        # the receiver's secrets never reach a capture child
        from integrations import graph_notifications as gn
        for name in ("mail", "teams"):
            env = forever.child_env({gn.ENV_NAMESPACE: "x", gn.ENV_CLIENT_STATE: "y",
                                     "PATH": "/bin"}, forever.SOURCES[name], self.tmp)
            self.assertFalse({gn.ENV_NAMESPACE, gn.ENV_CLIENT_STATE} & set(env), name)

    def test_default_off_touches_no_notification_code_and_keeps_the_summary_shape(self) -> None:
        """No PLX_GRAPH_NOTIFY_* settings and ingest.graph_notifications false (the default):
        the host never builds Wakes, the scheduler never reads one, and the run summary has
        exactly the keys it had before the receiver existed."""
        summary = self.run_with(None, wake_after_first_mail=True, seconds=1.5)
        self.assertEqual(
            {"reason", "runs", "gaps", "stopped_sources", "preempted", "drain_overran",
             "unrefreshed"} - set(summary), set())
        self.assertNotIn("woken", summary)
        self.assertEqual(summary["runs"], {"mail": 1, "teams": 1})   # cadence only
        self.assertFalse(forever.ingest_settings({"mode": forever.MODE})["graph_notifications"])
        host = mock.MagicMock()
        host.config.ingest = {"grace_seconds": 1}          # the setting absent
        host.state_dir = self.tmp
        seen: list = []

        class Stub:
            def __init__(self, *args, **kwargs):
                seen.append(kwargs.get("wakes", "absent"))

            def run(self):
                return {}
        with mock.patch.object(forever, "Continuous", Stub), \
                mock.patch.object(forever, "budget_environ", return_value={}), \
                mock.patch.object(forever, "max_parallel", return_value=1), \
                mock.patch.object(host_module, "CycleGap"), \
                mock.patch.object(host_module, "_notify_wakes",
                                  side_effect=AssertionError("built wakes")), \
                mock.patch.object(host_module.session_identity, "bind_state_dir"):
            host_module.Host.ingest_continuous(host, stop_file=self.tmp / "stop")
        self.assertEqual(seen, [None])

    def test_a_wake_older_than_the_run_or_during_backoff_runs_nothing_extra(self) -> None:
        from integrations import graph_notifications as gn
        gn.wake(self.tmp, "mail", now=lambda: time.time() - 60)   # before the run began
        summary = self.run_with(gn.Wakes(self.tmp), wake_after_first_mail=False, seconds=1.5)
        self.assertEqual(summary["runs"], {"mail": 1, "teams": 1}, self.events)
        self.events.clear()
        self.fail.add("mail")   # a failing source keeps its backoff, woken or not
        summary = self.run_with(gn.Wakes(self.tmp), wake_after_first_mail=True, seconds=1.5)
        self.assertEqual(summary["runs"], {"mail": 1, "teams": 1}, self.events)

    def test_the_host_reads_wakes_only_with_the_setting(self) -> None:
        seen: list = []

        class Stub:
            def __init__(self, *args, **kwargs):
                seen.append(kwargs.get("wakes"))

            def run(self):
                return {}
        for setting, expect in ((False, type(None)), (True, __import__(
                "integrations.graph_notifications", fromlist=["Wakes"]).Wakes)):
            host = mock.MagicMock()
            host.config.ingest = {"grace_seconds": 1, "graph_notifications": setting}
            host.state_dir = self.tmp
            with mock.patch.object(forever, "Continuous", Stub), \
                    mock.patch.object(forever, "budget_environ", return_value={}), \
                    mock.patch.object(forever, "max_parallel", return_value=1), \
                    mock.patch.object(host_module, "CycleGap"), \
                    mock.patch.object(host_module.session_identity, "bind_state_dir"):
                host_module.Host.ingest_continuous(host, stop_file=self.tmp / "stop")
            self.assertIsInstance(seen[-1], expect)


class SourceStateWhileCycleRunsTest(unittest.TestCase):
    """AUD-10 (audit A section 12): ``source_state`` was written when a cycle ended, so a
    source whose child had ended read as old for the hours a build ran, and one whose child
    a killer left running read as captured. Each source is recorded when its child ends, and
    a source an interrupted holder left reading is unknown, not fresh."""

    setUp, config = IngestModeTest.setUp, IngestModeTest.config
    host, until = ConcurrentIngestTest.host, ConcurrentIngestTest.until
    runs = ConcurrentIngestTest.runs

    def generation(self, host: host_module.Host, name: str) -> int:
        row = next((s for s in host.status()["sources"] if s["source"] == name), None)
        return row["generation"] if row else 0

    def test_a_source_that_captured_after_a_killed_holder_is_not_marked_unknown(self) -> None:
        # the dead holder's mark is made when the lock is taken, before any capture: a
        # capture recorded afterwards stands (continuous mode)
        table = fake_table(self.script, "fast")
        host = self.host(self.config({"fast": {"min_interval_seconds": 1}}), table)
        with host._ledger() as db:
            db.execute("INSERT INTO source_state VALUES ('fast', 1, 3, '2026-11-13T10:00:00Z',"
                       " 'live', 1, '2026-11-13T10:00:00Z', NULL, NULL, NULL)")
        (self.state / host_module.HOST_DIR / host_module.CHILDREN_FILE).write_text(json.dumps({
            "schema": host_module.CHILDREN_SCHEMA, "children": [
                {"pid": 4194000, "pgid": 4194000, "token": "gone", "source": "fast"}]}))
        host.ingest_continuous(stop_file=self.tmp / "stop", max_refreshes=1)
        (row,) = [r for r in host.status()["sources"] if r["source"] == "fast"]
        self.assertIsNone(row["last_error"])
        self.assertTrue(row["complete"])
        self.assertIsNone(row["stale_since"])
        self.assertGreater(row["generation"], 3)

    def test_an_outcome_replaced_while_a_refresh_waits_is_still_recorded_once(self) -> None:
        host = self.host(self.config({"fast": {}}), fake_table(self.script, "fast"))
        first = {"state": "captured", "complete": True, "reasons": []}
        second = {"state": "captured", "complete": True, "reasons": []}
        host._record_early("fast", first)
        host._record_early("fast", second)   # ends while the refresh of `first` is queued
        with mock.patch.object(host_module.Host, "_project", lambda s_, c: {
                "state": "skipped", "generation": None, "reasons": ["x"]}):
            cycle_id, started = host._open_cycle("ingest")
            host._finish(cycle_id, started, {"fast": first}, "ingest", {})
        self.assertEqual(host.status()["sources"][0]["generation"], 2)

    def test_a_source_a_killed_holder_left_reading_is_unknown_until_read_again(self) -> None:
        table = fake_table(self.script, "fast")
        config = self.config({"fast": {"min_interval_seconds": 1}})
        host = self.host(config, table)
        with host._ledger() as db:
            db.execute("INSERT INTO source_state VALUES ('fast', 1, 3, '2026-11-13T10:00:00Z',"
                       " 'live', 1, '2026-11-13T10:00:00Z', NULL, NULL, NULL)")
            db.execute("INSERT INTO cycle(started_at, state, report) VALUES "
                       "('2026-11-13T10:00:00Z', 'running', '{}')")
        children = self.state / host_module.HOST_DIR / host_module.CHILDREN_FILE
        children.write_text(json.dumps({"schema": host_module.CHILDREN_SCHEMA, "children": [
            {"pid": 4194000, "pgid": 4194000, "token": "gone", "source": "fast"}]}))
        with host._cycle_lock():
            host._open_cycle("ingest")
        (row,) = host.status()["sources"]
        self.assertEqual((row["generation"], row["captured_at"]), (3, "2026-11-13T10:00:00Z"))
        self.assertIsNone(row["complete"])
        self.assertEqual((row["stale_since"], row["last_error"]),
                         ("2026-11-13T10:00:00Z", "cycle_interrupted"))
        # a source nobody was reading when the holder died keeps its state
        with host._ledger() as db:
            db.execute("UPDATE source_state SET complete=1, stale_since=NULL, error=NULL")
        children.write_text(json.dumps({"schema": host_module.CHILDREN_SCHEMA,
                                        "children": []}))
        with host._cycle_lock():
            host._open_cycle("ingest")
        self.assertTrue(host.status()["sources"][0]["complete"])
        # read again: recorded as captured, the marks gone
        with host._ledger() as db:
            host._record(db, "fast", {"state": "captured", "complete": True,
                                      "reasons": []}, host.clock())
        (row,) = host.status()["sources"]
        self.assertEqual((row["generation"], row["stale_since"], row["last_error"],
                          row["complete"]), (4, None, None, True))


if __name__ == "__main__":
    unittest.main()
