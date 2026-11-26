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
  (in recovery, its slot on the box kept, replay within 10 min), the Spark's standby socket folder (~/plx-ai/standby.json, default ~/plx-ai/standby-run)
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
# no-sandbox mode (a Spark where bwrap cannot start): the sudo stand-in also hands the
# path map to the process it starts. The box's evals start Claude as `sudo -n -u X env -i
# HOME=... claude`; `env -i` clears LD_PRELOAD, so without this Claude would look for
# /var/lib/plx-workbench (which only exists on the box) instead of the Spark's copy.
SUDO_SHIM_PRELOAD = """#!/usr/bin/python3
import os, re, sys
a = sys.argv[1:]
while a:
    if a[0] in ("-n", "-E", "-H"): a = a[1:]
    elif a[0] in ("-u", "-g"): a = a[2:]
    elif a[0] == "--": a = a[1:]; break
    elif a[0].startswith("-"): a = a[1:]
    else: break
if not a: sys.exit(2)
maps = sorted((m.split("=")[:2] for m in os.environ.get("PLX_PATHMAP", "").split(":") if m.count("=") == 2),
              key=lambda m: -len(m[0]))
def real(v):
    # a program the map cannot reach (Claude's own file access) gets the Spark's paths in its
    # arguments and in KEY=VALUE words
    k, eq, val = v.partition("=") if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", v) else ("", "", v)
    for dst, src in maps:
        if val == dst or val.startswith(dst + "/"):
            val = src + val[len(dst):]
            break
    return f"{k}={val}" if eq else val
a = [a[0]] + [real(x) for x in a[1:]]
if os.path.basename(a[0]) == "env" and "-i" in a[1:3]:
    keep = [f"{k}={os.environ[k]}" for k in ("LD_PRELOAD", "PLX_PATHMAP") if k in os.environ]
    i = a.index("-i") + 1
    a = a[:i] + keep + a[i:]
os.execvp(a[0], a)
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import standby_guard
    if pick is None:
        pick = standby_guard.pick
    try:
        use, why = pick()
    except Exception as exc:  # noqa: BLE001 - a failed guard reads the box
        use, why = False, f"standby guard failed ({type(exc).__name__})"
    print(f"spark_run: postgres = {'the standby' if use else 'the box'}: {why}", file=sys.stderr)
    if not use:
        return AI / "run", {}
    try:
        socket = standby_guard.host_standby()[0]
    except standby_guard.StandbySettingsError as exc:  # a broken settings file reads the box, said
        print(f"spark_run: postgres = the box: {standby_guard.MARKER} {exc}", file=sys.stderr)
        return AI / "run", {}
    return Path(socket), {
        "PLX_PG_KEYSET_DSN": f"host={PRIMARY_IN_SANDBOX} dbname=plx user=plx_eval"}


USERNS_REFUSED = re.compile(r"namespace|uid map|gid map|Operation not permitted|Permission denied|apparmor",
                            re.I)


def sandbox_available(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """True when bwrap starts here; False only when it REFUSES (the kernel or AppArmor says no to
    user namespaces, e.g. sb4ec): then the run uses the path-map mode. A timeout (a loaded
    Spark) is tried once more and then fails loudly, and so does any other failure (no bwrap,
    an unknown error): none of them may quietly drop a run's sandbox."""
    last = ""
    for _ in range(2):
        try:
            got = run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True, text=True,
                      timeout=60)
        except subprocess.TimeoutExpired:
            last = "bwrap did not answer within 60 s"
            continue
        except OSError as exc:
            _fail(f"cannot run bwrap: {exc}")
        if got.returncode == 0:
            return True
        if USERNS_REFUSED.search(got.stderr or ""):
            return False
        _fail(f"bwrap failed for a reason other than user namespaces being refused: "
              f"{(got.stderr or '').strip()[-200:]}")
    _fail(f"{last} (twice); not choosing a mode for the run")
    return False


def mounts(spec: dict, wt: Path, pg: Path | None = None, sandbox: bool = True
           ) -> list[tuple[str, str, bool]]:
    """(source on the Spark, path in the sandbox, read-only) for every bind of the run;
    ``pg``: the socket folder read as /run/postgresql (the box's balancer by default);
    ``sandbox`` False: the same list drives the path map, and /tmp is not remapped (the run's
    own tmp goes through TMPDIR, so a path handed to a process outside the map still holds)."""
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
    if sandbox:
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
    (shim / "sudo").write_text(SUDO_SHIM if sandbox else SUDO_SHIM_PRELOAD)
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


