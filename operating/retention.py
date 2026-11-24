"""Nightly retention on /data (speed specs P3): what can go, in this order, each step logging
the GB before and after and stopping at its first error. Dry run by default: it prints what
it would remove and the GB it would free; ``--apply`` removes.

1. ``biz`` build leftovers: ``*_next``, ``*_ship``, ``*_next_stage``, ``lineage_*_next``
   tables no backend reads (``business_views_access.sweep_leftovers``, the refresh's own rule).
2. releases beyond the newest ``KEEP_RELEASES`` (``plx-release prune --keep``: it skips any
   release a process runs in).
3. Spark-test logs (``/data/scratch/runs/spark``) older than ``LOG_DAYS`` days.
4. worktrees under ``/data/scratch/wt`` that are (merged: the branch was pushed, its remote
   branch equals HEAD and HEAD is in origin/main, so a worktree with no commits of its own
   never counts; AND idle over ``IDLE_HOURS``: no file or git state touched) or (last commit
   and files older than ``STALE_DAYS`` days), with nothing uncommitted, and not in the
   exclude set: any path named in /etc/systemd, /data/scratch/cfg or deploy/ops, in a
   running process's arguments, the current directory of ANY process (/proc/<pid>/cwd; the
   count of processes whose cwd could not be read is said), or one of the infrastructure
   worktrees (``INFRA``). Removed with ``git worktree remove`` (never forced).

Never touched: /tmp/claude-1000, the owner link file, anything under .claude/, backups (P10
moves them off the box), the Portal capture (source-mirror.sqlite: the delta's input)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

DATA = "/data"
REPO = Path("/data/OntologyProject")
WT = Path("/data/scratch/wt")
LOGS = Path("/data/scratch/runs/spark")
KEEP_RELEASES = 6       # rollback room: the plan's deploy rehearsal keeps six
LOG_DAYS = 7
STALE_DAYS = 7
IDLE_HOURS = 24
INFRA = ("deploy", "landtool", "batch-land", "spark-launch-main")   # never removed
EXCLUDE_SOURCES = ("/etc/systemd", "/data/scratch/cfg", str(REPO / "deploy" / "ops"))


def free_gb(path: str | None = None) -> float:
    return shutil.disk_usage(path or DATA).free / 1024 ** 3


def _size(path: Path) -> int:
    got = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True)
    return int(got.stdout.split()[0]) if got.returncode == 0 and got.stdout else 0


CWD_UNREADABLE = 0


def process_cwds(proc: str = "/proc") -> tuple[set[str], int]:
    """(the worktree under /data/scratch/wt that each process's current directory is in,
    how many processes' cwd could not be read)."""
    found: set[str] = set()
    unreadable = 0
    for entry in os.listdir(proc):
        if not entry.isdigit():
            continue
        try:
            cwd = os.readlink(f"{proc}/{entry}/cwd")
        except FileNotFoundError:
            continue                      # the process ended
        except OSError:
            unreadable += 1               # another user's process: counted, said
            continue
        m = re.match(r"(/data/scratch/wt/[A-Za-z0-9._-]+)", cwd)
        if m:
            found.add(m.group(1))
    return found, unreadable


def excluded_paths() -> set[str]:
    """Worktree paths something still names: units, ops scripts, cfg, running processes."""
    out: set[str] = set()
    pat = re.compile(r"/data/scratch/wt/[A-Za-z0-9._-]+")
    for src in EXCLUDE_SOURCES:
        got = subprocess.run(["grep", "-rhoE", pat.pattern, src], capture_output=True, text=True)
        out.update(got.stdout.split())
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    out.update(pat.findall(ps))
    seen, unreadable = process_cwds()
    out.update(seen)
    global CWD_UNREADABLE
    CWD_UNREADABLE = unreadable
    out.update(str(WT / n) for n in INFRA)
    return out


def _git(wt: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(wt), *args], capture_output=True, text=True)


