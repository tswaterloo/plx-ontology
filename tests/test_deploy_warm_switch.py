"""ARCH-T6: `plx-release restart` never routes a reader to a cold API and never reloads
Caddy for it. Run against stand-in APIs (one small HTTP server per unit, started by a stub
systemctl; each opens its port only once warm when the deploy's hold file names it, and on
SIGTERM closes it and ends what is under way, as operator_api/server.py does) behind the real
Caddy with the real deploy/Caddyfile's API routing (8780, then the standby's unix socket): readers keep being answered through the
whole switch, the new release serves only once warm, a standby that never gets warm leaves
the old API serving, and every reason the switch may not run (template missing, a setting
that differs, no memory, no Postgres room, a Caddy that does not send to the standby) restarts
in place and says why. Nothing listening anywhere but the standby's socket is ever sent a
reader, and `serve` refuses the mock and the fixed dev identities on a socket."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import http.client
import http.server
import urllib.error
import urllib.request
from tools.timing_policy import budget

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
TOOL = DEPLOY / "release" / "plx-release"
CADDY = shutil.which("caddy")

# A stand-in API: /api/health at once; /api/ready true once WARM seconds passed since the
# first probe (the warm-up starts on the probe), with the release's code version; anything
# else answers its own port and code. NOT_READY in the release: never ready.
FAKE_API = r'''
import http.server, json, os, signal, socketserver, sys, threading, time
where = sys.argv[1]
uds = not where.isdigit()
name = os.path.basename(os.path.dirname(where)) if uds else where   # server.hold_name
code = open("CODE").read().strip()
warm = float(os.environ.get("WARM", "1")); never = os.path.exists("NOT_READY")
if os.path.exists(os.path.join(os.environ["PLX_API_HOLD_DIR"], f"hold-{name}")):
    if never:                      # held: open only once warm
        time.sleep(3600)
    time.sleep(warm)
    cg = os.environ.get("PLX_CGROUP_ROOT", "")   # its weight while it warmed (the deploy's)
    unit = "plx-api@standby" if uds else "plx-api"
    try:
        seen = open(f"{cg}/system.slice/{unit}.service/cpu.weight").read().strip()
        io = open(f"{cg}/system.slice/{unit}.service/io.weight").read().strip()
        with open(os.path.join(os.environ["STUB_STATE"], "weights"), "a") as f:
            f.write(f"{unit} {seen} {io}\n")
    except OSError:
        pass
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def address_string(self): return "local"
    def send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == "/api/health": return self.send(200, {"status": "ok"})
        if self.path == "/api/ready":
            return self.send(503 if never else 200, {"ready": not never, "code_version": code})
        time.sleep(0.05)
        self.send(200, {"port": "standby" if uds else int(where), "code": code})
class T(http.server.ThreadingHTTPServer):
    daemon_threads = False         # SIGTERM: the requests under way end first
    block_on_close = True
class U(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True
server = U(where, H) if uds else T(("127.0.0.1", int(where)), H)
def stop(*_):                      # close the port, end what is under way, exit
    threading.Thread(target=server.shutdown).start()
signal.signal(signal.SIGTERM, stop)
server.serve_forever()
server.server_close()
if uds:
    os.unlink(where)
'''

# systemctl for the test: units are stand-in API processes on their port, run from current
SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, re, signal, subprocess, sys, time
from pathlib import Path
S = Path(os.environ["STUB_STATE"]); cfg = json.loads((S / "config.json").read_text())
args = sys.argv[1:]
def port(unit): return str(cfg["api_port"]) if unit == "plx-api" else cfg["standby_socket"]
def pidfile(unit): return S / (unit + ".pid")
def alive(unit):
    try: os.kill(int(pidfile(unit).read_text()), 0); return True
    except (OSError, ValueError): return False
def stop(unit):
    if alive(unit):
        pid = int(pidfile(unit).read_text()); os.kill(pid, signal.SIGTERM)
        for _ in range(500):
            try: os.kill(pid, 0); time.sleep(0.02)
            except OSError: break
    pidfile(unit).unlink(missing_ok=True)
    if unit != "plx-api":          # RuntimeDirectory: removed when the unit stops
        import shutil; shutil.rmtree(os.path.dirname(cfg["standby_socket"]), True)
def start(unit):
    stop(unit)
    cwd = os.path.realpath(cfg["current"])
    if unit != "plx-api":
        os.makedirs(os.path.dirname(cfg["standby_socket"]), mode=0o711, exist_ok=True)
    with open(S / "log", "a") as log:
        p = subprocess.Popen([sys.executable, cfg["fake"], port(unit)], cwd=cwd,
                             stdout=log, stderr=log, start_new_session=True,
                             env={**os.environ, "PLX_API_HOLD_DIR": cfg["hold"]})
    pidfile(unit).write_text(str(p.pid))
    with open(S / "calls", "a") as f: f.write(f"start {unit} {Path(cwd).name}\n")
def show(unit, props, value):
    standby = "@" in unit
    loaded = "not-found" if (standby and cfg.get("no_template")) else "loaded"
    p = "--port " + port(unit) if unit == "plx-api" else "--unix-socket " + port(unit)
    out = {"LoadState": loaded, "MemoryCurrent": str(cfg.get("api_bytes", 1 << 30)),
           "CPUWeight": "1000", "IOWeight": "1000",
           "MainPID": pidfile(unit).read_text() if alive(unit) else "0",
           "DropInPaths": " ".join(cfg["dropins"]) if not standby else "",
           "ExecStart": "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -m operator_api serve "
                        f"--bind 127.0.0.1 {p} $PLX_API_ARGS ; ignore_errors=no ; pid=1 }}",
           "User": cfg.get("standby_user", "plx-workbench") if standby else "plx-workbench",
           "Environment": f"PGAPPNAME={unit} PLX_ONTOLOGY_PG=only"}
    for name in props:
        if name in out:
            print(out[name] if value else f"{name}={out[name]}")
        elif not value:
            print(f"{name}=")
cmd = args[0]
if cmd not in ("is-active", "show"):
    with open(S / "timeline", "a") as f: f.write(time.strftime("%H:%M:%S ") + " ".join(args) + "\n")
if cmd == "is-active":
    up = alive(args[-1])
    if "-q" not in args: print("active" if up else "inactive")
    sys.exit(0 if up else 3)
if cmd in ("restart", "start"):
    for unit in args[1:]:          # only the API units are stood in for (plx-app: nothing)
        if unit.startswith("plx-api"): start(unit)
elif cmd == "try-restart":
    for unit in args[1:]:
        if unit.startswith("plx-api") and alive(unit): start(unit)
elif cmd == "stop":
    for unit in args[1:]: stop(unit)
    with open(S / "calls", "a") as f: f.write(f"stop {' '.join(args[1:])}\n")
elif cmd == "reload":
    if cfg.get("caddyfile"):
        # the Caddyfile's {$...} are read by this process: Caddy's own environment
        r = subprocess.run([cfg["caddy"], "reload", "--config", cfg["caddyfile"], "--adapter",
                            "caddyfile", "--force"], capture_output=True, text=True,
                           env={**os.environ, **cfg["caddy_env"]})
        with open(S / "calls", "a") as f: f.write(f"reload {r.returncode}\n")
        sys.exit(r.returncode)
elif cmd == "show":
    value = "--value" in args
    props = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
    unit = [a for a in args[1:] if not a.startswith("-") and a not in props][-1]
    show(unit, props, value)
'''


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=2)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(2)
        self.sock.connect(self.path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def caddy_snippets() -> str:
    """The real Caddyfile's snippet of the site's routes (the API's proxy in it), nothing else:
    each from its "(name) {" line to the first "}" line at the start of a line."""
    lines = (DEPLOY / "Caddyfile").read_text().splitlines()
    out = []
    for name in ("plx_workbench",):
        start = lines.index(f"({name}) {{")
        end = lines.index("}", start)
        out.append("\n".join(lines[start:end + 1]))
    return "\n\n".join(out)


class Host:
    """Two releases (old, new), the stand-in units, the hold folder, optionally Caddy."""

    def __init__(self, test: unittest.TestCase, *, caddy: bool, **cfg) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t6-", dir="/tmp"))
        test.addCleanup(shutil.rmtree, self.tmp, True)
        test.addCleanup(self.cleanup)
        self.api_port = free_port()
        self.standby_socket = str(self.tmp / "run" / "plx-api-standby" / "api.sock")
        rel = self.tmp / "rel"
        for rid in ("old", "new"):
            pkg = rel / rid / "operator_api"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "page_store.py").write_text(f"def code_version():\n    return 'code-{rid}'\n")
            (rel / rid / "CODE").write_text(f"code-{rid}")
            (rel / rid / "RELEASE.json").write_text("{}")
            (rel / rid / "deploy").mkdir()
            (rel / rid / "deploy" / "Caddyfile").write_text("the same in both\n")
        os.symlink("old", rel / "current")
        bins = self.tmp / "bin"
        bins.mkdir()
        venv = self.tmp / "venv" / "bin"
        venv.mkdir(parents=True)
        os.symlink(sys.executable, venv / "python3")
        for name, body in (("systemctl", SYSTEMCTL), ("sudo", '#!/bin/sh\n[ "$1" = -n ] && shift\n[ "$1" = -u ] && shift 2\nexec "$@"\n'),
                           ("sleep", '#!/bin/sh\nexec /bin/sleep 0.2\n')):
            path = bins / name
            path.write_text(body)
            path.chmod(0o755)
        (self.tmp / "fake.py").write_text(FAKE_API)
        dropins = self.tmp / "etc" / "plx-api.service.d"
        dropins.mkdir(parents=True)
        (dropins / "a.conf").write_text("[Service]\n")
        (self.tmp / "meminfo").write_text("MemAvailable:   %d kB\n" % (cfg.pop("avail_mb", 32000) * 1024))
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.hold = self.tmp / "run" / "plx-api"
        self.hold.parent.mkdir()
        self.cfg = {"api_port": self.api_port, "current": str(rel / "current"),
                    "standby_socket": self.standby_socket,
                    "hold": str(self.hold),
                    "fake": str(self.tmp / "fake.py"),
                    "dropins": [str(dropins / "a.conf"), str(self.tmp / "etc/plx-.service.d/x.conf")],
                    **cfg}
        self.caddy_proc = None
        if caddy:
            self.caddy_port = free_port()
            caddyfile = self.tmp / "Caddyfile"
            caddyfile.write_text(
                "{\n\tadmin unix/%s/admin.sock\n\tauto_https off\n}\n\n%s\n\nhttp://127.0.0.1:%d {\n"
                "\timport plx_workbench\n}\n" % (self.tmp, caddy_snippets(), self.caddy_port))
            self.cfg.update(caddyfile=str(caddyfile), caddy=CADDY)
            own = {"XDG_DATA_HOME": str(self.tmp / "xdg"),
                   "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
                   "PLX_API_UPSTREAM": f"127.0.0.1:{self.api_port}",
                   "PLX_API_STANDBY": f"unix/{self.standby_socket}",
                   "PLX_APP_UPSTREAM": f"127.0.0.1:{free_port()}"}
            self.caddy_env = {**os.environ, **own}
            self.cfg["caddy_env"] = own
            self.caddy_proc = subprocess.Popen(
                [CADDY, "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
                env=self.caddy_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.state / "config.json").write_text(json.dumps(self.cfg))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("PLX_") and k not in ("PGUSER", "PGHOST")}
        self.env.update({
            "PATH": f"{bins}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_STATE": str(self.state), "PLX_DEPLOY_ENV": "/nonexistent",
            "PLX_RELEASES": str(rel), "PLX_CURRENT": str(rel / "current"), "PLX_RELEASE_OWNER": "",
            "PLX_VENV": str(self.tmp / "venv"),
            "PLX_API_URL": f"http://127.0.0.1:{self.api_port}",
            "PLX_API_STANDBY_SOCKET": self.standby_socket,
            "PLX_API_HOLD_DIR": str(self.hold), "PLX_SYSTEMD_DIR": str(self.tmp / "units"),
            "PLX_CADDY_ADMIN": str(self.tmp / "admin.sock"),
            "PLX_MEMINFO": str(self.tmp / "meminfo"), "PLX_CGROUP_ROOT": str(self.tmp / "nocg"),
            "PLX_API_READY_S": "8", "TMPDIR": str(self.tmp),
            "PLX_RELEASE_AUTH_PROBE": "0", "PLX_HOST_OPS": str(self.tmp / "ops")})
        self.systemctl("start", "plx-api")
        if caddy:
            assert self.wait_through(lambda r: r and r["code"] == "code-old"), "caddy not up"

    def cleanup(self) -> None:
        for pid in self.state.glob("*.pid"):
            try:
                os.kill(int(pid.read_text()), 15)
            except (OSError, ValueError):
                pass
        if self.caddy_proc is not None:
            self.caddy_proc.terminate()
            self.caddy_proc.wait(10)

    def systemctl(self, *args: str) -> None:
        subprocess.run([str(self.tmp / "bin" / "systemctl"), *args], env=self.env, check=True,
                       capture_output=True)
        if args[0] in ("start", "restart"):
            for _ in range(250):
                if self.serving("plx-api" if args[1] == "plx-api" else "standby"):
                    return
                time.sleep(0.02)
            raise AssertionError(f"stand-in {args[1]} never came up")

    def through(self, path: str = "/api/ping", errors: list | None = None) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.caddy_port}{path}", timeout=10) as r:
                return json.loads(r.read())
        except (OSError, ValueError) as error:
            if errors is not None:
                errors.append((time.strftime("%H:%M:%S"), repr(error)[:200]))
            return None

    def wait_through(self, check, seconds: float = 10) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check(self.through()):
                return True
            time.sleep(0.1)
        return False

    def switch_to_new(self) -> None:
        current = Path(self.cfg["current"])
        os.symlink("new", current.with_name(".c"))
        os.replace(current.with_name(".c"), current)
        os.symlink("old", current.with_name("previous"))   # as plx-release switch leaves it

    def restart(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(TOOL), "restart", *args], env=self.env, text=True,
                              capture_output=True, timeout=120)

    def calls(self) -> list[str]:
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def serving(self, who: str) -> dict | None:
        """What plx-api (its port) or the standby (its socket) answers, asked directly."""
        if who == "plx-api":
            conn = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=2)
        else:
            conn = UnixConnection(self.standby_socket)
        try:
            conn.request("GET", "/api/ping")
            return json.loads(conn.getresponse().read())
        except (OSError, ValueError):
            return None
        finally:
            conn.close()


class Loop(threading.Thread):
    """Readers asking through Caddy every 20 ms: every answer kept, every failure counted."""

    def __init__(self, host: Host) -> None:
        super().__init__(daemon=True)
        self.host, self.stop, self.answers, self.failed = host, threading.Event(), [], 0
        self.errors: list = []

    def run(self) -> None:
        while not self.stop.is_set():
            got = self.host.through(errors=self.errors)
            if got is None:
                self.failed += 1
            else:
                self.answers.append((got["port"], got["code"]))
            time.sleep(0.02)


@unittest.skipIf(CADDY is None, "caddy not installed")
class WarmSwitchThroughCaddy(unittest.TestCase):
    def test_a_restart_switches_through_a_warm_standby_and_no_request_fails(self):
        host = Host(self, caddy=True)
        self.assertIsNone(host.through("/api/ready"))      # the probe is loopback-only
        host.switch_to_new()
        loop = Loop(host)
        loop.start()
        got = host.restart()
        time.sleep(0.5)
        loop.stop.set()
        loop.join()
        timeline = (host.state / "timeline").read_text()
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertIn("warm switch", got.stdout)
        self.assertEqual(loop.failed, 0, (loop.errors, got.stdout, got.stderr, timeline))
        served = list(dict.fromkeys(loop.answers))
        # old API, then the warm standby on the new release, then plx-api on the new one
        self.assertEqual(served, [(host.api_port, "code-old"), ("standby", "code-new"),
                                  (host.api_port, "code-new")])
        self.assertNotIn("plx-caddy", timeline)            # Caddy never reloaded
        self.assertIsNone(host.serving("standby"))         # the standby is stopped
        self.assertFalse(Path(host.standby_socket).exists())
        self.assertEqual(list(host.hold.iterdir()), [])     # no start held after this
        starts = [c for c in host.calls() if c.startswith("start")]
        self.assertEqual(starts, ["start plx-api old", "start plx-api@standby new",
                                  "start plx-api new"])
        slot = (host.tmp / "units" / "plx-api@.service.d" / "zzzz-slot.conf").read_text()
        self.assertIn("ExecStart=\nExecStart=/usr/bin/python3 -m operator_api serve --bind "
                      "127.0.0.1 --unix-socket /run/plx-api-%i/api.sock $PLX_API_ARGS", slot)
        links = sorted(p.name for p in (host.tmp / "units" / "plx-api@.service.d").iterdir())
        self.assertEqual(links, ["a.conf", "zzzz-slot.conf"])   # not the plx-.service.d one

    def test_a_warming_api_yields_the_cores_to_the_one_that_serves(self):
        """While a held API warms beside the serving one its cgroup's CPU and IO weight are
        the deploy's low ones (live 11-21: at equal weight the serving standby's reads took
        14-17 s), and its own again before it serves; a warm-up past WARM_LOW_S gets its
        own back while still warming, so a loaded box never starves it past the wait."""
        def run(**env):
            host = Host(self, caddy=True)
            for unit in ("plx-api", "plx-api@standby"):
                cg = host.tmp / "cg" / "system.slice" / f"{unit}.service"
                cg.mkdir(parents=True)
                (cg / "cpu.weight").write_text("1000\n")
                (cg / "io.weight").write_text("default 1000\n")
            host.env.update({"PLX_CGROUP_ROOT": str(host.tmp / "cg"), **env})
            host.switch_to_new()
            got = host.restart()
            self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
            self.assertIn("warm switch", got.stdout)
            seen = (host.state / "weights").read_text().split("\n")
            after = {u: ((host.tmp / "cg" / "system.slice" / f"{u}.service" / "cpu.weight")
                         .read_text().strip(),
                         (host.tmp / "cg" / "system.slice" / f"{u}.service" / "io.weight")
                         .read_text().strip()) for u in ("plx-api", "plx-api@standby")}
            return [line for line in seen if line], after
        own = {"plx-api": ("1000", "default 1000"), "plx-api@standby": ("1000", "default 1000")}
        seen, after = run()
        # the standby warms beside the old, warm API at its own weight; plx-api, warming
        # again while the standby serves alone, at the low one
        self.assertEqual(seen, ["plx-api@standby 1000 default 1000", "plx-api 100 default 100"])
        self.assertEqual(after, own)
        # a warm-up longer than WARM_LOW_S (here 0 s; the stand-in warms 3 s) has its own
        # weight back before it is ready
        seen, after = run(PLX_API_WARM_LOW_S="0", WARM="3")
        self.assertEqual(seen, ["plx-api@standby 1000 default 1000",
                                "plx-api 1000 default 1000"])
        self.assertEqual(after, own)

    def _weighted(self) -> "Host":
        host = Host(self, caddy=True)
        for unit in ("plx-api", "plx-api@standby"):
            cg = host.tmp / "cg" / "system.slice" / f"{unit}.service"
            cg.mkdir(parents=True)
            (cg / "cpu.weight").write_text("1000\n")
            (cg / "io.weight").write_text("default 1000\n")
        host.env["PLX_CGROUP_ROOT"] = str(host.tmp / "cg")
        return host

    @staticmethod
    def _weights(host: "Host") -> dict:
        cg = host.tmp / "cg" / "system.slice"
        return {u: ((cg / f"{u}.service" / "cpu.weight").read_text().strip(),
                    (cg / f"{u}.service" / "io.weight").read_text().strip())
                for u in ("plx-api", "plx-api@standby")}

    OWN = {"plx-api": ("1000", "default 1000"), "plx-api@standby": ("1000", "default 1000")}

    def test_a_failed_or_stopped_warm_up_gives_the_unit_its_weight_back(self):
        """A standby never ready, and a deploy stopped (SIGTERM) while plx-api warms,
        both leave every API unit at its own weights; weights a killed deploy left low are
        put back by the next switch and by ``plx-release slots``."""
        host = self._weighted()
        (Path(host.cfg["current"]).parent / "new" / "NOT_READY").write_text("")
        host.switch_to_new()
        got = host.restart()
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("not ready within", got.stderr)
        self.assertEqual(self._weights(host), self.OWN)

        host = self._weighted()
        host.env["WARM"] = "4"
        host.switch_to_new()
        tool = subprocess.Popen([str(TOOL), "restart"], env=host.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        end = time.monotonic() + 15
        while self._weights(host)["plx-api"][0] != "100" and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(self._weights(host)["plx-api"], ("100", "default 100"))
        tool.terminate()
        tool.communicate(timeout=30)
        self.assertEqual(self._weights(host), self.OWN)

        for unit in ("plx-api", "plx-api@standby"):   # left low by a deploy killed -9
            cg = host.tmp / "cg" / "system.slice" / f"{unit}.service"
            (cg / "cpu.weight").write_text("100\n")
            (cg / "io.weight").write_text("default 100\n")
        subprocess.run([str(TOOL), "slots"], env=host.env, text=True, capture_output=True,
                       timeout=60)
        self.assertEqual(self._weights(host), self.OWN)

    def _releases_from_commits(self, host, changed: str) -> None:
        """old and new built from two commits of a repo whose second commit changes ``changed``."""
        repo = host.tmp / "repo"
        git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / Path(changed).parent).mkdir(parents=True, exist_ok=True)
        shas = []
        for text in ("one\n", "two\n"):
            (repo / changed).write_text(text)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", text], check=True)
            shas.append(subprocess.run([*git, "rev-parse", "HEAD"], check=True, text=True,
                                       capture_output=True).stdout.strip())
        rel = Path(host.cfg["current"]).parent
        for rid, sha in zip(("old", "new"), shas):
            (rel / rid / "RELEASE.json").write_text(json.dumps({"id": rid, "sha": sha,
                                                                "overlays": []}))
        host.env["PLX_REPO"] = str(repo)

    def test_a_release_that_changes_nothing_the_api_reads_keeps_it_serving(self):
        host = Host(self, caddy=True)
        self._releases_from_commits(host, "docs/notes.md")
        host.switch_to_new()
        got = host.restart()
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertIn("kept: no API change since old", got.stdout)
        starts = [c for c in host.calls() if c.startswith("start")]
        self.assertEqual(starts, ["start plx-api old"])          # no standby, no restart
        self.assertEqual(host.serving("plx-api")["code"], "code-old")   # the same process
        self.assertIsNone(host.serving("standby"))

    def test_a_release_that_changes_the_api_code_switches_warm(self):
        host = Host(self, caddy=True)
        self._releases_from_commits(host, "operating/x.py")
        host.switch_to_new()
        got = host.restart()
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertIn("warm switch", got.stdout)
        starts = [c for c in host.calls() if c.startswith("start")]
        self.assertEqual(starts, ["start plx-api old", "start plx-api@standby new",
                                  "start plx-api new"])

    def test_a_standby_that_never_gets_warm_leaves_the_old_api_serving(self):
        host = Host(self, caddy=True)
        (Path(host.cfg["current"]).parent / "new" / "NOT_READY").write_text("")
        host.switch_to_new()
        loop = Loop(host)
        loop.start()
        got = host.restart()
        loop.stop.set()
        loop.join()
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("not ready within", got.stderr)
        self.assertIn("still serves", got.stderr)
        self.assertEqual(loop.failed, 0, loop.errors)
        self.assertEqual(set(loop.answers), {(host.api_port, "code-old")})
        self.assertIsNone(host.serving("standby"))
        self.assertEqual(list(host.hold.iterdir()), [])

    def test_a_deploy_stopped_on_the_standby_finishes_back_on_plx_api(self):
        host = Host(self, caddy=True)
        host.switch_to_new()
        host.systemctl("start", "plx-api@standby")
        host.systemctl("stop", "plx-api")
        self.assertTrue(host.wait_through(lambda r: r and r["port"] == "standby"))
        (host.hold / "hold-plx-api-standby").parent.mkdir(exist_ok=True)
        (host.hold / "hold-plx-api-standby").write_text("")   # an interrupted run's leftover
        got = host.restart()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(host.wait_through(
            lambda r: r == {"port": host.api_port, "code": "code-new"}))
        self.assertIsNone(host.serving("standby"))
        self.assertEqual(list(host.hold.iterdir()), [])

    def test_a_switch_whose_signed_in_read_fails_rolls_the_api_back_to_the_old_code(self):
        """The warm switch succeeds, the authenticated /api/me read then fails on the new code:
        the self-rollback's nested restart switches the API back (11-15 review: it waited 900 s
        on the first switch's lock and left plx-api on the failed release)."""
        host = Host(self, caddy=True)
        mint = host.tmp / "bin" / "plx-mint-link"   # the signed-in read fails on code-new only
        mint.write_text('#!/bin/sh\ncurl -s "$PLX_API_URL/api/ping" | grep -q code-new && exit 1\n'
                        'mkdir -p "$HOME/.config/plx"\n'
                        'echo "http://x/#ticket=t" > "$HOME/.config/plx/workbench-access-link"\n')
        mint.chmod(0o755)
        host.env.update({"PLX_RELEASE_AUTH_PROBE": "required", "PLX_MINT_LINK": str(mint),
                         "HOME": str(host.tmp / "home")})
        host.switch_to_new()
        t0 = time.monotonic()
        got = host.restart()
        self.assertLess(time.monotonic() - t0, budget(90))
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("health=200 auth=failed", got.stdout)
        self.assertIn("rolled back to old (", got.stderr)
        self.assertNotIn("another switch holds the lock", got.stdout + got.stderr)
        self.assertEqual(os.readlink(host.cfg["current"]), "old")
        self.assertEqual(host.serving("plx-api")["code"], "code-old")
        self.assertIsNone(host.serving("standby"))
        self.assertTrue(host.wait_through(
            lambda r: r == {"port": host.api_port, "code": "code-old"}))
        self.assertEqual(list(host.hold.iterdir()), [])

    def test_a_failed_warm_switch_with_nothing_to_roll_back_to_runs_one_release(self):
        host = Host(self, caddy=True)
        (Path(host.cfg["current"]).parent / "new" / "NOT_READY").write_text("")
        host.switch_to_new()
        os.unlink(Path(host.cfg["current"]).with_name("previous"))
        got = host.restart()
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("no rollback", got.stderr)
        self.assertIn("restarted in place on it (health=200)", got.stderr)
        self.assertEqual(host.serving("plx-api")["code"], "code-new")   # the app's release
        self.assertIsNone(host.serving("standby"))

    def test_nothing_but_the_standby_socket_is_ever_sent_a_reader(self):
        """With 8780 down (the app stopped while unused, a crash), a dev or mock API on the
        old standby port 8781 or any other port gets nothing: Caddy answers 502 as before."""
        host = Host(self, caddy=True)
        stray = free_port()
        seen: list = []

        class Stray(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"port": "stray", "code": "dev"}')
        for port in (stray, 8781):
            try:
                server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Stray)
            except OSError:
                continue   # 8781 in use on this machine: the free port still checks it
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        host.systemctl("stop", "plx-api")
        for _ in range(5):
            self.assertIsNone(host.through())
        self.assertEqual(seen, [])

    def test_a_caddy_that_does_not_send_to_the_standby_restarts_in_place(self):
        host = Host(self, caddy=True)
        host.env["PLX_API_STANDBY_SOCKET"] = str(host.tmp / "elsewhere" / "api.sock")
        host.switch_to_new()
        got = host.restart()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("the running Caddy does not send /api/* to", got.stderr)
        self.assertIn("(cold:", got.stdout)