def pathmap(binds: list[tuple[str, str, bool]]) -> str:
    """PLX_PATHMAP for deploy/spark/pathmap.c: ``<in job>=<on the Spark>=<ro|rw>`` per bind."""
    return ":".join(f"{dst}={src}={'ro' if ro else 'rw'}" for src, dst, ro in binds)


def skeleton(binds: list[tuple[str, str, bool]], base: Path) -> list[tuple[str, str, bool]]:
    """Read-only binds for the folders on the way to a bind target that do not exist on this Spark
    (/data, /data/plx-plan ...): under bwrap they exist because the sandbox rebuilds them
    (``bwrap_args``), here the map points each at an empty real folder, so a job's
    ``os.makedirs('/data/plx-plan/out', exist_ok=True)`` finds its parents instead of trying to
    create /data. Folders that exist on the Spark (/usr/local/bin, /var/lib, /run) are left alone."""
    seen: dict[str, str] = {}
    for _src, dst, _ro in binds:
        for parent in Path(dst).parents:
            d = str(parent)
            if d != "/" and not parent.exists() and d not in seen and all(d != b[1] for b in binds):
                seen[d] = str(base / d.lstrip("/"))
                Path(seen[d]).mkdir(parents=True, exist_ok=True)
    return [(src, dst, True) for dst, src in seen.items()]


def build_preload() -> Path:
    """Compile pathmap.c once per content (the Spark has gcc); the .so is keyed by its hash."""
    src = Path(__file__).resolve().parent / "pathmap.c"
    digest = __import__("hashlib").sha256(src.read_bytes()).hexdigest()[:12]
    so = RUN / f"pathmap-{digest}.so"
    if not so.exists():
        tmp = RUN / f".pathmap-{os.getpid()}.so"
        got = subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-o", str(tmp), str(src), "-ldl"],
                             capture_output=True, text=True)
        if got.returncode:
            _fail(f"cannot build pathmap.so: {got.stderr.strip()[-300:]}")
        os.replace(tmp, so)
    return so


_EPHEMERAL_RUN_NAMES = frozenset({"utmp", "nologin"})
_EPHEMERAL_RUN_SUFFIXES = (".new", ".tmp", ".lock", ".pid")


def _ephemeral_run_entry(name: str) -> bool:
    """True for transient host files under /run (such as pam_motd's motd.dynamic.new or
    daemon pid/lock files) that can appear and vanish between listing /run and execing
    bwrap."""
    return (name.startswith("motd")
            or name in _EPHEMERAL_RUN_NAMES
            or name.endswith(_EPHEMERAL_RUN_SUFFIXES))


def _needs_mountpoint(src: str, dst: Path) -> bool:
    """True when ``dst`` cannot be bound directly over the host entry (missing, a symlink,
    or a file/directory kind mismatch): its ancestor directories must be rebuilt with
    ``--dir`` so bwrap can create a clean mountpoint at ``dst``."""
    try:
        if dst.is_symlink() or not dst.exists():
            return True
        return Path(src).is_dir() != dst.is_dir()
    except OSError:
        return True


