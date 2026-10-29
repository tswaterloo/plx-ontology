"""The host's ingest mode (``operating.host`` with ``ingest``; ``operating.ingest_forever``
its capture-CLI reads) at the host's own boundary, with fake capture CLIs and no network.

One writer per state directory (decision 2026-10-21-ingest-one-writer): an ingest cycle is
a host ledger cycle under the host's one cycle lock, rebuilds the host's projection and is
checked by the host's ``check``. An unset people exclusion blocks every source; one
failing source does not stop the cycle; each source's cursor carries from one cycle to
the next; a killed runner's capture child is stopped before the next cycle reads.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
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
    if mode == "fail":
        print(json.dumps({"error": "SourceUnavailable", "error_class": "upstream",
                          "http_status": 503,
                          "message": "mailbox alice@example.com unreachable"}),
              file=sys.stderr)
        sys.exit(1)
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
        deadline = time.time() + 30
        while not os.path.exists(os.path.join(state, "release")) and time.time() < deadline:
            time.sleep(0.05)
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
        table = fake_table(self.script, "broken", "hung", "mail")
        config = self.config({"broken": {"mode": "fail"},
                              "hung": {"mode": "hang", "timeout_seconds": 1}, "mail": {}})
        report, text = self.run_cycle(config, table)
        states = {n: o["state"] for n, o in report["sources"].items()}
        self.assertEqual(states, {"broken": "failed", "hung": "failed", "mail": "captured"})
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
        self.assertEqual(self.ledger("SELECT source, generation, error FROM source_state "
                                     "ORDER BY source"),
                         [("broken", 0, "SourceUnavailable"), ("hung", 0, "child_timeout"),
                          ("mail", 1, None)])
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
        child = forever.child_env({"FAKE_SOURCE_TOKEN": "x", "OTHER_SECRET": "y", "PATH": "p"},
                                  table["mail"], self.state)
        self.assertEqual(child, {"FAKE_SOURCE_TOKEN": "x", "PATH": "p",
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
                ({"nosuch": {}}, None, "host_config_invalid"),
                ({"mail": {"unknown_key": 1}}, None, "host_config_invalid:mail"),
                ({"mail": {"timeout_seconds": 0}}, None, "host_config_invalid:mail"),
                ({"mail": {}}, {"mode": "loop"}, "host_config_invalid:ingest"),
                ({"mail": {}}, {"mode": forever.MODE, "grace_seconds": 0},
                 "host_config_invalid:ingest"),
                ({"mail": {}}, {"mode": forever.MODE, "cadence": 5},
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
        with self.assertRaises(host_module.HostError) as raised:
            host_module.HostConfig.from_file(plain)
        self.assertEqual(raised.exception.code, "host_config_invalid")
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

class ConcurrentIngestTest(unittest.TestCase):
    """Sources read concurrently (2026-10-27: one cycle of seven sources one after another
    took about 2.5 h before the projection showed anything). A cycle runs its children
    at once; the continuous mode (``serve``) refreshes the projection as each source
    finishes, re-runs a fast source while a slow one reads, and stops on its stop file
    or low disk; one failing source never stops the others."""

    setUp, config, main, run_cycle, ran, ledger = (
        IngestModeTest.setUp, IngestModeTest.config, IngestModeTest.main,
        IngestModeTest.run_cycle, IngestModeTest.ran, IngestModeTest.ledger)

    def host(self, config: Path, table: dict, env: dict | None = None) -> host_module.Host:
        return host_module.Host(self.state, host_module.HostConfig.from_file(
            config, capture_sources=table), environ=self.env if env is None else env,
            disk_free=ROOMY)

    def until(self, condition, what: str, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.05)

    def runs(self, name: str) -> int:
        path = self.state / f"fake-{name}" / "ran"
        return len(path.read_text().splitlines()) if path.is_file() else 0

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
        for bad in (0, 8, "2", True):
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
        config = self.config({"bc": {"min_interval_seconds": 1},
                              "slow": {"mode": "slow", "timeout_seconds": 60},
                              "broken": {"mode": "fail", "min_interval_seconds": 1}})
        host, stop_file, reports = self.host(config, table), self.tmp / "stop", []
        summary: list[dict] = []
        runner = threading.Thread(target=lambda: summary.append(host.ingest_continuous(
            stop_file=stop_file, on_refresh=reports.append)))
        runner.start()
        self.addCleanup(lambda: (stop_file.touch(), runner.join(60)))
        # The slow source still reads while the fast one is projected and runs again.
        self.until(lambda: any("bc" in r["sources"] and r["projection"]["state"] == "built"
                               for r in reports), "no projection after the first finisher")
        self.until(lambda: self.runs("bc") >= 3, "the fast source waited for the slow one")
        self.assertTrue((self.state / "fake-slow" / "started").is_file())
        self.assertNotIn("slow", {n for r in reports for n in r["sources"]})
        self.assertTrue(host_module.projection_path(self.state).is_file())
        with self.assertRaises(host_module.HostError) as held:   # one writer meanwhile
            self.host(config, table).cycle()
        self.assertEqual(held.exception.code, "host_cycle_running")
        (self.state / "release").touch()
        self.until(lambda: any("slow" in r["sources"] for r in reports), "slow never refreshed")
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
        self.assertLess(len(broken), self.runs("bc"))
        # Each refresh is one ledger cycle with its own generation, chained, incremental
        # after the first; none overlapped another.
        built = [r["projection"] for r in reports if r["projection"]["state"] == "built"]
        self.assertEqual([p["generation"] for p in built], [r["cycle"] for r in reports
                                                            if r["projection"]["state"]
                                                            == "built"])
        self.assertEqual([p["previous_generation"] for p in built[1:]],
                         [p["generation"] for p in built[:-1]])
        self.assertEqual({p["mode"] for p in built[1:]}, {"incremental"})
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
                    refresh=lambda batch, reason, exclusion: refreshed.extend(
                        name for name, o in batch.items() if o["state"] == "captured"),
                    should_stop=lambda: "done" if len(refreshed) >= n else None).run()
                self.assertLess(time.monotonic() - started, 10 + n)
                self.assertEqual(sorted(refreshed), names)
                self.assertEqual((done["reason"], done["runs"]), ("done", dict.fromkeys(names, 1)))


class BuildExclusionTest(unittest.TestCase):
    """Live 2026-10-27, repeated: under ``serve`` the Portal mirror capture wrote while a
    build held its read snapshot, so its WAL could not be checkpointed and grew 4-6 GB
    per overlap until builds failed or skipped on a 15 GB disk. A capture of a store the
    build reads never overlaps a build; the build waits for it, checkpoints its store,
    then builds; a capture of a store the build does not read still overlaps."""

    setUp, config = IngestModeTest.setUp, IngestModeTest.config
    host, until = ConcurrentIngestTest.host, ConcurrentIngestTest.until

    def running(self, name: str) -> bool:
        return (self.state / f"fake-{name}" / "running").exists()

    def scheduler(self, build_reads, refreshes: int = 6):
        """Continuous over a ``mirror`` capture (writes a store ``build_reads`` may
        name), an ``other`` one (writes a store no build reads) and a ``fast`` one that
        finishes often; each refresh is a 0.3 s build that notes which captures ran at
        its start and its end."""
        read_store, other_store = self.tmp / "read.sqlite", self.tmp / "other.sqlite"
        table = fake_table(self.script, "mirror", "other", "fast", writes={
            "mirror": lambda state: (read_store,), "other": lambda state: (other_store,)})
        sections = {"mirror": {"mode": "busy", "min_interval_seconds": 0},
                    "other": {"mode": "long", "min_interval_seconds": 0},
                    "fast": {"min_interval_seconds": 0.2}}
        builds: list[dict] = []

        def refresh(batch, reason, exclusion):
            seen = {n for n in ("mirror", "other") if self.running(n)}
            time.sleep(0.3)
            seen |= {n for n in ("mirror", "other") if self.running(n)}
            builds.append({"ran": seen, "batch": set(batch), **exclusion})

        done = forever.Continuous(
            table, sections, self.state, self.env, grace=1, parallel=3, refresh=refresh,
            should_stop=lambda: "done" if len(builds) >= refreshes else None,
            build_reads=lambda: build_reads(read_store)).run()
        return done, builds

    def test_a_build_never_overlaps_a_capture_of_a_store_it_reads(self) -> None:
        done, builds = self.scheduler(lambda store: [store, self.tmp / "unrelated"])
        self.assertEqual(done["reason"], "done")
        self.assertEqual([b for b in builds if "mirror" in b["ran"]], [])
        self.assertTrue(any("other" in b["ran"] for b in builds), builds)  # still overlaps
        self.assertTrue(any(b["waited_for"] == ["mirror"] and b["wait_seconds"] > 0
                            for b in builds), builds)
        self.assertTrue(all(b["exclusive"] == sorted({"mirror"} & b["batch"])
                            for b in builds), builds)
        self.assertGreater(done["runs"]["mirror"], 1)   # it ran again between builds
        # Not vacuous: when the build reads no store of it, the mirror capture overlaps.
        self.state = self.tmp / "control"
        _, builds = self.scheduler(lambda store: [])
        self.assertTrue(any("mirror" in b["ran"] for b in builds), builds)
        self.assertEqual({w for b in builds for w in b["waited_for"]}, set())
        # Unknown fails closed: a build_reads that raises makes every writer exclusive.
        table = fake_table(self.script, "mirror", "other", "fast", writes={
            "mirror": lambda state: (self.tmp / "a",), "other": lambda state: (self.tmp / "b",)})

        def broken():
            raise OSError("ledger")
        self.assertEqual(forever.Continuous(
            table, {n: {} for n in table}, self.state, self.env, grace=1, parallel=1,
            refresh=print, should_stop=lambda: "x", build_reads=broken).exclusive(),
            {"mirror", "other"})

    def test_every_real_capture_writes_a_store_the_host_build_reads(self) -> None:
        """Every store a build opens, checked: each capture of the real table writes
        one, so none overlaps a build (the Portal mirror among them)."""
        host = host_module.Host(self.state, None, environ=self.env)
        scheduler = forever.Continuous(
            forever.SOURCES, {n: {} for n in forever.SOURCES}, self.state, self.env,
            grace=1, parallel=1, refresh=print, should_stop=lambda: "x",
            build_reads=host.build_reads)
        self.assertEqual(scheduler.exclusive(), set(forever.SOURCES))
        self.assertIn(str(forever.portal_mirror(self.state).resolve()),
                      {str(p.resolve()) for p in host.build_reads()})

    def test_the_host_checkpoints_the_waited_store_and_reports_the_wait(self) -> None:
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
        table = fake_table(self.script, "mirror", "fast", writes={
            "mirror": lambda state: (forever.portal_mirror(state),)})
        config = self.config({"mirror": {"mode": "busy", "min_interval_seconds": 7200},
                              "fast": {"min_interval_seconds": 7200}})
        reports: list[dict] = []
        self.host(config, table).ingest_continuous(
            stop_file=self.tmp / "stop", max_refreshes=1, on_refresh=reports.append)
        exclusions = [r["build_exclusion"] for r in reports]
        self.assertEqual(sorted(n for r in reports for n in r["sources"]), ["fast", "mirror"])
        # fast finished first and its build waited for mirror; one batch then holds both.
        (first,) = [e for e in exclusions if "mirror" in e["exclusive"]]
        self.assertEqual(first["waited_for"], ["mirror"])
        self.assertEqual(first["checkpoint"]["mirror"]["state"], "truncated", first)
        self.assertGreater(first["checkpoint"]["mirror"]["wal_bytes_before"], 0)
        self.assertEqual(wal_bytes(mirror), 0)
        self.assertNotIn(str(self.state), json.dumps(exclusions))   # names, never paths


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
        catalog = json.loads((ROOT / "ontology" / "schema" / "generated" / "meta" /
                              "portal_catalog.json").read_text())
        self.assertEqual(sorted(config.sources["portal"]["relations"]),
                         sorted(r["name"] for r in catalog["relations"]))
        scope = json.loads(Path(config.sources["mail"]["scope"]).read_text())
        self.assertTrue(scope["all_mailboxes"]["enabled"])
        # SharePoint (owner rule everything, 2026-10-27): every document library of every
        # site is discovered and none is left out, no named folder narrows a library, and
        # a cut root stays a gap rather than a smaller scope.
        from integrations.capture_sharepoint import CaptureScope
        sharepoint = CaptureScope.from_file(config.sources["sharepoint"]["scope"])
        self.assertIsNotNone(sharepoint.discover)
        self.assertEqual((sharepoint.discover.exclude, list(sharepoint.targets)), ([], []))
        self.assertEqual((sharepoint.discover.max_depth, sharepoint.discover.max_folders),
                         (20, 5000))   # the largest walk bounds the scope accepts
        # Teams: the census reaches every chat (the largest cap; past it, it rotates).
        teams = json.loads(Path(config.sources["teams"]["scope"]).read_text())
        self.assertEqual(teams["census"]["max_chats"], 5000)

if __name__ == "__main__":
    unittest.main()