class WarmSwitchRefused(unittest.TestCase):
    """Each reason the warm switch may not run: the API restarts in place and the reason is
    named (a cold restart, as before ARCH-T6, rather than no deploy)."""

    def refused(self, **cfg) -> tuple[subprocess.CompletedProcess, Host]:
        host = Host(self, caddy=False, **cfg)
        host.switch_to_new()
        got = host.restart()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("(cold:", got.stdout)
        self.assertEqual(host.serving("plx-api")["code"], "code-new")
        self.assertFalse([c for c in host.calls() if "@" in c and c.startswith("start")])
        return got, host

    def test_no_template_installed(self):
        got, _ = self.refused(no_template=True)
        self.assertIn("plx-api@.service is not installed", got.stderr)

    def test_a_setting_that_differs_names_the_setting_only(self):
        got, _ = self.refused(standby_user="root")
        self.assertIn("differs from plx-api in: User", got.stderr)
        self.assertNotIn("PGAPPNAME", got.stderr)            # name and port normalised

    def test_no_memory_for_a_second_api(self):
        got, _ = self.refused(avail_mb=3000, api_bytes=2 << 30)
        self.assertIn("memory: 3000 MB available, a second API needs ~2048 MB", got.stderr)

    def test_asked_cold(self):
        host = Host(self, caddy=False)
        host.switch_to_new()
        got = host.restart("--cold")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("warm switch off: --cold asked", got.stderr)
        self.assertEqual(host.serving("plx-api")["code"], "code-new")

    def test_asked_cold_while_only_the_standby_serves_starts_plx_api(self):
        host = Host(self, caddy=False)
        host.switch_to_new()
        host.systemctl("start", "plx-api@standby")
        host.systemctl("stop", "plx-api")
        got = host.restart("--cold")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(host.serving("plx-api")["code"], "code-new")
        self.assertIsNone(host.serving("standby"))

    def test_postgres_without_room_for_a_second_api(self):
        bins = Path(tempfile.mkdtemp(prefix="t6pg-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, bins, True)
        psql = bins / "psql"
        psql.write_text("#!/bin/sh\ncat >/dev/null\necho \"$STUB_PG_ROW\"\n")
        psql.chmod(0o755)
        for row, refused in (("64|40|20", True), ("64|20|20", False), ("garbage", True)):
            host = Host(self, caddy=False)
            host.env.update({"PLX_ONTOLOGY_PG": "only", "PLX_PG_DSN": "dbname=none",
                             "STUB_PG_ROW": row, "PATH": f"{bins}:{host.env['PATH']}"})
            host.switch_to_new()
            got = host.restart()
            self.assertIn("(cold:", got.stdout, row)
            if refused:
                self.assertRegex(got.stderr, r"Postgres has no room|could not be counted", row)
            else:   # past the Postgres check: the next one (no Caddy here) refuses
                self.assertNotIn("Postgres", got.stderr, row)
                self.assertIn("the running Caddy does not send", got.stderr, row)
            self.assertNotIn("dbname=none", got.stdout + got.stderr)   # the DSN never shown


class CaddyfileRouting(unittest.TestCase):
    @unittest.skipIf(CADDY is None, "caddy not installed")
    def test_the_api_goes_to_8780_then_the_standby_socket_and_ready_is_not_served(self):
        out = subprocess.run([CADDY, "adapt", "--config", str(DEPLOY / "Caddyfile"),
                              "--adapter", "caddyfile"], capture_output=True, text=True,
                             check=True, env={**os.environ, "PLX_APP_HOST": "wb.example"}).stdout
        text = json.dumps(json.loads(out), separators=(",", ":"))
        proxy = json.loads(re.search(r'\{"flush_interval":-1,"handler":"reverse_proxy".*?'
                                     r'"upstreams":\[[^\]]*\]\}', text).group(0))
        self.assertEqual([u["dial"] for u in proxy["upstreams"]],
                         ["127.0.0.1:8780", "unix//run/plx-api-standby/api.sock"])
        self.assertEqual(proxy["load_balancing"]["selection_policy"], {"policy": "first"})
        self.assertGreater(proxy["load_balancing"]["try_duration"], 0)   # a refusal goes on
        self.assertIn('{"handler":"static_response","status_code":404}],"match":[{"path":'
                      '["/api/ready"]}]', text)

    def test_the_standby_unit_serves_only_on_its_own_socket_directory(self):
        """The rendered template, the Caddyfile and plx-release name one socket: the unit's
        RuntimeDirectory (its user's alone, 0711, gone when it stops)."""
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["python3", str(DEPLOY / "render.py"), "--out", tmp], check=True,
                           capture_output=True)
            unit = Path(tmp, "plx-api@.service").read_text()
            rest = Path(tmp, "plx-api.service").read_text()
            stop = Path(tmp, "plx-api.service.d", "zz-stop.conf").read_text()
        self.assertIn("--unix-socket /run/plx-api-%i/api.sock", unit)
        self.assertNotRegex(unit, r"(?m)^ExecStart=.*--port")
        self.assertIn("RuntimeDirectory=plx-api-%i\nRuntimeDirectoryMode=0711", unit)
        for text in (unit, rest, stop):   # the drain (180 s) and the joins (20 s) fit
            self.assertIn("TimeoutStopSec=210", text)
            self.assertIn("KillMode=mixed", text)   # the Ask helpers outlive the SIGTERM
        self.assertNotRegex(unit, r"(?m)^\[Install\]")
        # every setting plx-release compares (PARITY) is the same in both units' own files:
        # 11-14..11-18 the template said Restart=on-failure, plx-api Restart=always, and every
        # deploy restarted the API cold ("differs in: Restart")
        parity = set(re.search(r'PARITY="([^"]*)"', TOOL.read_text()).group(1).split())

        def settings(text: str) -> dict:
            got: dict = {}
            for line in text.splitlines():
                key, eq, value = line.partition("=")
                if eq and key in parity and key != "ExecStart":
                    got.setdefault(key, []).append(value.strip())
            got.setdefault("Slice", ["system.slice"])   # a service's default
            return got
        self.assertEqual(settings(rest), settings(unit))
        tool = TOOL.read_text()
        self.assertIn("STANDBY=${PLX_API_STANDBY:-standby}", tool)
        self.assertIn("/run/plx-api-$STANDBY/api.sock", tool)
        self.assertIn("unix//run/plx-api-standby/api.sock", (DEPLOY / "Caddyfile").read_text())


class ApiRestartPaths(unittest.TestCase):
    """plx-release keeps the API only when a release changes nothing it runs: every path the API
    process imports or reads restarts it, and a path nobody listed restarts it too."""

    RESTART = ["operator_api/app.py", "operator_api/auth.py", "operating/proactive_engine_inbox.py",
               "ontology/model.py", "pyproject.toml", "requirements.txt", "app.py", "newpkg/x.py",
               "config/policy.yaml", "deploy/env.example", "deploy/Caddyfile.d/x",
               "operator_api/README.md", "deploy/systemd2/x", "apps/web/x.ts"]
    KEEP = ["app/src/lib/api.ts", "docs/TECHNICAL.md", "README.md", "desktop/main.js",
            "devloop/x.md", "deploy/ops/watch.sh", "deploy/release/plx-release",
            "deploy/spark/run.sh", "deploy/systemd/plx-api.service", "deploy/backup/b.sh",
            "deploy/Caddyfile", "tests/test_x.py", "operating/tests/test_y.py"]

    def test_each_path_restarts_or_keeps_the_api_as_listed(self):
        m = re.search(r"^API_IGNORED='([^']+)'$", TOOL.read_text(), re.M)
        self.assertIsNotNone(m)
        paths = self.RESTART + self.KEEP
        # the same grep -vE the script runs: what it prints is what restarts the API
        got = subprocess.run(["grep", "-vE", m.group(1)], input="\n".join(paths) + "\n",
                             text=True, capture_output=True).stdout.split()
        self.assertEqual(got, self.RESTART)


if __name__ == "__main__":
    unittest.main()
