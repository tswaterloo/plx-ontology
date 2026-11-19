"""Runs ON a Spark (spark_run.sh copies it there): one command in a sandbox that looks like the
box to it. Reads one JSON line on stdin (the run: sha, tree path, cwd, shipped paths, state,
environment), checks out the sha, then execs ``bwrap`` with:

* ``/data/OntologyProject`` and the caller's tree path: the checkout of the sha;
* ``/data/venv``: the Spark's Python environment (~/plx-tests/venv, aarch64);
* ``/data/plx-state``: the Spark's state copy (deploy/spark/spark_state_sync.py) for
  ``state=copy``, an empty folder for ``state=none``;
* each shipped path at its own absolute path (~/plx-run/files/<path>);
* ``/run/postgresql``: ~/plx-ai/run, whose socket is spark_pg_balance.py (spread over the
  box's reverse tunnels), so ``postgresql:///plx`` reaches the box's Postgres. The box end
  maps every login to ``plx_eval`` (read-only);
  with ``pg: standby`` (spark_run.sh ``--pg standby``) and standby_guard.py's check passed
  (in recovery, its slot on the box kept, replay within 10 min), ~/plx-ai/standby-run
  instead: the Spark's own hot standby of the box (read-only by being one), the box's
  socket then at ``/run/plx-primary`` (``PLX_PG_KEYSET_DSN``: a reader asks the box to keep
  a long key list's shared set there, migration 0050); a failed check reads the box, said
  on stderr;
* ``/usr/local/bin/claude``: the same Claude Code version as the box, for this CPU;
  ``/var/lib/plx-workbench`` and ``/var/lib/plx-autopilot/claude-home``: their Spark copies,
  holding only the access token the box syncs (deploy/spark/spark_cred_sync.sh);
* a ``sudo`` that runs its command as the login user (the box's evals start the Workbench's
  Claude with ``sudo -n -u plx-workbench ... env -i HOME=/var/lib/plx-workbench``; here that
  home is the copy above). It is first on PATH and is bound over the Spark's own sudo, so a
  job that sets its own PATH reaches it too.

Everything is read-only in the sandbox except the ``--out`` folders, a ``/tmp`` of the run's
own (~/plx-run/tmp/<name>-<pid>, gone after two days), the two Claude homes (Claude writes
its session files there) and the state copy (the stores open their SQLite read-write; the
copy is the Spark's replica, never copied back, rewritten by each sync): the Spark's own
files, the checkout and the shipped paths cannot be changed by a job. Nothing in the sandbox can write the box: Postgres is
read-only by role (``plx_eval``, a full reader: owner-level evals only; a staff reader's eval
stays on the box until a scoped Spark role exists), outputs leave only through the ``--out``
folders spark_run.sh copies back.
"""
from __future__ import annotations

from typing import Callable

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
RUN = HOME / "plx-run"
TESTS = HOME / "plx-tests"
AI = HOME / "plx-ai"
STATE_COPY = HOME / "plx-state-copy"
# usage metering (operating/usage_meter.py): the Spark's exporter settings, KEY=VALUE lines;
# its endpoint is the box receiver through the reverse tunnel (127.0.0.1:4318 on the Spark)
OTEL_FILE = AI / "otel.env"
CONTENT_SWITCHES = frozenset({
    "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_TOOL_CONTENT",
    "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_RAW_API_BODIES"})
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]+$")
# where a sudo may sit on a Spark's PATH (/bin and /sbin are links into /usr there)
SUDO_PATHS = ("/usr/bin/sudo", "/usr/sbin/sudo", "/usr/local/bin/sudo", "/usr/local/sbin/sudo")
SUDO_SHIM = """#!/bin/sh
# spark_run sandbox: the box's evals call `sudo -n -u <user> -g <group> cmd...`; here every
# command already runs as the only user there is.
while [ $# -gt 0 ]; do case "$1" in -n|-E|-H) shift ;; -u|-g) shift 2 ;; --) shift; break ;; -*) shift ;; *) break ;; esac; done
exec "$@"
"""


def _fail(msg: str, code: int = 97) -> None:
    print(f"spark_run (remote): {msg}", file=sys.stderr)
    sys.exit(code)


