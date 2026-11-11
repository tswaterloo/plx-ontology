"""Reliability alarms over a made-up state dir and injected probes (no systemctl, Postgres
or mail): every stop is an incident, each incident pages once through the send path (a
dry run without a page address), recovers with one notice, and the owner alone reads it.
Evidence: isolated-fixture; nothing is ever sent."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from operating import ops_alarms, ops_health
from operating.tests.test_ops_health import NOW, make_state

STAMP = int(NOW.timestamp())
GB = 1024 ** 3


def unit(uid, active="inactive", sub="dead", result="success", **extra):
    return {"Id": uid, "LoadState": "loaded", "ActiveState": active, "SubState": sub,
            "Result": result, "UnitFileState": "static", **extra}


def healthy_units():
    return [unit("plx-backup.service", ExecMainExitTimestamp=f"@{STAMP - 9 * 3600}"),
            unit("plx-restore-test.service", ExecMainExitTimestamp=f"@{STAMP - 8 * 3600}"),
            unit("plx-delta.timer", "active", "running", UnitFileState="enabled",
                 Unit="plx-delta.service", NextElapseUSecMonotonic="infinity"),
            # an interval timer has no next run while its service runs: not a stop
            unit("plx-delta.service", "activating", "start",
                 StateChangeTimestamp=f"@{STAMP - 600}"),
            # a timer switched off on purpose (disabled) is not a stop
            unit("plx-paused.timer", UnitFileState="disabled", Unit="plx-paused.service",
                 NextElapseUSecMonotonic="infinity")]


class Fake:
    """Probes and a send path that record what they were asked; never any I/O."""

    def __init__(self, system=None, user=None, pg=None, mem_gb=10.0, disk_gb=200.0):
        self.units = {"system": system if system is not None else healthy_units(),
                      "user": user or []}
        self.pg_value = pg if pg is not None else {
            "max": 200, "clients": 40, "idle": 30, "idle_in_transaction": 0,
            "longest_idle_in_transaction_minutes": None}
        self.mem_gb, self.disk_gb = mem_gb, disk_gb
        self.sent: list[tuple] = []

    def systemctl(self, scope, args):
        if self.units[scope] is None:
            raise OSError("no bus")
        if args[0] == "list-units":
            return "".join(f"{u['Id']} loaded x y desc\n" for u in self.units[scope])
        return "\n".join("\n".join(f"{k}={v}" for k, v in u.items()) + "\n"
                         for u in self.units[scope])

    def pg(self, dsn, idle):
        if isinstance(self.pg_value, Exception):
            raise self.pg_value
        return self.pg_value

    def probes(self):
        return ops_alarms.Probes(
            systemctl=self.systemctl, pg=self.pg,
            meminfo=lambda: f"MemTotal: 1 kB\nMemAvailable: {int(self.mem_gb * 1024 ** 2)} kB\n",
            disk_free=lambda path: int(self.disk_gb * GB), pg_follow=False,
            consumers=self.consumers)

    def consumers(self):
        self.consumer_reads = getattr(self, "consumer_reads", 0) + 1
        return {"cgroups": [{"name": "postgresql@17-main.service", "gb": 12.48},
                            {"name": "tmux-spawn-1.scope", "gb": 9.5}],
                "processes": [{"pid": 4242, "name": "python3 -m operating.proactive",
                               "cgroup": "plx-proactive.service", "gb": 16.2}]}

    def sender(self, environ):
        fake = self

        class Sender:
            def dispatch(self, kind, sender, payload):
                fake.sent.append((kind, sender, payload))
                return {"status": "sent", "missing": [], "http_status": 202}
        return Sender()


def never(environ):
    raise AssertionError("no page address: the send path must not be built")


class AlarmPassTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # the delta's last cycle stays fresh however far the passes below run
        self.root = make_state(Path(self.tmp.name), delta_built="2030-01-02T13:00:00Z")
        (self.root / "search").mkdir()
        (self.root / "search" / "follow.json").write_text(
            json.dumps({"updated_at": "2030-01-02T11:58:00+00:00"}))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_at(self, fake, minutes=0, environ=None, sender=None):
        return ops_alarms.run_pass(self.root, now=NOW + timedelta(minutes=minutes),
                                   environ=environ or {}, probes=fake.probes(),
                                   dsn="dbname=x", sender_factory=sender or never)

    def test_each_kind_of_stop_opens_an_incident_and_pages_as_a_dry_run(self) -> None:
        stale = make_state(Path(self.tmp.name) / "stale", delta_built="2030-01-01T20:00:00Z",
                           proactive="2030-01-01T12:00:00Z")
        self.root = stale
        system = healthy_units()
        system[0] = unit("plx-backup.service", "failed", "failed", "exit-code",
                         ExecMainExitTimestamp=f"@{STAMP - 3600}")
        system += [unit("plx-proactive.service", "failed", "failed", "oom-kill",
                        StateChangeTimestamp=f"@{STAMP - 7200}"),
                   unit("plx-proactive.timer", "active", "waiting", UnitFileState="enabled",
                        Unit="plx-proactive.service", NextElapseUSecMonotonic="infinity"),
                   unit("plx-compact.timer", UnitFileState="enabled",
                        Unit="plx-compact.service", NextElapseUSecMonotonic="infinity"),
                   unit("plx-stuck.service", "activating", "start",
                        StateChangeTimestamp=f"@{STAMP - 7 * 3600}"),
                   unit("plx-ignored.service", "failed", "failed", "exit-code")]
        user = [unit("plx-pg-follow.service", "activating", "auto-restart", NRestarts="9")]
        fake = Fake(system=system, user=user, disk_gb=40, pg={
            "max": 200, "clients": 164, "idle": 150, "idle_in_transaction": 2,
            "longest_idle_in_transaction_minutes": 42.0})
        status = self.run_at(fake, environ={"PLX_OPS_IGNORE_UNITS": "plx-ignored.service"})
        opened = {i["key"] for i in status["open"]}
        self.assertEqual(opened, {
            "delta_last_cycle", "proactive_last_pass", "unit:plx-proactive.service",
            "unit:plx-proactive.timer", "unit:plx-compact.timer", "unit:plx-stuck.service",
            "unit:user:plx-pg-follow.service", "backup", "pg_connections",
            "pg_idle_in_transaction", "disk_free"})
        lines = {i["key"]: i["line"] for i in status["open"]}
        self.assertIn("killed: out of memory", lines["unit:plx-proactive.service"])
        self.assertIn("164 of 200", lines["pg_connections"])
        self.assertIn("Data refresh (delta) last finished 16 h ago", lines["delta_last_cycle"])
        for i in status["open"]:
            self.assertEqual(i["page"]["status"], "dry_run", i)
            self.assertEqual(i["page"]["missing"], ["PLX_OPS_PAGE_TO not set"])
        self.assertFalse(status["paging"]["configured"])
        written = (stale / "ops-alarms" / "status.json").read_text()
        self.assertEqual(json.loads(written)["open"], status["open"])
        # what the owner reads now
        read = ops_alarms.read_status(stale, now=NOW + timedelta(minutes=3))
        self.assertTrue(read["running"])
        self.assertFalse(ops_alarms.read_status(stale, now=NOW + timedelta(minutes=16))
                         ["running"])

    def test_one_page_per_incident_then_one_recovery_notice(self) -> None:
        env = {"PLX_OPS_PAGE_TO": "owner@example.test"}
        fake = Fake(system=healthy_units() + [
            unit("plx-proactive.service", "failed", "failed", "oom-kill")])
        status = self.run_at(fake, 0, env, fake.sender)
        self.assertEqual([i["key"] for i in status["open"]], ["unit:plx-proactive.service"])
        self.assertEqual(len(fake.sent), 1)
        kind, sender, payload = fake.sent[0]
        self.assertEqual((kind, sender, payload["to"]),
                         ("send_email", "owner@example.test", ["owner@example.test"]))
        self.assertIn("out of memory", payload["subject"])
        self.assertIn("ET)", payload["body"])           # times in Eastern
        self.assertNotIn("owner@example.test", json.dumps(status))
        for minutes in (5, 10):                         # still broken: no second page
            self.run_at(fake, minutes, env, fake.sender)
        self.assertEqual(len(fake.sent), 1)
        fake.units["system"] = healthy_units()
        status = self.run_at(fake, 15, env, fake.sender)
        self.assertEqual(len(status["open"]), 1)        # one passing run is not recovery
        status = self.run_at(fake, 20, env, fake.sender)
        self.assertEqual(status["open"], [])
        self.assertEqual(len(fake.sent), 2)
        self.assertIn("recovered", fake.sent[1][2]["subject"])
        self.assertEqual(status["recovered"][0]["recovery_page"]["status"], "sent")
        # breaks again: a new incident, a new page
        fake.units["system"] = healthy_units() + [
            unit("plx-proactive.service", "failed", "failed", "exit-code")]
        self.run_at(fake, 25, env, fake.sender)
        self.assertEqual(len(fake.sent), 3)

    def test_unknown_neither_opens_nor_closes_and_dry_runs_page_once_set(self) -> None:
        fake = Fake(user=[unit("plx-pg-follow.service", "failed", "failed", "exit-code")])
        self.run_at(fake, 0)                             # dry run: nothing built
        fake.units["user"] = None                        # the user manager cannot answer
        fake.pg_value = ImportError("psycopg")
        for minutes in (5, 10):
            status = self.run_at(fake, minutes)
            self.assertEqual([i["key"] for i in status["open"]],
                             ["unit:user:plx-pg-follow.service"])
        checks = {c["name"]: c["state"] for c in status["checks"]}
        self.assertEqual((checks["units:user"], checks["pg_connections"]),
                         ("unknown", "unknown"))
        env = {"PLX_OPS_PAGE_TO": "owner@example.test"}
        self.run_at(fake, 15, env, fake.sender)          # set later: paged then, once
        self.run_at(fake, 20, env, fake.sender)
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("plx-pg-follow.service (user)", fake.sent[0][2]["body"])
        # the send path's own dry run (sending off) is asked again hourly, not every pass
        dry = Fake(system=healthy_units() + [unit("plx-x.service", "failed", "failed",
                                                   "exit-code")])
        dry_sender = lambda environ: type("S", (), {"dispatch": lambda self, *a: (
            dry.sent.append(a), {"status": "dry_run", "missing": ["Sending is off"]})[1]})()
        (self.root / "ops-alarms" / "state.json").unlink()
        for minutes in (30, 35, 40, 95):
            self.run_at(dry, minutes, env, dry_sender)
        self.assertEqual(len(dry.sent), 2)
        # a server refusing the check's connection (too many clients) is itself a breach
        fake.pg_value = RuntimeError("too many clients")
        status = self.run_at(fake, 25, env, fake.sender)
        self.assertIn("pg_connections", [i["key"] for i in status["open"]])

    def test_memory_pages_only_when_low_for_five_minutes(self) -> None:
        fake = Fake(mem_gb=4.0)
        status = self.run_at(fake, 0)
        self.assertNotIn("memory_available", [i["key"] for i in status["open"]])
        fake.mem_gb = 7.0                                # a dip that recovered
        self.run_at(fake, 5)
        fake.mem_gb = 4.0
        status = self.run_at(fake, 10)
        self.assertNotIn("memory_available", [i["key"] for i in status["open"]])
        status = self.run_at(fake, 15)
        self.assertIn("memory_available", [i["key"] for i in status["open"]])

    def test_memory_under_eight_gb_warns_at_once_naming_the_top_consumers(self) -> None:
        env = {"PLX_OPS_PAGE_TO": "owner@example.test"}
        fake = Fake(mem_gb=9.0)
        status = self.run_at(fake, 0, env, fake.sender)
        self.assertEqual(status["open"], [])
        self.assertEqual(getattr(fake, "consumer_reads", 0), 0)   # read only when short
        fake.mem_gb = 7.5
        status = self.run_at(fake, 5, env, fake.sender)
        self.assertEqual([i["key"] for i in status["open"]], ["memory_pressure"])
        self.assertEqual(len(fake.sent), 1)
        page = fake.sent[0][2]
        self.assertIn("Memory pressure (early warning): 7.5 GB available (warning under "
                      "8 GB); top cgroups: postgresql@17-main.service 12.48 GB, "
                      "tmux-spawn-1.scope 9.5 GB; top processes: python3 -m "
                      "operating.proactive (pid 4242, plx-proactive.service) 16.2 GB",
                      page["body"])
        self.assertEqual(page["to"], ["owner@example.test"])
        # lower for five minutes: the second incident, also naming them; no repeat page
        fake.mem_gb = 4.0
        self.run_at(fake, 10, env, fake.sender)
        status = self.run_at(fake, 15, env, fake.sender)
        self.assertEqual(sorted(i["key"] for i in status["open"]),
                         ["memory_available", "memory_pressure"])
        self.assertEqual(len(fake.sent), 2)
        self.assertIn("under 6 GB for 5 min; top cgroups", fake.sent[1][2]["body"])
        self.assertNotIn("memory_pressure", fake.sent[1][2]["body"])
        # two runs above 8 GB close both with one recovery notice
        fake.mem_gb = 20.0
        self.run_at(fake, 20, env, fake.sender)
        status = self.run_at(fake, 25, env, fake.sender)
        self.assertEqual(status["open"], [])
        self.assertEqual(len(fake.sent), 3)
        self.assertIn("Recovered: 2", fake.sent[2][2]["body"])

    def test_memory_warning_holds_when_the_consumers_or_meminfo_cannot_be_read(self) -> None:
        fake = Fake(mem_gb=7.0)
        probes = fake.probes()
        probes.consumers = lambda: (_ for _ in ()).throw(PermissionError("cgroup"))
        status = ops_alarms.run_pass(self.root, now=NOW, environ={}, probes=probes,
                                     dsn=None, sender_factory=never)
        self.assertEqual([i["key"] for i in status["open"]], ["memory_pressure"])
        self.assertIn("top consumers unreadable (PermissionError)", status["open"][0]["line"])
        # meminfo unreadable or without MemAvailable: unknown, neither opens nor closes
        for text in (OSError("proc"), "MemTotal: 1 kB\n"):
            def broken(text=text):
                if isinstance(text, Exception):
                    raise text
                return text
            probes.meminfo = broken
            for minutes in (5, 10):
                status = ops_alarms.run_pass(self.root, now=NOW + timedelta(minutes=minutes),
                                             environ={}, probes=probes, dsn=None,
                                             sender_factory=never)
            got = {c["name"]: c["state"] for c in status["checks"]}
            self.assertEqual((got["memory_pressure"], got["memory_available"]),
                             ("unknown", "unknown"))
            self.assertEqual([i["key"] for i in status["open"]], ["memory_pressure"])

    def test_the_minute_memory_pass_touches_only_memory_incidents(self) -> None:
        env = {"PLX_OPS_PAGE_TO": "owner@example.test"}
        fake = Fake(mem_gb=7.0)
        # before any full pass: incidents kept and paged, the status left to the full pass
        early = ops_alarms.run_pass(self.root, now=NOW, environ=env, probes=fake.probes(),
                                    sender_factory=fake.sender, memory_only=True)
        self.assertEqual([i["key"] for i in early["open"]], ["memory_pressure"])
        self.assertFalse((self.root / "ops-alarms" / "status.json").exists())
        self.assertEqual(len(fake.sent), 1)
        fake.units["system"] = healthy_units() + [
            unit("plx-proactive.service", "failed", "failed", "oom-kill")]
        full = self.run_at(fake, 1, env, fake.sender)
        self.assertEqual(sorted(i["key"] for i in full["open"]),
                         ["memory_pressure", "unit:plx-proactive.service"])
        self.assertEqual(len(fake.sent), 2)
        fake.units["system"] = healthy_units()   # fixed, but only a full pass may say so
        fake.mem_gb = 30.0
        for minute in (2, 3, 4):
            ops_alarms.run_pass(self.root, now=NOW + timedelta(minutes=minute), environ=env,
                                probes=fake.probes(), sender_factory=fake.sender,
                                memory_only=True)
        status = ops_alarms.read_status(self.root, now=NOW + timedelta(minutes=4))
        self.assertEqual([i["key"] for i in status["open"]], ["unit:plx-proactive.service"])
        self.assertEqual(status["checked_at"], "2030-01-02T12:01:00Z")   # the full pass's
        self.assertEqual(status["memory_checked_at"], "2030-01-02T12:04:00Z")
        names = [c["name"] for c in status["checks"]]
        self.assertEqual(names.count("memory_pressure"), 1)
        self.assertIn("units:system", names)
        self.assertEqual(len(fake.sent), 3)                  # the memory recovery notice
        self.assertIn("Memory pressure", fake.sent[2][2]["body"])
        # the full checks stopped: Home still says so whatever the memory pass does
        status = ops_alarms.read_status(self.root, now=NOW + timedelta(minutes=30))
        self.assertFalse(status["running"])
        out = io.StringIO()
        probes = fake.probes()
        with mock.patch.object(ops_alarms, "Probes", lambda: probes), \
                mock.patch.object(ops_alarms, "_default_sender", never), \
                mock.patch.dict(os.environ, {"PLX_OPS_PAGE_TO": ""}), redirect_stdout(out):
            code = ops_alarms.main(["memory", "--state-dir", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("1 open", out.getvalue())

    def test_sixty_failed_units_are_sixty_incidents_in_one_page(self) -> None:
        env = {"PLX_OPS_PAGE_TO": "owner@example.test"}
        for n in (1, 10, 60):
            with self.subTest(n=n):
                fake = Fake(system=healthy_units() + [
                    unit(f"plx-u{i:02d}.service", "failed", "failed", "exit-code")
                    for i in range(n)])
                (self.root / "ops-alarms" / "state.json").unlink(missing_ok=True)
                status = self.run_at(fake, 0, env, fake.sender)
                keys = [i["key"] for i in status["open"]]
                self.assertEqual(len(keys), n)
                self.assertEqual(len(set(keys)), n)
                self.assertEqual(len(fake.sent), 1)
                self.assertEqual(fake.sent[0][2]["body"].count("\n- "), n)

    def test_backups_too_old_or_never_run(self) -> None:
        t = ops_health.targets({})
        units = [unit("plx-backup.service", ExecMainExitTimestamp=f"@{STAMP - 40 * 3600}"),
                 unit("plx-restore-test.service")]
        found = {c["name"]: c for c in ops_alarms.backup_checks(units, NOW, t)}
        self.assertEqual(found["backup"]["state"], "breach")
        self.assertEqual(found["restore_test"]["state"], "unknown")   # none since boot
        self.assertEqual(ops_alarms.backup_checks([], NOW, t)[0]["detail"], "not installed")

    def test_the_command_exits_zero_on_a_breach_and_two_without_a_state_dir(self) -> None:
        fake = Fake(system=healthy_units() + [
            unit("plx-proactive.service", "failed", "failed", "oom-kill")])
        out = io.StringIO()
        probes = fake.probes()
        with mock.patch.object(ops_alarms, "Probes", lambda: probes), \
                mock.patch.object(ops_alarms, "_default_sender", never), \
                mock.patch.dict(os.environ, {"PLX_OPS_PAGE_TO": ""}), redirect_stdout(out):
            code = ops_alarms.main(["run", "--state-dir", str(self.root), "--pg-dsn", ""])
        self.assertEqual(code, 0)   # the page and the banner are the alarm, not the unit
        self.assertIn("OPEN    plx-proactive.service (system): killed: out of memory "
                      "(page dry_run)", out.getvalue())
        self.assertEqual(ops_alarms.main(["run", "--state-dir", str(self.root / "no")]), 2)


class MemoryConsumersTest(unittest.TestCase):
    """The top consumers read from a made-up /proc and cgroup tree."""

    def build(self, root: Path, n: int) -> tuple[Path, Path]:
        cg, proc = root / "cg", root / "proc"
        mib = 1024 ** 2

        def group(rel, anon, shmem=0, file=0):
            d = cg / rel
            d.mkdir(parents=True, exist_ok=True)
            (d / "memory.stat").write_text(
                f"anon {anon * mib}\nfile {file * mib}\nshmem {shmem * mib}\n")
        group("system.slice", 9000, file=40000)   # a parent: never ranked itself
        group("system.slice/postgresql@17-main.service", 100, shmem=3000, file=30000)
        group("system.slice/plx-cache.service", 10, file=50000)   # page cache: given back
        group("user.slice/u/tmux-spawn-1.scope", 5000)
        (cg / "user.slice" / "u" / "tmux-spawn-1.scope" / "memory.stat").chmod(0)
        for i in range(n):
            d = proc / str(1000 + i)
            d.mkdir(parents=True)
            (d / "status").write_text(f"Name:\tx\nVmRSS:\t{(i + 1) * 2048} kB\n"
                                      f"RssAnon:\t{(i + 1) * 1024} kB\n")
            (d / "comm").write_text("python3\n")
            argv = ["python3", "-m", "operating.proactive", "--password", "s3cret"]
            (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
            (d / "cgroup").write_text("0::/system.slice/plx-proactive.service\n")
        (proc / "self").mkdir(parents=True, exist_ok=True)
        (proc / "999").mkdir()                    # vanished mid-read: skipped
        return proc, cg

    def test_leaf_groups_and_processes_by_memory_reclaim_cannot_free(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n), tempfile.TemporaryDirectory() as tmp:
                proc, cg = self.build(Path(tmp), n)
                found = ops_alarms.memory_consumers(proc, cg)
                text = ops_alarms.consumers_text(found)
                names = [g["name"] for g in found["cgroups"]]
                if os.geteuid() != 0:   # root reads a mode-0 file
                    self.assertEqual(names, ["postgresql@17-main.service",
                                             "plx-cache.service"])
                self.assertNotIn("system.slice", names)
                self.assertEqual(found["cgroups"][0]["gb"], round(3100 / 1024, 2))
                pids = [p["pid"] for p in found["processes"]]
                self.assertEqual(pids, [999 + n - k for k in range(min(n, 3))])
                self.assertEqual(found["processes"][0]["gb"], round(n / 1024, 2))
                self.assertEqual(found["processes"][0]["cgroup"], "plx-proactive.service")
                self.assertIn("python3 -m operating.proactive (pid", text)
                self.assertNotIn("s3cret", text)
                self.assertNotIn("password", text)

    def test_nothing_readable_is_said_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            found = ops_alarms.memory_consumers(Path(tmp) / "none", Path(tmp) / "none")
        self.assertEqual(found, {"cgroups": [], "processes": []})
        self.assertEqual(ops_alarms.consumers_text(found), "top consumers unreadable")


class CarriedSourcesTest(unittest.TestCase):
    """Sage and FileMaker (Portal mirror lineages) and calendar and Planner (meetings run
    kinds) each have their own age, bounded by their carrier's."""

    def test_each_carried_source_breaches_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_state(Path(tmp))
            conn = sqlite3.connect(root / "host" / "host.sqlite")
            conn.executemany("INSERT INTO source_state VALUES (?, ?, NULL, 1)",
                             [("portal", "2030-01-02T11:00:00Z"),
                              ("meetings", "2030-01-02T11:00:00Z")])
            conn.commit()
            conn.close()
            mirror = root / ops_health.MIRROR_PATH
            mirror.parent.mkdir(parents=True)
            conn = sqlite3.connect(mirror)
            conn.execute("CREATE TABLE relation_state (relation TEXT, last_refresh_at TEXT)")
            conn.executemany("INSERT INTO relation_state VALUES (?, ?)", [
                ("sage.oe_order_header", "2030-01-02T11:30:00Z"),
                ("public.order_entry", "2030-01-01T00:00:00Z"),        # FileMaker bronze
                ("public.app_sales_order", "2030-01-02T11:59:00Z")])   # the Portal's own
            conn.commit()
            conn.close()
            meetings = root / "meetings-capture" / "capture.db"
            meetings.parent.mkdir()
            conn = sqlite3.connect(meetings)
            conn.execute("CREATE TABLE runs (kind TEXT, state TEXT, finished_at TEXT)")
            conn.executemany("INSERT INTO runs VALUES (?, ?, ?)", [
                ("calendar_event", "complete", "2030-01-02T11:50:00Z"),
                ("planner_task", "complete", "2029-12-30T00:00:00Z"),
                ("planner_task", "in_progress", None)])
            conn.commit()
            conn.close()
            report = ops_health.collect(root, now=NOW, environ={}, include_api=False)
            got = {c["name"]: c["state"] for c in report["checks"]}
            self.assertEqual({k: got[k] for k in ("capture:sage", "capture:filemaker",
                                                  "capture:calendar", "capture:planner")},
                             {"capture:sage": "ok", "capture:filemaker": "breach",
                              "capture:calendar": "ok", "capture:planner": "breach"})
            alarm = ops_health.capture_alarm(report)
            self.assertIn("filemaker", alarm["stalled"])


