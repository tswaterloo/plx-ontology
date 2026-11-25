"""One release for every unit: the committed deploy files name no user or home directory,
every rendered unit runs from the current release, and plx-release builds, switches
atomically and rolls back."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
PG_ONLY_UNITS = (
    "plx-api", "plx-api@", "plx-api-check@", "plx-ai-watcher", "plx-autopilot-fast-serve",
    "plx-autopilot-follow", "plx-proactive", "plx-proactive-inc", "plx-proactive-nightly",
    "plx-proactive-whole", "plx-delta", "plx-delta-compact", "plx-ontology-pg-follow",
    "plx-search-follow", "plx-search-generation", "plx-rag-backfill", "plx-truth-check",
    "plx-alerts", "plx-comm-text", "plx-assist-readings", "plx-assist-readings-fresh", "plx-comms-indexes", "plx-daily-changes",
)
# The interpreter running the test is the release's Python (lib.sh defaults PLX_VENV to the
# served host's /data/venv, which a test runner elsewhere, a Spark, does not have).
VENV = str(Path(sys.executable).parents[1])
PERSONAL = re.compile(r"/home/[a-z_][a-z0-9_-]*|/Users/\w+|\btanush\b", re.IGNORECASE)


def tracked(folder: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", str(folder)], cwd=ROOT, text=True,
                         capture_output=True, check=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


class DeployFilesTest(unittest.TestCase):
    def test_whole_pass_is_capped_at_22g_in_the_unit_and_the_jobq_class(self) -> None:
        # AL-06, resized 11-16: 6 workers under a 22G cap, the high mark below it
        unit = (ROOT / "deploy/systemd/plx-proactive-whole.service").read_text()
        self.assertRegex(unit, r"(?m)^MemoryMax=22G$")
        self.assertRegex(unit, r"(?m)^MemoryHigh=20G$")
        self.assertTrue((ROOT / "deploy/systemd/plx-proactive-whole.timer").is_file())
        from operating.jobq import classes as jobq_classes
        self.assertEqual(jobq_classes.load({})["proactive-whole"].mem_gb, 22)

    def test_scheduled_passes_end_inside_their_timeout_and_fit_the_host(self) -> None:
        # 11-16: plx-proactive and -whole both failed Result=timeout at 4800 s with a 4200 s
        # budget; the pass's budget plus at least an hour's margin must sit inside the
        # unit's timeout, the pool must fit its cap, and a pass finding another running
        # must skip (exit 0), never wait on the lock
        for name in ("plx-proactive", "plx-proactive-whole", "plx-proactive-nightly"):
            unit = (ROOT / f"deploy/systemd/{name}.service").read_text()
            if name == "plx-proactive-whole":
                # the Spark transport carries the pass flags in its runner, not the unit
                runner = (ROOT / "deploy/spark/proactive_whole.sh").read_text()
                budget = float(re.search(r"budget=\$\{WHOLE_BUDGET_SECONDS:-(\d+)\}",
                                         runner).group(1))
                share = float(re.search(r"refresh=\$\{WHOLE_REFRESH_SECONDS:-(\d+)\}",
                                        runner).group(1))
            else:
                budget = float(re.search(r"--budget-seconds (\d+)", unit).group(1))
                share = float(re.search(r"--refresh-seconds (\d+)", unit).group(1))
            timeout = float(re.search(r"(?m)^TimeoutStartSec=(\d+)$", unit).group(1))
            workers = int(re.search(r"(?m)^Environment=PLX_PROACTIVE_WORKERS=(\d+)$",
                                    unit).group(1))
            cap = float(re.search(r"(?m)^MemoryMax=(\d+)G$", unit).group(1))
            high = float(re.search(r"(?m)^MemoryHigh=(\d+)G$", unit).group(1))
            self.assertLess(share, budget, name)
            self.assertGreaterEqual(timeout, budget + 3600, name)
            self.assertLess(timeout, 4 * 3600, name)   # ends before the next 4-hourly run
            self.assertGreaterEqual(workers, 4, name)
            self.assertGreaterEqual(cap, workers * 2.5 + 6, name)   # 2.5 GB/worker + engine
            self.assertLessEqual(cap, 24, name)   # the host: 61 GB, Postgres ~18 GB
            self.assertLess(high, cap, name)
            self.assertNotIn("--pass-wait-seconds", unit, name)   # default 0: skip at once

    def test_no_username_or_home_path_is_committed_under_deploy(self) -> None:
        found = [f"{p.relative_to(ROOT)}:{n}" for p in tracked(DEPLOY) if p.is_file()
                 for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1)
                 if PERSONAL.search(line)]
        self.assertEqual(found, [])

    def test_every_script_parses(self) -> None:
        for path in tracked(DEPLOY):
            if path.suffix == ".sh" or path.name in ("plx-release", "plx-mint-link"):
                subprocess.run(["bash", "-n", str(path)], check=True)

    def test_every_rendered_unit_runs_from_the_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, "deploy.env")
            env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
            for user in ([], ["--user"]):
                subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", f"{tmp}/out",
                                "--env", str(env), *user], check=True, capture_output=True)
            rendered = [p for p in Path(tmp, "out").rglob("*") if p.is_file()]
            self.assertTrue(any(p.name == "plx-api.service" for p in rendered))
            for path in rendered:
                text = path.read_text()
                self.assertNotIn("@PLX_", text, path)
                for wd in re.findall(r"^WorkingDirectory=(.*)$", text, re.M):
                    if path.parent.name == "plx-api-check@.service.d":
                        # the preflight checks the candidate release before the switch: its
                        # own drop-in, read after the API's copied ones, names it
                        if path.name == "zzzz-check.conf":
                            self.assertEqual(wd, "/srv/rel/%i", path)
                        continue
                    self.assertTrue(wd.startswith("/srv/rel/current"), (path, wd))
                for unit_path in re.findall(r"(?:/data/OntologyProject|/data/scratch/wt/)", text):
                    self.fail(f"{path} runs from a checkout: {unit_path}")

    def test_the_ingest_unit_spaces_restarts_and_stops_a_crash_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", f"{tmp}/out"],
                           check=True, capture_output=True)
            text = Path(tmp, "out", "plx-ingest.service").read_text()
            for line in ("StartLimitIntervalSec=600", "StartLimitBurst=5", "RestartSec=60",
                         "Restart=on-failure"):
                self.assertIn(line, text)
            self.assertLess(text.index("StartLimitBurst"), text.index("[Service]"))

    def test_proactive_units_keep_the_store_and_calling_refresh_and_name_no_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, "deploy.env")
            env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
            subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", f"{tmp}/out",
                            "--env", str(env)], check=True, capture_output=True)
            out = Path(tmp, "out")
            proactive = (out / "plx-proactive.service").read_text()
            self.assertIn("Environment=PLX_ONTOLOGY_PG=only", proactive)
            self.assertIn("--refresh calling", proactive)
            self.assertNotIn("--refresh always", proactive)
            for name in ("plx-proactive-inc.service", "plx-proactive-nightly.service"):
                text = (out / name).read_text()
                self.assertIn("User=someone", text, name)
                self.assertIn("WorkingDirectory=/srv/rel/current", text, name)
            inc = (out / "plx-proactive-inc.service").read_text()
            self.assertIn("ExecStart=/srv/rel/current/deploy/spark/proactive_inc.sh", inc)
            self.assertRegex(inc, r"(?m)^MemoryHigh=2G$")
            self.assertRegex(inc, r"(?m)^MemoryMax=3G$")
            self.assertIn("Environment=PLX_ONTOLOGY_PG=only", inc)
            inc_mem = (out / "plx-proactive-inc.service.d" / "memory.conf").read_text()
            self.assertRegex(inc_mem, r"(?m)^MemoryHigh=2G$")
            self.assertRegex(inc_mem, r"(?m)^MemoryMax=3G$")
            self.assertRegex((out / "plx-proactive-inc.service.d" / "zzz-limits.conf").read_text(),
                             r"(?m)^MemoryMax=3G$")
            inc_runner = (ROOT / "deploy/spark/proactive_inc.sh").read_text()
            self.assertIn("--prepare-dir", inc_runner)
            self.assertIn("--export-dir", inc_runner)
            self.assertIn("--apply-folds", inc_runner)
            self.assertIn("--pg standby", inc_runner)
            self.assertIn("--cleanup-ship", inc_runner)
            self.assertIn("INC_APPLY", inc_runner)
            self.assertIn("RELEASE.json", inc_runner)
            self.assertTrue((out / "plx-proactive-inc.path").is_file())
            self.assertTrue((out / "plx-proactive-nightly.timer").is_file())
            whole = (out / "plx-proactive-whole.service").read_text()
            self.assertIn("deploy/spark/proactive_whole.sh", whole)
            runner = (ROOT / "deploy/spark/proactive_whole.sh").read_text()
            self.assertIn("--refresh whole", runner)
            self.assertIn("Environment=PLX_ONTOLOGY_PG=only", whole)
            self.assertIn("User=someone", whole)
            self.assertIn("01,05,09,13,17,21:15:00",
                          (out / "plx-proactive-whole.timer").read_text())
            secret = (out / "plx-api.service.d" / "live-secret.conf").read_text()
            # required, never optional: a missing live secret must stop the API, not fall
            # back to the development session secret another env file sets
            self.assertIn("EnvironmentFile=/etc/plx/live-session.env", secret)
            self.assertNotIn("EnvironmentFile=-", secret)
            for path in out.rglob("*"):
                if path.is_file():
                    text = path.read_text()
                    self.assertNotIn(str(Path.home()), text, path)
                    self.assertNotIn("/data/OntologyProject", text, path)

    def test_the_search_followers_embedding_writes_are_batch_and_hour_bounded(self) -> None:
        unit = (ROOT / "deploy/systemd/plx-search-follow.service").read_text()
        self.assertIn("deploy/spark/search_follow.sh", unit)
        self.assertRegex(unit, r"(?m)^MemoryHigh=2G$")
        self.assertRegex(unit, r"(?m)^MemoryMax=3G$")
        self.assertRegex(unit, r"(?m)^Nice=10$")
        self.assertRegex(unit, r"(?m)^CPUWeight=15$")
        self.assertRegex(unit, r"(?m)^UMask=0077$")
        runner = (ROOT / "deploy/spark/search_follow.sh").read_text()
        self.assertIn("SEARCH_FOLLOW_EMBED_BATCH:-16", runner)
        self.assertIn("SEARCH_FOLLOW_EMBED_ROWS:-32", runner)
        self.assertIn("SEARCH_FOLLOW_EMBED_WRITE_MB_PER_HOUR:-256", runner)
        self.assertIn("SEARCH_FOLLOW_LAYER_SECONDS:-600", runner)
        self.assertIn("SEARCH_FOLLOW_MIN_FREE_GB:-60", runner)
        self.assertIn("--prepare-dir", runner)
        self.assertIn("--export-dir", runner)
        self.assertIn("--apply-dir", runner)
        self.assertIn("--pg standby", runner)
        self.assertIn("--cleanup-ship", runner)
        self.assertIn("SEARCH_FOLLOW_APPLY", runner)
        self.assertIn("RELEASE.json", runner)

    def test_the_search_follower_host_dropin_keeps_the_write_budget_effective(self) -> None:
        """The host drop-in must keep ExecStart on search_follow.sh (never falling back to
        in-process bge-m3 on the box) and keep the runner's bounded defaults active."""
        unit = (ROOT / "deploy/systemd/plx-search-follow.service").read_text()
        dropin = (ROOT / "deploy/systemd/host/plx-search-follow.service.d" /
                  "chunk-workers.conf").read_text()
        effective = ""
        for line in (*unit.splitlines(), *dropin.splitlines()):
            if line == "ExecStart=":
                effective = ""
            elif line.startswith("ExecStart="):
                effective = line.removeprefix("ExecStart=")
        self.assertIn("deploy/spark/search_follow.sh", effective)
        self.assertNotIn("operating.embed", effective)
        self.assertIn("SEARCH_FOLLOW_CHUNK_WORKERS=4", dropin)

    def test_upkeep_units_render_bounded_and_carry_no_secret(self) -> None:
        """The units that keep data current: each has Nice and MemoryMax, the ones that read
        people data load the exclusion env file, and none embeds a credential value."""
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, "deploy.env")
            env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
            for flag, out in ((["--user"], "u"), ([], "s")):
                subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", f"{tmp}/{out}",
                                "--env", str(env), *flag], check=True, capture_output=True)
            wanted = {"s": ["plx-sheet-readings", "plx-system-activity", "plx-team-activity",
                            "plx-work-graph", "plx-proactive-whole", "plx-backup", "plx-restore-test",
                            "plx-ops-health", "plx-labour"]}
            for out, names in wanted.items():
                for name in names:
                    service = Path(tmp, out, f"{name}.service").read_text()
                    self.assertRegex(service, r"(?m)^MemoryMax=", name)
                    self.assertRegex(service, r"(?m)^Nice=", name)
                    self.assertTrue(Path(tmp, out, f"{name}.timer").is_file(), name)
                    self.assertNotRegex(service, r"(?i)(SECRET|TOKEN|API_KEY|PASSWORD)=\S",
                                        name)
            moved = ["plx-sheet-readings", "plx-system-activity", "plx-team-activity",
                     "plx-work-graph"]
            for name in moved:
                service = Path(tmp, "s", f"{name}.service").read_text()
                self.assertIn("EnvironmentFile=-/srv/someone/.config/plx/proactive.env",
                              service, name)
                # 11-15: system units run as the Workbench user with its groups from
                # /etc/group (the capture stores the API user shares); a user manager that
                # predates the plx-state group ran them without it
                self.assertRegex(service, r"(?m)^User=someone$", name)
                self.assertRegex(service, r"(?m)^Group=someone$", name)
                self.assertFalse(Path(tmp, "u", f"{name}.service").exists(), name)
            # the work graph runs as the Workbench user with its groups (the capture stores
            # the API user shares), on Postgres only (no projection file since DI-02)
            graph = Path(tmp, "s", "plx-work-graph.service").read_text()
            self.assertRegex(graph, r"(?m)^User=someone$")
            self.assertIn("Environment=PLX_ONTOLOGY_PG=only", graph)
            self.assertIn("--source postgres", graph)
            # a system unit that reads the ontology store runs on Postgres only: without
            # it the job finds no projection file and its first run wipes its own output
            for name in ("plx-work-graph", "plx-sheet-readings"):
                self.assertIn("Environment=PLX_ONTOLOGY_PG=only",
                              Path(tmp, "s", f"{name}.service").read_text(), name)
            # DI-02: every unit that reads the ontology runs on Postgres only, so a rebuilt
            # host (without its zz-pg-only drop-ins) cannot wipe or fall back to a file
            for name in PG_ONLY_UNITS:
                text = (DEPLOY / "systemd" / f"{name}.service").read_text()
                self.assertEqual(re.findall(r"(?m)^Environment=PLX_ONTOLOGY_PG=(.*)$", text),
                                 ["only"], name)
            # CAP-L148: the labour-hours build is scheduled daily, reads the capture store
            # read-only and writes only the labour store
            labour = Path(tmp, "s", "plx-labour.service").read_text()
            self.assertIn("-m operating.labour_hours build --state-dir ${PLX_INGEST_STATE} "
                          "--out ${PLX_INGEST_STATE}/labour/labour.db", labour)
            self.assertRegex(labour, r"(?m)^Type=oneshot$")
            self.assertRegex(labour, r"(?m)^ProtectSystem=strict$")
            self.assertRegex(labour, r"(?m)^ReadOnlyPaths=/data/plx-state$")
            self.assertRegex(labour, r"(?m)^ReadWritePaths=/data/plx-state/labour$")
            self.assertIn("MC_MCP_API_KEY", labour)   # the business credentials are dropped
            # 11-16: the jobs that read the capture stores need the people exclusion from the
            # proactive env file (without it they refuse: excluded_people_oids_absent)
            for name in ("plx-labour", "plx-doc-style"):
                self.assertIn("EnvironmentFile=-/srv/someone/.config/plx/proactive.env",
                              Path(tmp, "s", f"{name}.service").read_text(), name)
            self.assertRegex(Path(tmp, "s", "plx-labour.timer").read_text(),
                             r"(?m)^OnCalendar=\*-\*-\* \d\d:\d\d:00 ")
            install = (DEPLOY / "ops" / "install_keep_fresh.sh").read_text()
            # plx-labour is one of the system units the installer enables (any position).
            self.assertIn("plx-labour", install.split('\nSYS_UNITS="', 1)[1].split('"', 1)[0]
                          .split())
            self.assertTrue(set(moved) <= set(install.split('\nSYS_UNITS="', 1)[1]
                                              .split('"', 1)[0].split()))
            self.assertNotIn("USER_UNITS=", install.replace("MOVED_USER_UNITS=", ""))
            activity = Path(tmp, "s", "plx-system-activity.service").read_text()
            self.assertIn("--weeks 13", activity)
            # the alert rules run after every successful business views refresh and on their
            # own timer, never ordered behind a refresh (11-18: an 80-min rebuild held the
            # start and every alert went stale), bounded, and the installer installs and
            # enables both
            views = Path(tmp, "s", "plx-business-views.service").read_text()
            self.assertRegex(views, r"(?m)^OnSuccess=plx-alerts\.service$")
            # owner 11-18: a build runs at full speed (no nice, CPU or IO caps), bounded by
            # memory and its disk floor only
            for name in ("plx-business-views",):
                unit = Path(tmp, "s", f"{name}.service").read_text()
                self.assertNotRegex(unit, r"(?m)^(Nice|CPUWeight|IOWeight|IOSchedulingClass)=",
                                    name)
                self.assertRegex(unit, r"(?m)^MemoryMax=", name)
            self.assertRegex(views, r"(?m)^Environment=PLX_BIZ_READ_WORKERS=8$")
            alerts = Path(tmp, "s", "plx-alerts.service").read_text()
            self.assertIn("-m operating.pg.alerts run", alerts)
            self.assertRegex(alerts, r"(?m)^Type=oneshot$")
            self.assertRegex(alerts, r"(?m)^MemoryMax=")
            self.assertRegex(alerts, r"(?m)^TimeoutStartSec=")
            self.assertNotRegex(alerts, r"(?m)^After=.*plx-business-views")
            self.assertRegex(Path(tmp, "s", "plx-alerts.timer").read_text(),
                             r"(?m)^OnCalendar=")
            self.assertIn("plx-alerts", install.split('\nSYS_UNITS="', 1)[1].split('"', 1)[0]
                          .split())
            # the comms text sidecar's refresh: its own timer, Spark runner (compute + apply), installed
            ct = Path(tmp, "s", "plx-comm-text.service").read_text()
            self.assertIn("deploy/spark/comm_text.sh", ct)
            ct_runner = (ROOT / "deploy/spark/comm_text.sh").read_text()
            self.assertIn("-m operating.pg.comm_text compute", ct_runner)
            self.assertIn("-m operating.pg.comm_text apply", ct_runner)
            self.assertRegex(ct, r"(?m)^Type=oneshot$")
            self.assertRegex(ct, r"(?m)^TimeoutStartSec=")
            self.assertRegex(Path(tmp, "s", "plx-comm-text.timer").read_text(),
                             r"(?m)^OnCalendar=\*:0/5$")
            self.assertIn("plx-comm-text", install.split('\nSYS_UNITS="', 1)[1].split('"', 1)[0]
                          .split())
            # every mail and Teams message is read after every refresh and on its own timer,
            # bounded, never ordered behind a refresh; the model is named (off: no read)
            self.assertRegex(views, r"(?m)^OnSuccess=plx-assist-readings\.service$")
            found = Path(tmp, "s", "plx-assist-readings.service").read_text()
            self.assertIn("-m operating.assist_readings run", found)
            for line in (r"^Type=oneshot$", r"^MemoryMax=", r"^TimeoutStartSec=",
                         r"^Environment=PLX_READINGS_MODEL=\w+$",
                         r"^EnvironmentFile=-.*proactive\.env$"):
                self.assertRegex(found, "(?m)" + line)
            self.assertNotRegex(found, r"(?m)^After=.*plx-business-views")
            self.assertRegex(Path(tmp, "s", "plx-assist-readings.timer").read_text(),
                             r"(?m)^OnCalendar=")
            self.assertIn("plx-assist-readings", install.split('\nSYS_UNITS="', 1)[1]
                          .split('"', 1)[0].split())

    def test_only_units_that_read_a_business_system_keep_its_credentials(self) -> None:
        """SEC-S-05: the shared env file holds the send-capable Graph app, the BC app and the
        MC key; a unit that reads no business system drops them, so a job's environment
        (and every child it starts) cannot send or write outside the Workbench's gates."""
        readers = {   # unit -> why it keeps the credentials
            "plx-ingest": "captures mail, meetings, SharePoint and BC",
            "plx-delta": "captures the sources' changes",
            "plx-ai-watcher": "runs the live host, whose resolver reads the sources",
            "plx-truth-check": "re-reads source records against the served values",
            "plx-expiry": "reads the credential files to report their expiry dates",
            "plx-write-readiness": "reads the apps' permission claims and BC's company list "
                                   "to record where a write could run",
        }
        dropped = {"AZURE_CLIENT_SECRET", "BC_CLIENT_SECRET", "MICROSOFT_GRAPH_CLIENT_SECRET",
                   "MC_MCP_API_KEY"}
        keeps_one = {   # unit -> the one credential it calls a business system with
            "plx-graph-subscriptions": "AZURE_CLIENT_SECRET",   # token + Graph /subscriptions
            "plx-org-capture": "MICROSOFT_GRAPH_CLIENT_SECRET",   # GET-only directory read
        }
        checked = []
        for path in sorted([*(DEPLOY / "systemd").glob("*.service"),
                            *(DEPLOY / "user").glob("*.service")]):
            text = path.read_text()
            if not re.search(r"(?m)^EnvironmentFile=-?\S*/plx\.env$", text):
                continue
            checked.append(path.stem)
            if path.stem in readers:
                continue
            unset = {name for line in re.findall(r"(?m)^UnsetEnvironment=(.*)$", text)
                     for name in line.split()}
            kept = {keeps_one[path.stem]} if path.stem in keeps_one else set()
            self.assertEqual(dropped - kept - unset, set(), path.stem)
            self.assertEqual(kept & unset, set(), path.stem)
        self.assertGreaterEqual(len(checked), 10, checked)   # the rule saw the units

    def test_caddy_has_no_tcp_admin_and_listens_only_on_loopback_and_tailscale(self) -> None:
        """SEC-S-15: the unauthenticated admin API is a caddy-only unix socket, never a TCP
        port any local account can reach to rebind the proxy."""
        import json
        import shutil
        text = (DEPLOY / "Caddyfile").read_text()
        admin = [line.split() for line in text.splitlines() if line.strip().startswith("admin")]
        self.assertEqual(len(admin), 1, admin)
        self.assertRegex(admin[0][1], r"^unix//\S+\|0[0-7]00$")
        caddy = shutil.which("caddy")
        if caddy is None:
            self.skipTest("caddy not installed: the text check above is all that runs")
        adapted = subprocess.run(
            [caddy, "adapt", "--config", str(DEPLOY / "Caddyfile"), "--adapter", "caddyfile"],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PLX_BIND_ADDRS": "192.0.2.9", "PLX_APP_HOST": "wb.example"})
        config = json.loads(adapted.stdout)
        self.assertTrue(config["admin"]["listen"].startswith("unix/"), config["admin"])
        for name, server in config["apps"]["http"]["servers"].items():
            for listen in server["listen"]:
                self.assertRegex(listen, r"^(127\.0\.0\.1|192\.0\.2\.9):(80|443)$", name)

    def test_install_script_renders_and_verifies_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, "deploy.env")
            env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
            # systemd-analyze --user needs the user's runtime directory, which a system
            # service (the nightly suite) is not given: name the user's own when it exists
            runtime = {} if os.environ.get("XDG_RUNTIME_DIR") or not Path(
                f"/run/user/{os.getuid()}").is_dir() else {
                "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
            # A host whose systemd-analyze reports something on a known-good unit (a Spark: a
            # root-only unit file of its own in /etc/systemd/system) cannot judge ours there
            probe = Path(tmp, "plx-probe.service")
            probe.write_text("[Unit]\nDescription=probe\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
            noise = subprocess.run(["systemd-analyze", "verify", str(probe)], capture_output=True,
                                   text=True, env={**os.environ, **runtime, "HOME": tmp})
            if noise.returncode == 0 and (noise.stdout + noise.stderr).strip():
                self.skipTest("this host's systemd-analyze reports on a known-good unit: "
                              + (noise.stdout + noise.stderr).strip()[:300])
            run = subprocess.run(["bash", str(DEPLOY / "ops" / "install_keep_fresh.sh"),
                                  "--dry-run"], capture_output=True, text=True,
                                 env={**os.environ, **runtime, "PLX_DEPLOY_ENV": str(env),
                                      "HOME": tmp})
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("nothing installed", run.stdout)
            self.assertFalse(Path(tmp, ".config", "systemd").exists())


class ApiUserTest(unittest.TestCase):
    """SEC-S-07: the API's own user sees a release it cannot change, none of the owner's home
    but the host config, no worktree, backup or Postgres data file, and the rest of the
    state read-only; the switch is its own render, never part of the release units."""

    def render(self, tmp: str, *flags: str) -> Path:
        env = Path(tmp, "deploy.env")
        env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
        out = Path(tmp, "out" + "".join(flags))
        subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", str(out), "--env",
                        str(env), *flags], check=True, capture_output=True)
        return out

    def test_the_api_user_drop_in_is_rendered_only_when_asked_and_confines_the_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = self.render(tmp)
            self.assertFalse((plain / "plx-api.service.d" / "zz-api-user.conf").exists())
            self.assertFalse((plain / "plx-api-trial.service.d").exists())
            out = self.render(tmp, "--api-user")
            text = (out / "plx-api.service.d" / "zz-api-user.conf").read_text()
            lines = dict(re.findall(r"(?m)^(\w+)=(.*)$", text))
            self.assertEqual(lines["User"], "plx-workbench")
            self.assertEqual(lines["ProtectHome"], "tmpfs")
            self.assertEqual(lines["ProtectSystem"], "strict")
            self.assertEqual(lines["CapabilityBoundingSet"], "CAP_DAC_READ_SEARCH")
            self.assertEqual(lines["WorkingDirectory"], "/srv/rel/current")
            self.assertEqual(re.findall(r"(?m)^ReadWritePaths=(.*)$", text), ["/data/plx-state"])
            hidden = " ".join(re.findall(r"(?m)^InaccessiblePaths=(.*)$", text))
            for path in ("/data/scratch", "/data/backups", "/data/OntologyProject", "/data/pg",
                         "/etc/shadow", "/root"):
                self.assertIn(path, hidden)
            for bound in re.findall(r"(?m)^BindReadOnlyPaths=-?(.*)$", text):
                self.assertTrue(bound.startswith("/srv/someone/.config/plx/workbench-host")
                                or bound.startswith("/srv/someone/.cache/plx/pg-facts"), bound)
            self.assertNotIn("EnvironmentFile=/etc/plx/live-session", text)
            for secret in ("/etc/plx/live-session.env", "/etc/plx/api-model.env",
                           "/etc/plx/mint-readers.env"):
                self.assertIn(secret, hidden)
            # the read capability opens by handle past every mount: refused
            self.assertIn("SystemCallFilter=~open_by_handle_at name_to_handle_at", text)
            trial = (out / "plx-api-trial.service.d" / "zzz-trial.conf").read_text()
            self.assertIn("--port 8792", trial)
            self.assertIn("UnsetEnvironment=PLX_ACTIONS_LIVE", trial)
            self.assertNotIn("8780", trial)

    def test_the_setup_check_names_a_shared_file_the_api_could_not_write(self) -> None:
        import grp
        import getpass
        me, group = getpass.getuser(), grp.getgrgid(os.getgid()).gr_name
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "state")
            (state / "feedback").mkdir(parents=True)
            os.chmod(state, 0o3770)
            os.chmod(state / "feedback", 0o2770)
            db = state / "feedback" / "feedback.sqlite"
            db.write_text("x")
            os.chmod(db, 0o660)
            sysctl = Path(tmp, "sys", "fs")
            sysctl.mkdir(parents=True)
            (sysctl / "protected_regular").write_text("1\n")
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_STATE": str(state),
                   "PLX_API_USER": me, "PLX_STATE_GROUP": group, "PLX_USER": me,
                   "PLX_SYSCTL_ROOT": str(Path(tmp, "sys"))}
            tool = str(DEPLOY / "release" / "api_user_setup.sh")

            def check() -> subprocess.CompletedProcess:
                return subprocess.run(["bash", tool, "--check"], env=env, text=True,
                                      capture_output=True)
            ok = check()
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            os.chmod(db, 0o600)                 # a job wrote it with a private umask
            drift = check()
            self.assertEqual(drift.returncode, 1)
            self.assertIn("feedback: 1 entries", drift.stdout)
            os.chmod(db, 0o660)
            # grants-1115: at 2 (Ubuntu's default) a job's O_CREAT open of the -wal/-shm the
            # API created in a sticky shared directory is refused: the job's store goes
            # read-only ("attempt to write a readonly database")
            (sysctl / "protected_regular").write_text("2\n")
            strict = check()
            self.assertEqual(strict.returncode, 1)
            self.assertIn("fs.protected_regular=2", strict.stdout)
            (sysctl / "protected_regular").write_text("1\n")
            self.assertEqual(check().returncode, 0)
            # the sessions store in the sticky root: only its owner, the API's user, can
            # replace it (a dev API run as the host user leaves it unwritable for the API)
            store = state / "app-sessions.json"
            store.write_text("{}")
            os.chmod(store, 0o660)
            self.assertEqual(check().returncode, 0, check().stdout)
            other = subprocess.run(["bash", tool, "--check"], text=True, capture_output=True,
                                   env={**env, "PLX_API_USER": "root"})
            self.assertIn(f"app-sessions.json: owned by {me}, not root", other.stdout)
            store.unlink()
            os.chmod(state, 0o1770)             # no setgid: the API's files there get its group
            self.assertEqual(check().returncode, 1)
            os.chmod(state, 0o2770)             # no sticky bit
            self.assertIn("sticky", check().stdout)
            dry = subprocess.run(["bash", tool], env=env, text=True, capture_output=True)
            self.assertNotEqual(dry.returncode, 0)          # changes need root
            self.assertIn("root", dry.stderr)

    def test_the_mint_wrapper_refuses_anyone_but_root(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("runs as root")
        run = subprocess.run([str(DEPLOY / "ops" / "plx-mint-link")], capture_output=True,
                             text=True, env={**os.environ, "SUDO_USER": "someone"})
        self.assertEqual(run.returncode, 2)
        self.assertIn("sudo", run.stderr)
        self.assertEqual(run.stdout, "")


class RoleWalkTest(unittest.TestCase):
    """deploy/pg/role_walk.py: a denial in the Postgres log becomes the grant to add, a file
    the API's user may not write is named, and the walk never targets the live API or runs
    without the people exclusion."""

    def setUp(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("role_walk", DEPLOY / "pg" / "role_walk.py")
        self.walk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.walk)

    def test_an_expired_session_signs_in_again_and_the_route_is_walked(self) -> None:
        """A walk on a busy host outlives its 30-minute ticket: a 401 signs the client in
        again once and repeats the route, so later routes are walked, not read as 401."""
        import http.server
        import threading
        state = {"session": False, "logins": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def _send(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self) -> None:
                self._send(200 if state["session"] else 401)

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                state["logins"] += 1
                state["session"] = True
                self._send(200)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = self.walk.Client(f"http://127.0.0.1:{server.server_address[1]}")
            self.assertEqual(client.call("/api/home")[0], 401)          # no re-login wired
            client.relogin = lambda: client.call("/api/auth/dev-login", {"ticket": "t"})[0] == 200
            self.assertEqual(client.call("/api/home")[0], 200)
            self.assertEqual(state["logins"], 1)
            state["session"] = False                                     # expired; refused again
            client.relogin = lambda: False
            self.assertEqual(client.call("/api/home")[0], 401)
        finally:
            server.shutdown()
            server.server_close()

    def test_denials_become_grants_and_file_refusals_name_the_path(self) -> None:
        log = ('2026-11-13 05:24:18.270 UTC [1] x@plx ERROR:  permission denied for table '
               'query_stats\n... ERROR:  permission denied for sequence search.x_id_seq\n'
               '... ERROR:  permission denied for schema plx_meta\n'
               '... ERROR:  canceling statement due to statement timeout\n')
        found = self.walk.denials(log)
        self.assertEqual(found, [("schema", "plx_meta"), ("sequence", "search.x_id_seq"),
                                 ("table", "query_stats")])
        self.assertEqual(self.walk.grants(found, "plx_api"), [
            "GRANT USAGE ON SCHEMA plx_meta TO plx_api;",
            "GRANT SELECT ON SEQUENCE search.x_id_seq TO plx_api;",
            "GRANT SELECT ON query_stats TO plx_api;"])
        files = self.walk.file_denials(
            "PermissionError: [Errno 13] Permission denied: '/data/plx-state/view-as/audit.jsonl'"
            "\nsqlite3.OperationalError: attempt to write a readonly database\n")
        self.assertIn("PermissionError: /data/plx-state/view-as/audit.jsonl", files)
        self.assertIn("attempt to write a readonly database", files)

    def test_records_are_found_in_suggestions_and_page_parts(self) -> None:
        payload = {"suggestions": [{"object_id": "a", "type": "Email", "label": "x"}],
                   "late": {"rows": [{"object_type": "PurchaseOrder", "object_id": "b"}]}}
        self.assertEqual(sorted(self.walk.records(payload)),
                         [("Email", "a"), ("PurchaseOrder", "b")])

    def test_the_synthetic_reader_lives_in_a_copy_and_holds_no_grant(self) -> None:
        """SEC-S-14's non-owner walk signs in a made-up L1 employee: added to a private copy
        of the host config only (the real files are untouched), never full-read, and the
        copy reads no directory."""
        import json
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp, "real")
            real.mkdir()
            Path(real, "host.json").write_text(json.dumps({
                "entra_directory": True, "portal_access": "portal_access.json",
                "employee_policy": "employee_policy.json"}))
            Path(real, "portal_access.json").write_text(json.dumps({"users": [{"id": "o"}]}))
            policy = {"entra_tenant_id": "t", "full_read": [{"object_id": "owner"}],
                      "principals": {"staff:x": {"department_ids": ["d1"]}}}
            Path(real, "employee_policy.json").write_text(json.dumps(policy))
            before = {p.name: p.read_text() for p in real.iterdir()}
            work = Path(tmp, "work")
            work.mkdir()
            host, claims = self.walk.synthetic_host(Path(real, "host.json"),
                                                    {"email": "owner@example.com"}, work)
            self.assertEqual({p.name: p.read_text() for p in real.iterdir()}, before)
            self.assertFalse(json.loads(host.read_text())["entra_directory"])
            users = json.loads(Path(host.parent, "portal_access.json").read_text())["users"]
            self.assertEqual(len(users), 2)
            added = users[-1]
            self.assertEqual((added["accessLevel"], added["role"], added["departmentIds"]),
                             ("L1", "STAFF", ["d1"]))
            self.assertEqual(added["entraObjectId"], claims["entraObjectId"])
            self.assertTrue(claims["email"].endswith("@example.com"))
            full = json.loads(Path(host.parent, "employee_policy.json").read_text())["full_read"]
            self.assertNotIn(claims["entraObjectId"], [g["object_id"] for g in full])

    def test_the_walk_proves_every_write_the_role_grants(self) -> None:
        """Each table the role file lets the API write is written by the walk's rolled-back
        write check, and the walk's synthetic reader carries a Portal role the access layer
        knows (an unknown role refuses the mint: the non-owner walk never ran)."""
        sql = (DEPLOY / "pg" / "plx_api_role.sql").read_text()
        granted = set(re.findall(r"GRANT (?:INSERT|UPDATE|DELETE)[A-Z, ]* ON ([a-z_.]+) TO", sql))
        self.assertTrue(granted)
        writes = " ".join(self.walk.WRITES)
        for table in granted:
            self.assertIn(table, writes)
        self.assertIn("temp_file_limit", writes)
        from operating.employee_permissions_portal import EMPLOYEE_ROLES
        self.assertIn("STAFF", EMPLOYEE_ROLES)

    def test_the_walk_refuses_the_live_api_and_a_missing_exclusion(self) -> None:
        for argv in (["--port", "8780"], ["--api-url", "http://127.0.0.1:8780"]):
            with self.assertRaises(SystemExit) as stop:
                self.walk.main(argv)
            self.assertIn("live API", str(stop.exception))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "workbench-session.env").write_text("PLX_WORKBENCH_SESSION_SECRET=x\n")
            self.walk.CONFIG = Path(tmp)
            self.walk.live_unit_settings = lambda: {}
            with self.assertRaises(SystemExit) as stop:
                self.walk.api_env("postgresql:///plx?options=-c%20role%3Dplx_api", base={})
            self.assertIn("PLX_EXCLUDED_PEOPLE_OIDS", str(stop.exception))
            env = self.walk.api_env("postgresql:///plx?options=-c%20role%3Dplx_api", base={
                "PLX_EXCLUDED_PEOPLE_OIDS": "a", "AZURE_CLIENT_SECRET": "s",
                "BC_CLIENT_SECRET": "s", "PLX_ACTIONS_LIVE": "1", "MC_MCP_API_KEY": "k",
                "FIIX_API_KEY": "k", "FREIGHTCOM_TOKEN": "t", "CLAUDE_CODE_OAUTH_TOKEN": "t",
                "SOME_PASSWORD": "p"})
            for name in ("FIIX_API_KEY", "FREIGHTCOM_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                         "SOME_PASSWORD"):
                self.assertNotIn(name, env)
            self.assertEqual(env["PLX_WORKBENCH_SESSION_SECRET"], "x")    # the dev secret
            self.assertEqual(env["PLX_EXCLUDED_PEOPLE_OIDS"], "a")
            self.assertNotIn("AZURE_CLIENT_SECRET", env)
            self.assertNotIn("BC_CLIENT_SECRET", env)
            self.assertNotIn("MC_MCP_API_KEY", env)
            self.assertEqual(env["PLX_ACTIONS_LIVE"], "0")
            self.assertEqual(env["PGOPTIONS"], "-c role=plx_api")


class ReleaseTest(unittest.TestCase):
    def test_build_switch_and_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for text in ("one", "two"):
                Path(repo, "code.txt").write_text(text)
                subprocess.run([*git, "add", "-A"], check=True)
                subprocess.run([*git, "commit", "-qm", text], check=True)
            Path(tmp, "state").mkdir()
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_REPO": str(repo),
                   "PLX_RELEASES": f"{tmp}/rel", "PLX_STATE": f"{tmp}/state",
                   "PLX_RELEASE_OWNER": "", "PLX_VENV": VENV}
            tool = str(DEPLOY / "release" / "plx-release")

            def run(*args: str) -> str:
                return subprocess.run([tool, *args], env=env, text=True, capture_output=True,
                                      check=True).stdout.strip().splitlines()[-1]

            first = run("build", "HEAD~1", "--no-app")
            second = run("build", "HEAD", "--no-app")
            self.assertEqual(run("build", "HEAD", "--no-app"), second)  # built once
            run("switch", first)
            run("switch", second)
            current = Path(tmp, "rel", "current")
            self.assertEqual(Path(current, "code.txt").read_text(), "two")
            self.assertEqual(os.readlink(Path(tmp, "rel", "previous")), first)
            run("rollback")
            self.assertEqual(Path(current, "code.txt").read_text(), "one")
            self.assertEqual(os.readlink(current), first)
            refused = subprocess.run([tool, "switch", "nope"], env=env, capture_output=True)
            self.assertNotEqual(refused.returncode, 0)

    def test_a_build_carries_the_trees_derived_code_versions(self) -> None:
        """An API started on a release reads its derived-code versions from the release
        (DERIVED-CODE.json, written in the tree before it is sealed) instead of parsing its
        code before the first page; a tree whose computation fails still builds."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            Path(repo, "operating").mkdir(parents=True)
            Path(repo, "operating", "__init__.py").write_text("")
            Path(repo, "operating", "derived_status.py").write_text(
                "import json, os, sys\n"
                "assert sys.argv[1:] == ['versions', '--release'], sys.argv\n"
                "json.dump({'cwd': os.getcwd()}, open('DERIVED-CODE.json', 'w'))\n")
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", "one"], check=True)
            Path(tmp, "state").mkdir()
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_REPO": str(repo),
                   "PLX_RELEASES": f"{tmp}/rel", "PLX_STATE": f"{tmp}/state",
                   "PLX_RELEASE_OWNER": "", "PLX_VENV": VENV}
            tool = str(DEPLOY / "release" / "plx-release")
            built = subprocess.run([tool, "build", "HEAD", "--no-app"], env=env, text=True,
                                   capture_output=True, check=True).stdout.strip().splitlines()[-1]
            doc = json.loads(Path(tmp, "rel", built, "DERIVED-CODE.json").read_text())
            self.assertNotEqual(Path(doc["cwd"]).name, built)   # computed before the rename
            Path(repo, "operating", "derived_status.py").write_text("raise SystemExit(3)\n")
            subprocess.run([*git, "commit", "-qam", "two"], check=True)
            failed = subprocess.run([tool, "build", "HEAD", "--no-app"], env=env, text=True,
                                    capture_output=True, check=True)
            self.assertIn("not precomputed", failed.stderr)
            self.assertFalse(Path(tmp, "rel", failed.stdout.strip().splitlines()[-1],
                                  "DERIVED-CODE.json").exists())

    def test_an_unchanged_app_tree_reuses_the_current_build(self) -> None:
        """Process sweep 11-07: a release whose app/ tree equals the current release's copies
        that release's .next instead of running `next build`; any app/ change builds."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            Path(repo, "app", "src").mkdir(parents=True)
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            Path(repo, "app", "package.json").write_text("{}")
            Path(repo, "app", "package-lock.json").write_text("{}")
            node = Path(tmp, "node", "bin")
            node.mkdir(parents=True)
            builds = Path(tmp, "builds.log")
            Path(node, "node").write_text(
                f"#!/bin/sh\nmkdir -p .next && date +%s%N > .next/BUILD_ID && "
                f"echo built >> {builds}\n")
            Path(node, "npm").write_text("#!/bin/sh\nmkdir -p node_modules\n")
            for f in ("node", "npm"):
                Path(node, f).chmod(0o755)

            def commit(path: str, text: str) -> None:
                Path(repo, path).write_text(text)
                subprocess.run([*git, "add", "-A"], check=True)
                subprocess.run([*git, "commit", "-qm", text], check=True)

            Path(tmp, "state").mkdir()
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_REPO": str(repo),
                   "PLX_RELEASES": f"{tmp}/rel", "PLX_STATE": f"{tmp}/state",
                   "PLX_RELEASE_OWNER": "", "PLX_NODE": str(node.parent), "PLX_VENV": VENV}
            tool = str(DEPLOY / "release" / "plx-release")

            def run(*args: str) -> str:
                return subprocess.run([tool, *args], env=env, text=True, capture_output=True,
                                      check=True).stdout.strip().splitlines()[-1]

            def profile(rid: str) -> dict:
                return json.loads(Path(tmp, "rel", rid, "RELEASE.json").read_text())["profile"]

            commit("app/src/page.tsx", "one")
            first = run("build", "HEAD")
            run("switch", first)
            self.assertEqual(builds.read_text().count("built"), 1)
            commit("code.txt", "server only")            # app/ unchanged
            second = run("build", "HEAD")
            self.assertEqual(builds.read_text().count("built"), 1)   # reused, not rebuilt
            self.assertEqual(Path(tmp, "rel", second, "app", ".next", "BUILD_ID").read_text(),
                             Path(tmp, "rel", first, "app", ".next", "BUILD_ID").read_text())
            self.assertEqual(profile(second)["app_tree"], profile(first)["app_tree"])
            self.assertTrue(profile(second)["app"])
            run("switch", second)
            commit("app/src/page.tsx", "two")            # app/ changed: built
            third = run("build", "HEAD")
            self.assertEqual(builds.read_text().count("built"), 2)
            self.assertNotEqual(profile(third)["app_tree"], profile(second)["app_tree"])

    def test_a_sealed_release_is_built_unprivileged_and_never_switched_to_once_writable(self
                                                                                        ) -> None:
        """SEC-S-12: with PLX_RELEASE_OWNER set, the build runs in the caller's staging
        directory and the release it renames into place has nothing writable by group or
        others; a release someone made writable afterwards is refused at switch. (The owner
        is the test's own user here, so no sudo runs; on the host it is root.)"""
        import getpass
        import stat
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            Path(repo, "code.txt").write_text("one")
            Path(repo, "run.sh").write_text("#!/bin/sh\n")
            Path(repo, "run.sh").chmod(0o775)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", "one"], check=True)
            rel = Path(tmp, "rel")
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_REPO": str(repo),
                   "PLX_RELEASES": str(rel), "PLX_STATE": f"{tmp}/state",
                   "PLX_RELEASE_OWNER": getpass.getuser(), "PLX_VENV": VENV}
            tool = str(DEPLOY / "release" / "plx-release")

            def run(*args: str) -> subprocess.CompletedProcess:
                # a group-writable umask: the seal, not the caller's umask, makes it read-only
                return subprocess.run(["bash", "-c", 'umask 002; exec "$@"', "-", tool, *args],
                                      env=env, text=True, capture_output=True)

            no_staging = run("build", "HEAD", "--no-app")
            self.assertNotEqual(no_staging.returncode, 0)
            self.assertIn(".staging", no_staging.stderr)
            Path(rel, ".staging").mkdir(parents=True)
            built = run("build", "HEAD", "--no-app")
            self.assertEqual(built.returncode, 0, built.stderr)
            rid = built.stdout.strip().splitlines()[-1]
            self.assertEqual(list(Path(rel, ".staging").iterdir()), [])   # staging left empty
            for path in [Path(rel, rid), *Path(rel, rid).rglob("*")]:
                mode = path.stat().st_mode
                self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH), path)
                self.assertTrue(mode & stat.S_IROTH, path)       # every unit's user reads it
            self.assertTrue(Path(rel, rid, "run.sh").stat().st_mode & stat.S_IXOTH)
            self.assertEqual(run("verify", rid).returncode, 0)
            Path(rel, rid, "code.txt").chmod(0o666)                # tampered after the seal
            self.assertNotEqual(run("verify", rid).returncode, 0)
            refused = run("switch", rid)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("not sealed", refused.stderr)
            self.assertFalse(Path(rel, "current").exists())
            Path(rel, rid, "code.txt").chmod(0o644)
            self.assertEqual(run("switch", rid).returncode, 0)
            self.assertEqual(Path(rel, "current", "code.txt").read_text(), "one")

    def test_a_build_reuses_a_release_only_as_asked_and_a_failed_inspection_refuses(self) -> None:
        """OPS-002: a release built without the app or without the schema check is not
        reused by a build that wants them. OPS-005: a served projection that cannot be
        read refuses the build, never counts as no projection."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            Path(repo, "app").mkdir(parents=True)
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            Path(repo, "app", "package.json").write_text("{}")
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", "one"], check=True)
            state = Path(tmp, "state")
            Path(state, "projection").mkdir(parents=True)
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_REPO": str(repo),
                   "PLX_RELEASES": f"{tmp}/rel", "PLX_STATE": str(state),
                   "PLX_RELEASE_OWNER": "", "PLX_VENV": VENV}
            tool = str(DEPLOY / "release" / "plx-release")

            def run(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run([tool, *args], env=env, text=True, capture_output=True)

            store = Path(state, "projection", "ontology.sqlite")
            store.write_bytes(b"not a database")
            refused = run("build", "HEAD", "--no-app")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("could not be read", refused.stderr)
            self.assertFalse(Path(tmp, "rel", "current").exists())
            built = run("build", "HEAD", "--no-app", "--no-schema-check")
            self.assertEqual(built.returncode, 0, built.stderr)
            rid = built.stdout.strip().splitlines()[-1]
            meta = json.loads(Path(tmp, "rel", rid, "RELEASE.json").read_text())
            self.assertEqual(meta["profile"], {"app": False, "schema_checked": False,
                                              "app_tree": None})
            again = run("build", "HEAD", "--no-schema-check")     # wants the app
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("without the app", again.stderr)
            store.unlink()
            with sqlite3.connect(store) as db:
                db.execute("CREATE TABLE store_meta (key TEXT, value TEXT)")
                db.execute("INSERT INTO store_meta VALUES ('schema_digest', 'served-digest')")
            checked = run("build", "HEAD", "--no-app")      # built unchecked: not reused
            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("not reused", checked.stderr)
            self.assertEqual(run("build", "HEAD", "--no-app", "--no-schema-check")
                             .stdout.strip().splitlines()[-1], rid)


    def test_in_postgres_mode_the_build_gates_on_the_release_s_own_postgres_check(self) -> None:
        """DI-02: with PLX_ONTOLOGY_PG=only the SQLite generation is not served; the release's
        own `operating.pg.ontology_migrate check` must say ok (Postgres holds its schema and
        step code), else nothing is built. A stale SQLite store is not read."""
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            Path(repo, "operating", "pg").mkdir(parents=True)
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            check = Path(repo, "operating", "pg", "ontology_migrate.py")
            for name in ("operating/__init__.py", "operating/pg/__init__.py"):
                Path(repo, name).write_text("")
            for ok in ("False", "True"):
                check.write_text("import json, os\n"
                                 "assert os.environ.get('PLX_PG_DSN') == 'postgresql:///t'\n"
                                 f"print(json.dumps({{'ok': {ok}, 'reason': 'step_code_differs',"
                                 " 'code': 'c', 'served': 's'}))\n")
                subprocess.run([*git, "add", "-A"], check=True)
                subprocess.run([*git, "commit", "-qm", ok], check=True)
            state = Path(tmp, "state")
            Path(state, "projection").mkdir(parents=True)
            Path(state, "projection", "ontology.sqlite").write_bytes(b"stale, not served")
            venv = Path(tmp, "venv", "bin")
            venv.mkdir(parents=True)
            Path(venv, "python3").symlink_to(sys.executable)
            env = {k: v for k, v in os.environ.items() if not k.startswith("PLX_")}
            env.update({"PLX_DEPLOY_ENV": "/nonexistent", "PLX_REPO": str(repo),
                        "PLX_RELEASES": f"{tmp}/rel", "PLX_STATE": str(state),
                        "PLX_RELEASE_OWNER": "", "PLX_VENV": str(Path(tmp, "venv")),
                        "PLX_CONFIG": f"{tmp}/cfg", "PLX_ONTOLOGY_PG": "only",
                        "PLX_PG_DSN": "postgresql:///t"})
            tool = str(DEPLOY / "release" / "plx-release")

            def run(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run([tool, *args], env=env, text=True, capture_output=True)

            refused = run("build", "HEAD~1", "--no-app")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("step_code_differs", refused.stderr)
            self.assertFalse(any(Path(tmp, "rel").glob("[0-9a-f]*")))
            built = run("build", "HEAD", "--no-app")
            self.assertEqual(built.returncode, 0, built.stderr)
            rid = built.stdout.strip().splitlines()[-1]
            meta = json.loads(Path(tmp, "rel", rid, "RELEASE.json").read_text())
            self.assertTrue(meta["profile"]["schema_checked"])
            self.assertEqual(run("build", "HEAD", "--no-app").stdout.strip().splitlines()[-1],
                             rid)                                   # reused: still ok
            env.pop("PLX_PG_DSN")
            no_dsn = run("build", "HEAD~1", "--no-app")
            self.assertNotEqual(no_dsn.returncode, 0)
            self.assertIn("no PLX_PG_DSN", no_dsn.stderr)


    def test_prune_keeps_current_previous_newest_and_busy_and_survives_a_vanished_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rel = Path(tmp, "rel")
            ids = [f"r{i}" for i in range(6)]  # r0 oldest
            for i, rid in enumerate(ids):
                Path(rel, rid).mkdir(parents=True)
                Path(rel, rid, "RELEASE.json").write_text("{}")
                os.utime(Path(rel, rid), (1_000_000 + i, 1_000_000 + i))
            os.symlink("r1", rel / "current")
            os.symlink("r0", rel / "previous")
            proc = Path(tmp, "proc")
            Path(proc, "10").mkdir(parents=True)
            os.symlink(str(rel / "r2" / "app"), proc / "10" / "cwd")  # a process runs in r2
            Path(proc, "99").mkdir()  # exited mid-scan: its cwd can no longer be read
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_RELEASES": str(rel),
                   "PLX_STATE": f"{tmp}/state", "PLX_RELEASE_OWNER": "", "PLX_PROC": str(proc)}
            out = subprocess.run([str(DEPLOY / "release" / "plx-release"), "prune", "--keep", "2"],
                                 env=env, text=True, capture_output=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(sorted(p.name for p in rel.iterdir() if not p.is_symlink()
                                    and p.name.startswith("r")), ["r0", "r1", "r2", "r4", "r5"])
            self.assertEqual(out.stdout.split(), ["pruned", "r3"])


    def test_prune_with_no_readable_process_prunes_nothing(self) -> None:
        # the API's processes run as their own user: a caller who can read none of the
        # working directories must not take "no process" for "no release in use"
        with tempfile.TemporaryDirectory() as tmp:
            rel = Path(tmp, "rel")
            for i, rid in enumerate(f"r{i}" for i in range(5)):
                Path(rel, rid).mkdir(parents=True)
                Path(rel, rid, "RELEASE.json").write_text("{}")
                os.utime(Path(rel, rid), (1_000_000 + i, 1_000_000 + i))
            os.symlink("r4", rel / "current")
            os.symlink("r3", rel / "previous")
            proc = Path(tmp, "proc")
            Path(proc, "10").mkdir(parents=True)   # its cwd unreadable to the caller
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_RELEASES": str(rel),
                   "PLX_STATE": f"{tmp}/state", "PLX_RELEASE_OWNER": "", "PLX_PROC": str(proc)}
            out = subprocess.run([str(DEPLOY / "release" / "plx-release"), "prune", "--keep", "1"],
                                 env=env, text=True, capture_output=True)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("nothing pruned", out.stderr)
            self.assertEqual(len([p for p in rel.iterdir() if not p.is_symlink()]), 5)


class ReleaseRestart(unittest.TestCase):
    """plx-release restart (Fable 11-14 #2/#3): the API and the app are always started, the
    API must answer /api/health and one authenticated read, and a release that does not
    serve rolls back by itself with a host alarm; a switch runs the configuration preflight
    first and a failed one switches nothing."""

    def run_release(self, *args: str, before: str = "active", health: str = "200",
                    health_after_rollback: str | None = None, me: str = "200",
                    check_loaded: bool = True, check_ok: bool = True,
                    check_root: str | None = None, preflight: str | None = None,
                    mint: str = "canary", release_mint: bool = False,
                    release_owner: str = "", probe: str | None = None,
                    mint_version: int = 0, release_mint_version: int = 0
                    ) -> subprocess.CompletedProcess:
        tmp = self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp]))
        rel = Path(tmp, "rel")
        for rid in ("old", "new"):
            Path(rel, rid).mkdir(parents=True)
            Path(rel, rid, "RELEASE.json").write_text("{}")
        os.symlink("new", rel / "current"); os.symlink("old", rel / "previous")
        stubs = Path(tmp, "bin"); stubs.mkdir()
        calls = Path(tmp, "calls"); calls.write_text("")
        state = Path(tmp, "api-state"); state.write_text(before)
        after = health_after_rollback or health
        scripts = {
            "systemctl": (f'echo "systemctl $*" >> {calls}\n'
                          'case "$1" in\n'
                          f'  is-active) [ "$(cat {state})" = active ];;\n'
                          f'  restart) echo active > {state};;\n'
                          # no warm switch here (ARCH-T6): the API restarts in place
                          '  show) case "$*" in *LoadState*plx-api@*) echo not-found;; *LoadState*) '
                          f'echo {"loaded" if check_loaded else "not-found"};;\n'
                          # the check unit's fixed release root (its WorkingDirectory=<root>/%i)
                          '    *WorkingDirectory*plx-api-check@*) u=${!#}; u=${u#plx-api-check@}; '
                          f'echo {check_root or rel}/${{u%.service}};; *) echo simple;; esac;;\n'
                          f'  start) case "$2" in plx-api-check@*) {"true" if check_ok else "false"};; esac;;\n'
                          '  *) true;; esac'),
            "sudo": 'shift_n=0; [ "$1" = -n ] && shift; exec "$@"',
            "journalctl": "echo 'store unusable'",
            # health answers from the release current points at (a rollback changes it)
            "curl": (f'case "$*" in *api/health*) if [ "$(readlink {rel}/current)" = new ]; '
                     f'then printf {health}; else printf {after}; fi;;\n'
                     f'  *api/me*) printf {me};;\n'
                     # the sign-in body (the ticket used) is recorded: which link was consumed
                     f'  *auth/dev-login*) for a; do case "$a" in @*) echo "dev-login $(cat "${{a#@}}")" >> {calls};; esac; done;;\n'
                     '  *) true;; esac'),
            "sleep": "true",
        }
        for name, body in scripts.items():
            f = Path(stubs, name); f.write_text("#!/bin/bash\n" + body + "\n"); f.chmod(0o755)
        home = Path(tmp, "home"); (home / ".config/plx").mkdir(parents=True)
        # the owner's own link, unused: no deploy may rewrite or consume it
        self.owner_link = home / ".config/plx/workbench-access-link"
        self.owner_link.write_text("https://x/#ticket=OWNER-UNUSED\n")
        self.owner_before = (self.owner_link.read_bytes(), self.owner_link.stat().st_mtime_ns)
        mint_path = Path(tmp, "plx-mint-link")
        cfg = home / ".config/plx"
        # current plx-mint-link: --canary writes the canary's own file, else the owner's
        canary_mint = (f'#!/bin/sh\necho "mint $*" >> {calls}\n'
                       f'if [ "$1" = "--canary" ]; then echo "https://x/#ticket=t0k3n" > {cfg}/ask-canary-access-link\n'
                       f'else echo "https://x/#ticket=OWNER-NEW" > {cfg}/workbench-access-link; fi\n')
        # the pre-canary copy: reads --canary as a TTL and refuses; otherwise mints the owner's
        old_mint = (f'#!/bin/sh\necho "mint $*" >> {calls}\n'
                    '[ -n "$1" ] && exit 2\n'
                    f'echo "https://x/#ticket=OWNER-NEW" > {cfg}/workbench-access-link\n')
        def versioned(body: str, version: int) -> str:
            return body + (f"PLX_MINT_LINK_VERSION={version}\n" if version else "")
        if mint != "none":
            mint_path.write_text(versioned(canary_mint, mint_version) if mint == "canary"
                                 else old_mint)
            mint_path.chmod(0o755)
        if release_mint:   # the serving release ships the current copy
            for rid in ("old", "new"):
                src = Path(rel, rid, "deploy/ops/plx-mint-link")
                src.parent.mkdir(parents=True)
                src.write_text(versioned(canary_mint, release_mint_version)); src.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLX_")}
        env.update({"PATH": f"{stubs}:{env.get('PATH', '/usr/bin:/bin')}", "HOME": str(home),
                    "PLX_DEPLOY_ENV": "/nonexistent", "PLX_RELEASES": str(rel),
                    "PLX_API_URL": "http://127.0.0.1:1", "PLX_RELEASE_OWNER": release_owner,
                    "PLX_HOST_OPS": f"{tmp}/ops", "PLX_MINT_LINK": str(mint_path)})
        if probe is not None:
            env["PLX_RELEASE_AUTH_PROBE"] = probe
        if preflight is not None:
            env["PLX_RELEASE_PREFLIGHT"] = preflight
        got = subprocess.run([str(DEPLOY / "release" / "plx-release"), *args],
                             env=env, text=True, capture_output=True, timeout=120)
        history = rel / "history.log"
        self.history = history.read_text() if history.exists() else ""
        self.calls = calls.read_text()
        self.current = os.readlink(rel / "current")
        status = Path(tmp, "ops", "status.json")
        self.alarms = json.loads(status.read_text()).get("attention", []) if status.exists() else []
        return got

    def test_a_stopped_api_and_app_are_started_and_must_serve(self) -> None:
        got = self.run_release("restart", before="inactive")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("systemctl restart plx-api plx-app", self.calls)
        self.assertIn("health=200 auth=ok", got.stdout)
        # the job-queue worker loads the new release too (it served a 6 h old one, 11-13)
        self.assertIn("plx-jobq-worker", self.history)

    def test_a_release_that_does_not_serve_rolls_back_by_itself_and_alarms(self) -> None:
        got = self.run_release("restart", health="000", health_after_rollback="200")
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("rolled back to old", got.stderr)
        self.assertEqual(self.current, "old")
        self.assertTrue(any("release new was not serving" in a and "rolled back to old" in a
                            for a in self.alarms), self.alarms)

    def test_a_failed_authenticated_read_is_a_failed_release(self) -> None:
        got = self.run_release("restart", me="401")
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(self.current, "old")
        self.assertIn("auth=failed", got.stderr + got.stdout)
        self.assertNotIn("t0k3n", got.stdout + got.stderr + self.history)

    def assert_owner_link_untouched(self) -> None:
        self.assertEqual((self.owner_link.read_bytes(), self.owner_link.stat().st_mtime_ns),
                         self.owner_before)
        self.assertNotIn("OWNER", "".join(l for l in self.calls.splitlines(True)
                                          if l.startswith("dev-login")))

    def test_the_deploy_sign_in_check_never_writes_or_consumes_the_owner_link(self) -> None:
        """alarms-1115 re-check: the probe minted an owner link (120 min) into the owner's
        file and signed in with it (single-use), so every deploy left the owner a dead link.
        It signs in as the Ask canary's service session through its own link file."""
        # restart is the step deploy and rollback serve through (deploy = migrate, switch, restart)
        got = self.run_release("restart")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("health=200 auth=ok", got.stdout)
        self.assertIn("mint --canary", self.calls)
        self.assertNotRegex(self.calls, r"(?m)^mint\s*$")   # never the owner mode
        self.assertIn("dev-login", self.calls)
        self.assertIn("t0k3n", self.calls)
        self.assert_owner_link_untouched()
        # a failed read rolls back and re-checks: still never the owner's link
        got = self.run_release("restart", me="401")
        self.assertNotEqual(got.returncode, 0)
        self.assert_owner_link_untouched()

    def test_an_old_mint_link_is_refreshed_from_the_sealed_release_before_the_check(self) -> None:
        import getpass
        got = self.run_release("restart", mint="old", release_mint=True,
                               release_owner=getpass.getuser())
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("health=200 auth=ok", got.stdout)
        self.assertIn("plx-mint-link refreshed from release new", self.history)
        self.assertIn('"--canary"', Path(self.tmp, "plx-mint-link").read_text())
        self.assertIn("mint --canary", self.calls)
        self.assert_owner_link_untouched()

    def test_a_mint_link_older_than_the_release_is_refreshed_and_never_downgraded(self) -> None:
        """An installed copy with --canary but a lower PLX_MINT_LINK_VERSION (the host kept
        one that swallowed the mint's refusal: exit 2, no reason) is replaced by the sealed
        release's; a newer installed copy is never replaced by an older release's (rollback)."""
        import getpass
        me = getpass.getuser()
        got = self.run_release("restart", mint="canary", mint_version=1, release_mint=True,
                               release_mint_version=2, release_owner=me)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("plx-mint-link refreshed from release new (version 2)", self.history)
        self.assertIn("PLX_MINT_LINK_VERSION=2", Path(self.tmp, "plx-mint-link").read_text())
        for installed in (2, 3):
            got = self.run_release("restart", mint="canary", mint_version=installed,
                                   release_mint=True, release_mint_version=2,
                                   release_owner=me)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertNotIn("refreshed", self.history)
            self.assertIn(f"PLX_MINT_LINK_VERSION={installed}",
                          Path(self.tmp, "plx-mint-link").read_text())
        # an older copy and an unsealed release: logged, left alone, deploy not rolled back
        got = self.run_release("restart", mint="canary", mint_version=1, release_mint=True,
                               release_mint_version=2)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("not refreshed", self.history)
        self.assertEqual(self.current, "new")

    def test_an_old_mint_link_that_cannot_be_refreshed_is_never_used(self) -> None:
        """No --canary and no sealed copy to refresh from (the release is not owned by the
        release owner): the deploy is not rolled back for it and the owner link is never
        minted; the check stays at /api/health (logged). Required: refused."""
        got = self.run_release("restart", mint="old", release_mint=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.current, "new")
        self.assertIn("not refreshed", self.history)
        self.assertIn("no --canary mode", self.history)
        self.assertNotIn("mint", "".join(l for l in self.calls.splitlines(True)
                                          if l.startswith("mint")))
        self.assert_owner_link_untouched()
        got = self.run_release("restart", mint="old", probe="required")
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("no --canary mode", got.stderr)
        self.assertNotIn("mint", "".join(l for l in self.calls.splitlines(True)
                                          if l.startswith("mint")))
        self.assert_owner_link_untouched()

    def test_a_rollback_that_does_not_serve_either_alarms_and_stops(self) -> None:
        got = self.run_release("restart", health="000")
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(self.current, "old")          # once, never back and forth
        self.assertTrue(any("does not serve either" in a for a in self.alarms), self.alarms)

    def test_a_healthy_restart_passes(self) -> None:
        got = self.run_release("restart")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.current, "new")
        self.assertEqual(self.alarms, [])

    def test_a_switch_runs_the_preflight_and_a_failed_one_switches_nothing(self) -> None:
        got = self.run_release("switch", "old", check_ok=False)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("failed its configuration check", got.stderr)
        self.assertEqual(self.current, "new")
        self.assertIn("start plx-api-check@old.service", self.calls)
        got = self.run_release("switch", "old")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.current, "old")
        got = self.run_release("switch", "old", check_loaded=False)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("skipped", got.stdout)

    def test_a_check_unit_over_another_release_root_never_judges_this_release(self) -> None:
        # main-red 11-15: the host's check unit runs /data/plx-releases/<id>; a release under a
        # test or staging root is skipped (said), or refused when the preflight is required
        got = self.run_release("switch", "old", check_root="/data/plx-releases")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("skipped", got.stdout)
        self.assertEqual(self.current, "old")
        self.assertNotIn("start plx-api-check@old.service", self.calls)
        got = self.run_release("switch", "old", check_root="/data/plx-releases",
                               preflight="required")
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual(self.current, "new")