def telemetry_env(name: str, path: Path = OTEL_FILE) -> dict[str, str]:
    """The metering settings from ``OTEL_FILE`` (none when it is missing or telemetry is
    off), never a content switch or exporter header; the run's name joins the resource
    attributes as ``plx.run``."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    env = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if (sep and re.fullmatch(r"(OTEL_[A-Z0-9_]+|CLAUDE_CODE_ENABLE_TELEMETRY)", key)
                and key not in CONTENT_SWITCHES and "HEADERS" not in key):
            env[key] = value.strip()
    if env.get("CLAUDE_CODE_ENABLE_TELEMETRY", "").strip() in ("", "0", "false"):
        return {}
    run = re.sub(r"[^A-Za-z0-9._-]", "_", name or "run")
    env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(
        p for p in (env.get("OTEL_RESOURCE_ATTRIBUTES", ""), f"plx.run={run}") if p)
    return env


def checkout(sha: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        _fail("sha must be 40 hex characters")
    wt = RUN / "wt" / sha
    if not wt.is_dir():
        (RUN / "wt").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(TESTS / "repo.git"), "worktree", "prune"], check=False)
        got = subprocess.run(["git", "-C", str(TESTS / "repo.git"), "worktree", "add", "-q",
                              "--detach", str(wt), sha], capture_output=True, text=True)
        if got.returncode:
            _fail(f"checkout failed: {got.stderr.strip()[-300:]}")
    return wt


def run_tmp(spec: dict) -> str:
    """The run's own /tmp (~/plx-run/tmp/<name>-<pid>); those of runs over two days old go."""
    base = RUN / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 2 * 86400
    for old in base.iterdir():
        try:
            if old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(spec.get("name") or "run"))[:60]
    tmp = base / f"{name}-{os.getpid()}"
    tmp.mkdir(mode=0o700)
    return str(tmp)


PRIMARY_IN_SANDBOX = "/run/plx-primary"


def database(spec: dict, pick: Callable[[], tuple[bool, str]] | None = None
             ) -> tuple[Path, dict[str, str]]:
    """(the socket folder the run reads as /run/postgresql, environment it adds): the box's
    balancer unless the run asks for the standby (``pg: standby``) and its guard passes."""
    if spec.get("pg", "box") != "standby":
        return AI / "run", {}
    if pick is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import standby_guard
        pick = standby_guard.pick
    try:
        use, why = pick()
    except Exception as exc:  # noqa: BLE001 - a failed guard reads the box
        use, why = False, f"standby guard failed ({type(exc).__name__})"
    print(f"spark_run: postgres = {'the standby' if use else 'the box'}: {why}", file=sys.stderr)
    if not use:
        return AI / "run", {}
    return AI / "standby-run", {
        "PLX_PG_KEYSET_DSN": f"host={PRIMARY_IN_SANDBOX} dbname=plx user=plx_eval"}


def mounts(spec: dict, wt: Path, pg: Path | None = None) -> list[tuple[str, str, bool]]:
    """(source on the Spark, path in the sandbox, read-only) for every bind of the run;
    ``pg``: the socket folder read as /run/postgresql (the box's balancer by default)."""
    out: list[tuple[str, str, bool]] = []
    venv = TESTS / "venv"
    out.append((str(venv), "/data/venv", True))
    out.append((str(wt), "/data/OntologyProject", True))
    tree = spec.get("tree") or ""
    if tree and tree != "/data/OntologyProject":
        out.append((str(wt), tree, True))
    state = spec.get("state", "copy")
    if state == "copy":
        if not STATE_COPY.is_dir():
            _fail("no state copy on this Spark (run deploy/spark/spark_state_sync.py --host ...)")
        # writable: the stores open their SQLite read-write (WAL side files, an attached
        # mirror); the copy is the Spark's own replica, never copied back, and each sync
        # writes its pages again
        out.append((str(STATE_COPY), "/data/plx-state", False))
    else:
        empty = RUN / "empty-state"
        empty.mkdir(parents=True, exist_ok=True)
        out.append((str(empty), "/data/plx-state", True))
    outs = list(spec.get("out") or [])
    for p in [*spec.get("ship", []), *outs]:
        src = RUN / "files" / p.lstrip("/")
        if not src.exists():
            _fail(f"shipped path missing on the Spark: {p}")
        out.append((str(src), p, p not in outs))
    out.append((run_tmp(spec), "/tmp", False))
    pg = pg or AI / "run"
    out.append((str(pg), "/run/postgresql", True))
    if pg != AI / "run":
        out.append((str(AI / "run"), PRIMARY_IN_SANDBOX, True))
    claude = AI / "bin" / "claude"
    if claude.exists():
        out.append((str(claude.resolve()), "/usr/local/bin/claude", True))
    for name, target in (("plx-workbench", "/var/lib/plx-workbench"),
                         ("autopilot", "/var/lib/plx-autopilot/claude-home")):
        home = AI / "home" / name
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        out.append((str(home), target, False))
    shim = AI / "shim"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "sudo").write_text(SUDO_SHIM)
    os.chmod(shim / "sudo", 0o755)
    # The shim also stands in for the Spark's own sudo, so a job that sets its own PATH (a
    # bench's `env PATH=...` wrapper calling `sudo -n -u plx-workbench ... claude`) reaches it
    # too: the real sudo cannot run in the sandbox (no new privileges) and every Claude
    # session it starts would exit 1 before its first turn.
    for real in SUDO_PATHS:
        if Path(real).exists():
            out.append((str(shim / "sudo"), real, True))
    for src, dst, _ in out:
        if not SAFE_PATH.match(dst) or ".." in dst.split("/"):
            _fail(f"refused sandbox path {dst!r}")
    return out