class OpsStatusRouteTest(unittest.TestCase):
    def test_only_the_owner_reads_the_alarms(self) -> None:
        from fastapi.testclient import TestClient
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        from operator_api.mock import MockBackend

        class Owned(MockBackend):
            owner = False

            def _is_owner(self, reader):
                return self.owner

        with tempfile.TemporaryDirectory() as tmp:
            root = make_state(Path(tmp))
            fake = Fake(system=healthy_units() + [
                unit("plx-proactive.service", "failed", "failed", "oom-kill")])
            ops_alarms.run_pass(root, environ={}, probes=fake.probes(), dsn=None,
                                sender_factory=never)
            for backend_type in (MockBackend, Owned):   # no Refused
                backend = backend_type()
                backend.state_dir = root
                client = TestClient(create_app(backend, DevAuth(
                    fixture_identity("plx", ["staff:demo"]), mode="mock"),
                    secure_cookie=False))
                self.assertEqual(client.get("/api/ops/status").status_code, 401)
                client.post("/api/auth/dev-login", json={})
                found = client.get("/api/ops/status", headers={"X-Owner": "1"})
                self.assertEqual(found.status_code, 403)
                self.assertNotIn("plx-proactive", found.text)
            backend.owner = True
            body = client.get("/api/ops/status").json()
            self.assertTrue(body["running"])
            self.assertEqual([i["key"] for i in body["open"]],
                             ["unit:plx-proactive.service"])


if __name__ == "__main__":
    unittest.main()