def worktree_candidates(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    keep = excluded_paths()
    out = []
    for wt in sorted(p for p in WT.iterdir() if p.is_dir()):
        if str(wt) in keep or wt.name in INFRA:
            continue
        if _git(wt, "status", "--porcelain").stdout.strip():
            continue                      # uncommitted work: never
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        if not head:
            continue
        branch = _git(wt, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        pushed = bool(branch) and _git(wt, "rev-parse", "-q", "--verify",
                                        f"refs/remotes/origin/{branch}").stdout.strip() == head
        in_main = _git(wt, "merge-base", "--is-ancestor", head, "origin/main").returncode == 0
        last = _git(wt, "log", "-1", "--format=%ct").stdout.strip()
        gitdir = _git(wt, "rev-parse", "--absolute-git-dir").stdout.strip()
        touched = [p.stat().st_mtime for p in wt.iterdir()]
        touched += [os.stat(os.path.join(gitdir, f)).st_mtime for f in ("HEAD", "logs/HEAD")
                    if gitdir and os.path.exists(os.path.join(gitdir, f))]
        newest = max(touched, default=0)
        merged = pushed and in_main and now - newest > IDLE_HOURS * 3600
        stale = last.isdigit() and now - int(last) > STALE_DAYS * 86400 \
            and now - newest > STALE_DAYS * 86400
        if merged or stale:
            out.append({"path": str(wt), "why": "merged" if merged else "untouched",
                        "bytes": _size(wt)})
    return out


def release_tool() -> str:
    """plx-release as the live release has it (``~/.local/bin/plx-release`` links the release's
    deploy/release/plx-release): on PATH, else the current release's file; never a fixed copy."""
    found = shutil.which("plx-release") or str(Path.home() / ".local/bin/plx-release")
    for cand in (found, "/data/plx-releases/current/deploy/release/plx-release"):
        if os.path.exists(cand):
            return cand
    raise RuntimeError("plx-release not found (PATH, ~/.local/bin, the current release)")


def log_candidates(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    if not LOGS.is_dir():
        return []
    return [{"path": str(p), "bytes": p.stat().st_size} for p in LOGS.iterdir()
            if p.is_file() and now - p.stat().st_mtime > LOG_DAYS * 86400]


def run(apply: bool, say: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    out: dict[str, Any] = {"apply": apply, "free_before_gb": round(free_gb(), 1)}
    steps: list[tuple[str, Callable[[], Iterable[dict[str, Any]]], Callable[[dict], None]]] = []

    def leftovers() -> list[dict[str, Any]]:
        import psycopg
        from operating.pg import config
        from operating.pg.business_views_access import sweep_leftovers
        if not apply:   # no database read in a dry run
            return [{"note": "biz leftovers are swept by the refresh itself; counted there"}]
        with psycopg.connect(config.dsn(), autocommit=True) as c:
            return [{"dropped": n} for n in sweep_leftovers(c)]

    def releases() -> list[dict[str, Any]]:
        exe = release_tool()
        cmd = [exe, "prune", "--keep", str(KEEP_RELEASES)]
        if not apply:
            return [{"would_run": " ".join(cmd)}]
        got = subprocess.run(cmd, capture_output=True, text=True)
        if got.returncode:
            raise RuntimeError(f"plx-release prune: {got.stderr.strip()}")
        return [{"ran": " ".join(cmd), "said": got.stdout.strip()}]

    for name, find in (("biz_leftovers", leftovers), ("releases", releases),
                       ("spark_logs", log_candidates), ("worktrees", worktree_candidates)):
        before = free_gb()
        try:
            got = list(find())
            if apply and name == "spark_logs":
                for c in got:
                    os.unlink(c["path"])
            if apply and name == "worktrees":
                for c in got:
                    r = subprocess.run(["git", "-C", str(REPO), "worktree", "remove", c["path"]],
                                       capture_output=True, text=True)
                    c["removed"] = r.returncode == 0
                    if r.returncode:
                        c["refused"] = r.stderr.strip()
        except Exception as exc:  # noqa: BLE001 - the step stops the run, said
            line = {"step": name, "failed": f"{type(exc).__name__}: {exc}"}
            say(line)
            out.setdefault("steps", []).append(line)
            break
        line = {"step": name, "items": len(got),
                "bytes": sum(c.get("bytes", 0) for c in got),
                "free_before_gb": round(before, 1), "free_after_gb": round(free_gb(), 1),
                "items_list": got}
        if name == "worktrees":   # processes whose cwd could not be read (other users): said
            line["cwd_unreadable"] = CWD_UNREADABLE
        say({k: v for k, v in line.items() if k != "items_list"})
        out.setdefault("steps", []).append(line)
    out["free_after_gb"] = round(free_gb(), 1)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="retention")
    ap.add_argument("--apply", action="store_true", help="remove (default: say only)")
    args = ap.parse_args(argv)
    got = run(args.apply, lambda line: print(json.dumps(line), flush=True))
    print(json.dumps({"apply": got["apply"], "free_before_gb": got["free_before_gb"],
                      "free_after_gb": got["free_after_gb"],
                      "would_free_gb" if not got["apply"] else "freed_gb": round(sum(
                          s.get("bytes", 0) for s in got.get("steps", [])) / 1024 ** 3, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