def bwrap_args(binds: list[tuple[str, str, bool]], root: str = "/") -> list[str]:
    """The bwrap arguments: a fresh root whose entries are the Spark's own, except that every
    folder on the way to a bind target that does not exist here is rebuilt from its entries
    (so a target like /data/plx-state or /usr/local/bin/claude can be created), then the binds,
    shortest target first."""
    targets = [Path(d) for _, d, _ in binds]
    args: list[str] = []

    def needs_expand(path: Path) -> bool:
        return any(t != path and path in t.parents and not t.exists() for t in targets)

    def walk(dirpath: Path) -> None:
        try:
            entries = sorted(os.listdir(dirpath))
        except OSError:
            return
        for e in entries:
            p = dirpath / e
            if str(p) in ("/proc", "/dev"):
                continue
            if p in targets and not needs_expand(p):
                continue                       # bound below
            if p.is_symlink():
                args.extend(["--symlink", os.readlink(p), str(p)])
            elif p.is_dir() and needs_expand(p):
                args.extend(["--dir", str(p)])
                walk(p)
            elif p.exists():
                args.extend(["--ro-bind", str(p), str(p)])

    walk(Path(root))
    for t in sorted(targets, key=lambda x: len(x.parts)):
        for parent in reversed(t.parents):
            if str(parent) != "/" and not parent.exists() and not any(
                    a == str(parent) for a in args):
                args.extend(["--dir", str(parent)])
    for src, dst, ro in sorted(binds, key=lambda b: len(Path(b[1]).parts)):
        args.extend(["--ro-bind" if ro else "--bind", src, dst])
    return ["--proc", "/proc", "--dev-bind", "/dev", "/dev", *args, "--die-with-parent"]


def main() -> int:
    line = sys.stdin.readline()
    try:
        spec = json.loads(line)
    except ValueError:
        _fail("no run description on stdin")
    cmd = spec.get("cmd") or []
    if not cmd or not all(isinstance(c, str) for c in cmd):
        _fail("no command")
    wt = checkout(spec.get("sha", ""))
    pg, pg_env = database(spec)
    binds = mounts(spec, wt, pg)
    env = {k: v for k, v in os.environ.items()
           if k in ("HOME", "USER", "LOGNAME", "LANG", "TERM", "SHELL", "XDG_RUNTIME_DIR",
                    "DBUS_SESSION_BUS_ADDRESS")}
    env.update(telemetry_env(spec.get("name") or ""))
    for k, v in (spec.get("env") or {}).items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and isinstance(v, str):
            env[k] = v
    env["PATH"] = f"{AI / 'shim'}:/data/venv/bin:/usr/local/bin:/usr/bin:/bin"
    env["PGHOST"] = "/run/postgresql"
    env["PGUSER"] = "plx_eval"
    env.update(pg_env)
    env["PLX_SPARK_RUN"] = spec.get("name") or "1"
    env.setdefault("TZ", "America/New_York")
    cwd = spec.get("cwd") or "/data/OntologyProject"
    argv = ["bwrap", *bwrap_args(binds), "--chdir", cwd, "--", *cmd]
    sys.stdout.flush()
    os.execvpe("bwrap", argv, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