class ProactiveIncRunnerTest(unittest.TestCase):
    """deploy/spark/proactive_inc.sh: fails loudly when RELEASE.json cannot be read or sha is
    empty, cleans temporary shipped files, clears stale output files, and skips apply on
    INC_APPLY=0."""

    def _setup(self, tmp: Path, release_text: str | None) -> tuple[Path, Path, Path, Path]:
        # Copy proactive_inc.sh into an isolated root so RELEASE.json is controlled
        root = tmp / "rel"
        spark = root / "deploy" / "spark"
        spark.mkdir(parents=True)
        runner = spark / "proactive_inc.sh"
        runner.write_text((ROOT / "deploy" / "spark" / "proactive_inc.sh").read_text())
        runner.chmod(0o755)
        if release_text is not None:
            (root / "RELEASE.json").write_text(release_text)
        calls = tmp / "calls.log"
        py_stub = tmp / "py-stub"
        py_stub.write_text(
            f'#!/bin/bash\necho "py $*" >> {calls}\n'
            'prev=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "--prepare-dir" ]; then\n'
            '    mkdir -p "$a"\n'
            '    echo \'{"state":"prepared"}\' > "$a/context.json"\n'
            '  fi\n'
            '  prev="$a"\n'
            'done\n',
            encoding="utf-8",
        )
        py_stub.chmod(0o755)
        spark_stub = tmp / "spark-stub"
        spark_stub.write_text(
            f'#!/bin/bash\necho "spark $*" >> {calls}\n'
            'prev=""; out_dir=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "--out" ]; then out_dir="$a"; fi\n'
            '  prev="$a"\n'
            'done\n'
            'echo \'{}\' > "$out_dir/inc-1.json"\n',
            encoding="utf-8",
        )
        spark_stub.chmod(0o755)
        return runner, calls, py_stub, spark_stub

    def test_missing_or_empty_release_sha_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for idx, content in enumerate((None, "{bad", json.dumps({"sha": ""}))):
                runner, _calls, py_stub, spark_stub = self._setup(tmp / f"c{idx}", content)
                env = {**os.environ, "INC_PYTHON": str(py_stub),
                       "INC_SPARK_RUN": str(spark_stub)}
                env.pop("INC_SHA", None)
                got = subprocess.run(["bash", str(runner), str(tmp / "state"),
                                      str(tmp / f"out{idx}")],
                                     env=env, capture_output=True, text=True)
                self.assertEqual(got.returncode, 2, (idx, got.stderr))
                self.assertIn("sha", got.stderr)

    def test_rehearsal_and_normal_cleanup_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            runner, calls, py_stub, spark_stub = self._setup(
                tmp / "r", json.dumps({"sha": "a" * 40}))
            out = tmp / "out"
            (out / "back").mkdir(parents=True)
            (out / "back" / "stale.json").write_text("stale")
            (out / "ship").mkdir(parents=True)
            (out / "ship" / "old.txt").write_text("old")

            env = {**os.environ, "INC_PYTHON": str(py_stub), "INC_SPARK_RUN": str(spark_stub),
                   "INC_APPLY": "0"}
            dry = subprocess.run(["bash", str(runner), str(tmp / "state"), str(out)],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse((out / "back" / "stale.json").exists())
            self.assertTrue((out / "back" / "inc-1.json").is_file())
            self.assertFalse((out / "ship").exists())
            log = calls.read_text()
            self.assertIn("--prepare-dir", log)
            self.assertIn("--cleanup-ship", log)
            self.assertNotIn("--apply-folds", log)

            calls.write_text("")
            env["INC_APPLY"] = "1"
            live = subprocess.run(["bash", str(runner), str(tmp / "state"), str(out)],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(live.returncode, 0, live.stderr)
            log2 = calls.read_text()
            self.assertIn("--prepare-dir", log2)
            self.assertIn("--cleanup-ship", log2)
            self.assertIn("--apply-folds", log2)
            self.assertFalse((out / "ship").exists())


class SearchFollowRunnerTest(unittest.TestCase):
    """deploy/spark/search_follow.sh: fails loudly on unreadable/empty RELEASE.json, skips
    Spark when prepare leaves no manifest, honors SEARCH_FOLLOW_APPLY=0, cleans $ship on exit,
    and discards $back on Spark setup failure."""

    def _setup(self, tmp: Path, release_text: str | None, *, write_manifest: bool = True,
               spark_rc: int = 0) -> tuple[Path, Path, Path, Path]:
        root = tmp / "rel"
        spark = root / "deploy" / "spark"
        spark.mkdir(parents=True)
        runner = spark / "search_follow.sh"
        runner.write_text((ROOT / "deploy" / "spark" / "search_follow.sh").read_text())
        runner.chmod(0o755)
        if release_text is not None:
            (root / "RELEASE.json").write_text(release_text)
        calls = tmp / "calls.log"
        py_stub = tmp / "py-stub"
        manifest_snippet = (
            '    mkdir -p "$a"\n    echo \'{"transport_version":1}\' > "$a/manifest.json"\n'
            if write_manifest else "")
        py_stub.write_text(
            f'#!/bin/bash\necho "py $*" >> {calls}\n'
            'prev=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "--prepare-dir" ]; then\n'
            f'{manifest_snippet}'
            '    true\n'
            '  fi\n'
            '  prev="$a"\n'
            'done\n',
            encoding="utf-8",
        )
        py_stub.chmod(0o755)
        spark_stub = tmp / "spark-stub"
        spark_stub.write_text(
            f'#!/bin/bash\necho "spark $*" >> {calls}\n'
            'prev=""; out_dir=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "--out" ]; then out_dir="$a"; fi\n'
            '  prev="$a"\n'
            'done\n'
            f'if [ "{spark_rc}" = 0 ]; then\n'
            '  echo \'{"search_follow_transport":1}\' > "$out_dir/export.json"\n'
            'fi\n'
            f'exit {spark_rc}\n',
            encoding="utf-8",
        )
        spark_stub.chmod(0o755)
        return runner, calls, py_stub, spark_stub

    def test_missing_or_empty_release_sha_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for idx, content in enumerate((None, "{bad", json.dumps({"sha": ""}))):
                runner, _calls, py_stub, spark_stub = self._setup(tmp_p / f"c{idx}", content)
                env = {**os.environ, "SEARCH_FOLLOW_PYTHON": str(py_stub),
                       "SEARCH_FOLLOW_SPARK_RUN": str(spark_stub)}
                env.pop("SEARCH_FOLLOW_SHA", None)
                got = subprocess.run(["bash", str(runner), str(tmp_p / "state"),
                                      str(tmp_p / f"out{idx}")],
                                     env=env, capture_output=True, text=True)
                self.assertEqual(got.returncode, 2, (idx, got.stderr))
                self.assertIn("sha", got.stderr)

    def test_idle_prepare_skips_spark_and_cleans_ship(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            runner, calls, py_stub, spark_stub = self._setup(
                tmp_p / "r", json.dumps({"sha": "a" * 40}), write_manifest=False)
            out = tmp_p / "out"
            env = {**os.environ, "SEARCH_FOLLOW_PYTHON": str(py_stub),
                   "SEARCH_FOLLOW_SPARK_RUN": str(spark_stub)}
            got = subprocess.run(["bash", str(runner), str(tmp_p / "state"), str(out)],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertFalse((out / "ship").exists())
            log = calls.read_text()
            self.assertIn("--prepare-dir", log)
            self.assertNotIn("spark ", log)
            self.assertNotIn("--apply-dir", log)

    def test_rehearsal_and_normal_apply_and_spark_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            runner, calls, py_stub, spark_stub = self._setup(
                tmp_p / "r", json.dumps({"sha": "a" * 40}))
            out = tmp_p / "out"
            (out / "back").mkdir(parents=True)
            (out / "back" / "stale.json").write_text("stale")
            (out / "ship").mkdir(parents=True)
            (out / "ship" / "old.txt").write_text("old")

            env = {**os.environ, "SEARCH_FOLLOW_PYTHON": str(py_stub),
                   "SEARCH_FOLLOW_SPARK_RUN": str(spark_stub), "SEARCH_FOLLOW_APPLY": "0"}
            dry = subprocess.run(["bash", str(runner), str(tmp_p / "state"), str(out)],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse((out / "back" / "stale.json").exists())
            self.assertTrue((out / "back" / "export.json").is_file())
            self.assertFalse((out / "ship").exists())
            log = calls.read_text()
            self.assertIn("--prepare-dir", log)
            self.assertIn("--cleanup-ship", log)
            self.assertIn("--pg standby", log)
            self.assertNotIn("--apply-dir", log)

            calls.write_text("")
            env["SEARCH_FOLLOW_APPLY"] = "1"
            live = subprocess.run(["bash", str(runner), str(tmp_p / "state"), str(out)],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(live.returncode, 0, live.stderr)
            log2 = calls.read_text()
            self.assertIn("--apply-dir", log2)
            self.assertFalse((out / "ship").exists())

            # Spark failure discards back and skips apply
            runner_f, calls_f, py_f, spark_f = self._setup(
                tmp_p / "rf", json.dumps({"sha": "b" * 40}), spark_rc=96)
            out_f = tmp_p / "out_f"
            (out_f / "back").mkdir(parents=True)
            (out_f / "back" / "old.json").write_text("old")
            env_f = {**os.environ, "SEARCH_FOLLOW_PYTHON": str(py_f),
                     "SEARCH_FOLLOW_SPARK_RUN": str(spark_f)}
            failed = subprocess.run(["bash", str(runner_f), str(tmp_p / "state"), str(out_f)],
                                    env=env_f, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 96)
            self.assertFalse((out_f / "back" / "old.json").exists())
            self.assertNotIn("--apply-dir", calls_f.read_text())

    def test_invoked_outside_root_sets_cwd_and_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            runner, calls, py_stub, spark_stub = self._setup(
                tmp_p / "r", json.dumps({"sha": "c" * 40}))
            rel_root = str((tmp_p / "r" / "rel").resolve())
            env_log = tmp_p / "env.log"
            py_stub.write_text(
                f'#!/bin/bash\necho "py pwd=$PWD py=$PYTHONPATH" >> {env_log}\n'
                f'echo "py $*" >> {calls}\n'
                'prev=""\n'
                'for a in "$@"; do\n'
                '  if [ "$prev" = "--prepare-dir" ]; then\n'
                '    mkdir -p "$a"\n'
                '    echo \'{"transport_version":1}\' > "$a/manifest.json"\n'
                '  fi\n'
                '  prev="$a"\n'
                'done\n',
                encoding="utf-8",
            )
            spark_stub.write_text(
                f'#!/bin/bash\necho "spark pwd=$PWD py=$PYTHONPATH" >> {env_log}\n'
                f'echo "spark $*" >> {calls}\n'
                'prev=""; out_dir=""\n'
                'for a in "$@"; do\n'
                '  if [ "$prev" = "--out" ]; then out_dir="$a"; fi\n'
                '  prev="$a"\n'
                'done\n'
                'echo \'{"search_follow_transport":1}\' > "$out_dir/export.json"\n',
                encoding="utf-8",
            )
            out = tmp_p / "out"
            env_unset = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env_unset.update({"SEARCH_FOLLOW_PYTHON": str(py_stub),
                              "SEARCH_FOLLOW_SPARK_RUN": str(spark_stub)})
            got1 = subprocess.run(["bash", str(runner), str(tmp_p / "state"), str(out)],
                                  cwd="/tmp", env=env_unset, capture_output=True, text=True)
            self.assertEqual(got1.returncode, 0, got1.stderr)
            lines1 = env_log.read_text().splitlines()
            self.assertEqual(len(lines1), 3)
            for line in lines1:
                self.assertIn(f"pwd={rel_root} ", line)
                self.assertTrue(line.endswith(f"py={rel_root}"), line)

            env_log.write_text("")
            env_set = {**env_unset, "PYTHONPATH": "/existing/site"}
            got2 = subprocess.run(["bash", str(runner), str(tmp_p / "state"), str(out)],
                                  cwd="/tmp", env=env_set, capture_output=True, text=True)
            self.assertEqual(got2.returncode, 0, got2.stderr)
            lines2 = env_log.read_text().splitlines()
            self.assertEqual(len(lines2), 3)
            for line in lines2:
                self.assertIn(f"pwd={rel_root} ", line)
                self.assertTrue(line.endswith(f"py={rel_root}:/existing/site"), line)

if __name__ == "__main__":
    unittest.main()