def bwrap_args(binds: list[tuple[str, str, bool]], root: str = "/") -> list[str]:
    """The bwrap arguments: a fresh root whose entries are the Spark's own, except that every
    folder on the way to a bind target that does not exist here (or is a symlink or kind
    mismatch) is rebuilt from its entries (so a target like /data/plx-state or
    /usr/local/bin/claude can be created cleanly), then the binds, shortest target first.
    Under /run, ephemeral host files (motd.dynamic*, *.new, *.tmp, *.lock, *.pid) are
    skipped and remaining entries use --ro-bind-try so a vanishing transient file never
    kills bwrap."""
    targets = [Path(d) for _, d, _ in binds]
    expand_targets = [Path(d) for s, d, _ in binds if _needs_mountpoint(s, Path(d))]
    root_path = Path(root)
    run_dir = root_path / "run"
    args: list[str] = []

    def needs_expand(path: Path) -> bool:
        return any(t != path and path in t.parents for t in expand_targets)

    def walk(dirpath: Path) -> None:
        try:
            entries = sorted(os.listdir(dirpath))
        except OSError:
            return
        in_run = dirpath == run_dir or run_dir in dirpath.parents
        bind_flag = "--ro-bind-try" if in_run else "--ro-bind"
        for e in entries:
            if in_run and _ephemeral_run_entry(e):
                continue
            p = dirpath / e
            if str(p) in ("/proc", "/dev"):
                continue
            if p in targets and not needs_expand(p):
                continue                       # bound below
            try:
                if p.is_symlink():
                    args.extend(["--symlink", os.readlink(p), str(p)])
                elif p.is_dir() and needs_expand(p):
                    args.extend(["--dir", str(p)])
                    walk(p)
                elif p.exists():
                    args.extend([bind_flag, str(p), str(p)])
            except OSError:
                continue

    walk(root_path)
    for t in sorted(targets, key=lambda x: len(x.parts)):
        for parent in reversed(t.parents):
            if str(parent) != "/" and not parent.exists() and not any(
                    a == str(parent) for a in args):
                args.extend(["--dir", str(parent)])
    for src, dst, ro in sorted(binds, key=lambda b: len(Path(b[1]).parts)):
        args.extend(["--ro-bind" if ro else "--bind", src, dst])
    return ["--proc", "/proc", "--dev-bind", "/dev", "/dev", *args, "--die-with-parent"]


def _which(name: str, path: str, binds: list[tuple[str, str, bool]]) -> str:
    if "/" in name:
        return next((src + name[len(dst):] for src, dst, _ in sorted(
            binds, key=lambda b: -len(b[1])) if name == dst or name.startswith(dst + "/")), name)
    """The command's real path on the Spark (a PATH entry like /data/venv/bin maps through the
    binds); this process is not under pathmap.so, the command it starts is."""
    for d in path.split(":"):
        cand = f"{d}/{name}"
        real = next((src + cand[len(dst):] for src, dst, _ in sorted(
            binds, key=lambda b: -len(b[1])) if cand.startswith(dst + "/")), cand)
        if os.access(real, os.X_OK):
            return real
    _fail(f"command not found: {name}")
    return name


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
    sandboxed = sandbox_available() and spec.get("sandbox") != "never"
    if not sandboxed and spec.get("sandbox") == "always":
        _fail("the sandbox is required (--sandbox) but bwrap cannot start on this Spark")
    if not sandboxed and spec.get("pg", "box") == "box":
        # a Spark that cannot sandbox reads its own hot standby of the box (plx_eval, read-only by
        # being a standby); the box's balancer stays the fallback when the standby guard fails
        spec = {**spec, "pg": "standby"}
    pg, pg_env = database(spec)
    binds = mounts(spec, wt, pg, sandbox=sandboxed)
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
    if not sandboxed:
        print("spark_run: NO SANDBOX on this Spark (bwrap cannot start): paths remapped by "
              "pathmap.so, writes refused under read-only binds, Postgres = plx_eval on the "
              "Spark's standby", file=sys.stderr)
        tmp = run_tmp(spec)
        env.update({"LD_PRELOAD": str(build_preload()),
                    "PLX_PATHMAP": pathmap(binds + skeleton(binds, Path(tmp) / ".skel")),
                    "TMPDIR": tmp, "PLX_SPARK_NO_SANDBOX": "1"})
        env["PGHOST"] = str(pg)                      # the real folder: libpq needs no map
        real_cwd = next((src + cwd[len(dst):] for src, dst, _ in sorted(
            binds, key=lambda b: -len(b[1])) if cwd == dst or cwd.startswith(dst + "/")), cwd)
        if not Path(real_cwd).is_dir():
            _fail(f"working directory {cwd} is not mapped on this Spark")
        os.chdir(real_cwd)
        sys.stdout.flush()
        os.execvpe(_which(cmd[0], env["PATH"], binds), cmd, env)
    argv = ["bwrap", *bwrap_args(binds), "--chdir", cwd, "--", *cmd]
    sys.stdout.flush()
    os.execvpe("bwrap", argv, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
