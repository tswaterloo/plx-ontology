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
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
PERSONAL = re.compile(r"/home/[a-z_][a-z0-9_-]*|/Users/\w+|\btanush\b", re.IGNORECASE)


def tracked(folder: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", str(folder)], cwd=ROOT, text=True,
                         capture_output=True, check=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


class DeployFilesTest(unittest.TestCase):
    def test_whole_pass_is_capped_at_8g_in_the_unit_and_the_jobq_class(self) -> None:
        # AL-06: the 4-hourly whole refresh is back on under an 8G cap, the high mark below it
        unit = (ROOT / "deploy/systemd/plx-proactive-whole.service").read_text()
        self.assertRegex(unit, r"(?m)^MemoryMax=8G$")
        self.assertRegex(unit, r"(?m)^MemoryHigh=7G$")
        self.assertTrue((ROOT / "deploy/systemd/plx-proactive-whole.timer").is_file())
        from operating.jobq import classes as jobq_classes
        self.assertEqual(jobq_classes.load({})["proactive-whole"].mem_gb, 8)

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
            self.assertIn("Environment=PLX_ONTOLOGY_PG=1", proactive)
            self.assertIn("--refresh calling", proactive)
            self.assertNotIn("--refresh always", proactive)
            for name in ("plx-proactive-inc.service", "plx-proactive-nightly.service"):
                text = (out / name).read_text()
                self.assertIn("User=someone", text, name)
                self.assertIn("WorkingDirectory=/srv/rel/current", text, name)
            self.assertTrue((out / "plx-proactive-inc.path").is_file())
            self.assertTrue((out / "plx-proactive-nightly.timer").is_file())
            whole = (out / "plx-proactive-whole.service").read_text()
            self.assertIn("--refresh whole", whole)
            self.assertIn("Environment=PLX_ONTOLOGY_PG=1", whole)
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
                    self.assertNotIn("/home/tanush", text, path)
                    self.assertNotIn("/data/OntologyProject", text, path)

    def test_upkeep_units_render_bounded_and_carry_no_secret(self) -> None:
        """The units that keep data current: each has Nice and MemoryMax, the ones that read
        people data load the exclusion env file, and none embeds a credential value."""
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp, "deploy.env")
            env.write_text("PLX_USER=someone\nPLX_HOME=/srv/someone\nPLX_RELEASES=/srv/rel\n")
            for flag, out in ((["--user"], "u"), ([], "s")):
                subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", f"{tmp}/{out}",
                                "--env", str(env), *flag], check=True, capture_output=True)
            wanted = {"u": ["plx-sheet-readings", "plx-system-activity", "plx-team-activity",
                           "plx-work-graph"],
                      "s": ["plx-proactive-whole", "plx-backup", "plx-restore-test",
                            "plx-ops-health", "plx-labour"]}
            for out, names in wanted.items():
                for name in names:
                    service = Path(tmp, out, f"{name}.service").read_text()
                    self.assertRegex(service, r"(?m)^MemoryMax=", name)
                    self.assertRegex(service, r"(?m)^Nice=", name)
                    self.assertTrue(Path(tmp, out, f"{name}.timer").is_file(), name)
                    self.assertNotRegex(service, r"(?i)(SECRET|TOKEN|API_KEY|PASSWORD)=\S",
                                        name)
            for name in wanted["u"]:
                self.assertIn("EnvironmentFile=-/srv/someone/.config/plx/proactive.env",
                              Path(tmp, "u", f"{name}.service").read_text(), name)
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
            self.assertRegex(Path(tmp, "s", "plx-labour.timer").read_text(),
                             r"(?m)^OnCalendar=\*-\*-\* \d\d:\d\d:00 ")
            install = (DEPLOY / "ops" / "install_keep_fresh.sh").read_text()
            self.assertRegex(install, r'(?m)^SYS_UNITS=".*\bplx-labour"')
            activity = Path(tmp, "u", "plx-system-activity.service").read_text()
            self.assertIn("--weeks 13", activity)

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
        }
        dropped = {"AZURE_CLIENT_SECRET", "BC_CLIENT_SECRET", "MICROSOFT_GRAPH_CLIENT_SECRET",
                   "MC_MCP_API_KEY"}
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
            self.assertEqual(dropped - unset, set(), path.stem)
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
            env = {**os.environ, "PLX_DEPLOY_ENV": "/nonexistent", "PLX_STATE": str(state),
                   "PLX_API_USER": me, "PLX_STATE_GROUP": group, "PLX_USER": me}
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
                   "PLX_RELEASE_OWNER": ""}
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
                   "PLX_RELEASE_OWNER": "", "PLX_NODE": str(node.parent)}
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
                   "PLX_RELEASE_OWNER": getpass.getuser()}
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
                   "PLX_RELEASE_OWNER": ""}
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


class ReleaseRestart(unittest.TestCase):
    """plx-release restart: an API stopped while unused is no failed deploy, but one the new
    release crashes (activating or failed after the restart) is, so the deploy rolls back."""

    def restart(self, before: str, after: str, health: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp, "bin"); stubs.mkdir()
            state = Path(tmp, "api-state"); state.write_text(before)
            scripts = {
                "systemctl": (f'case "$1" in is-active) [ "$(cat {state})" = active ];;\n'
                              f'  try-restart) if [ "$(cat {state})" = active ]; then echo {after} > {state}; fi;;\n'
                              '  *) true;; esac'),
                "sudo": 'exec "$@"',
                "curl": f'printf {health}',
                "sleep": "true",
            }
            for name, body in scripts.items():
                f = Path(stubs, name); f.write_text("#!/bin/sh\n" + body + "\n"); f.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith("PLX_")}
            env.update({"PATH": f"{stubs}:{env.get('PATH', '/usr/bin:/bin')}",
                        "PLX_DEPLOY_ENV": "/nonexistent", "PLX_RELEASES": f"{tmp}/rel",
                        "PLX_API_URL": "http://127.0.0.1:1", "PLX_RELEASE_OWNER": ""})
            Path(tmp, "rel").mkdir()
            got = subprocess.run([str(DEPLOY / "release" / "plx-release"), "restart"],
                                 env=env, text=True, capture_output=True, timeout=120)
            history = Path(tmp, "rel", "history.log")
            self.history = history.read_text() if history.exists() else ""
            return got

    def test_a_stopped_api_is_not_a_failed_deploy(self) -> None:
        got = self.restart("inactive", "inactive", "000")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("api stopped", got.stdout)
        # the job-queue worker loads the new release too (it served a 6 h old one, 11-13)
        self.assertIn("plx-jobq-worker", self.history)

    def test_an_api_the_release_crashes_fails_the_restart(self) -> None:
        for after in ("activating", "failed"):
            got = self.restart("active", after, "000")
            self.assertNotEqual(got.returncode, 0, after)
            self.assertIn("not healthy", got.stderr)

    def test_a_healthy_restart_passes(self) -> None:
        got = self.restart("active", "active", "200")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("api=200", got.stdout)


if __name__ == "__main__":
    unittest.main()
