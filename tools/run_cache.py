#!/usr/bin/env python3
"""Shared test-run results, cached affected selections and the Spark slot queue.

USAGE
    python3 tools/run_cache.py base-known --base origin/main MODULE...
    python3 tools/run_cache.py affected --base origin/main [--root DIR | --repo R --sha SHA]
    python3 tools/run_cache.py slots
    (spark_test.sh uses: passed, record, split)

**Results.** A test id (a dotted module, or ``module.Class`` when the lander splits a slow
module) has one result per git *tree*: the same tree is the same code, data and test lists
(``KNOWN-RED.md``, ``FLAKY.md``), so a module that passed on a tree passes there again, and
any changed file anywhere is a new tree and a miss (no dependency guesswork, no false
pass). ``deploy/spark/spark_test.sh`` records every result line of a complete
``tools/main_ci.py`` run (``record``) and, before the next run of that tree, ships the ids
that passed there (``passed``). Only passes are reused, and only when every
result of that id within ``TTL_HOURS`` passed on the same runner kind (``--env``: the
Sparks are one kind; a module that skips on one host proves nothing for another); a FAIL,
a flaky mix or an older pass runs again. Stored as one JSON line per result in
``<cache>/results/<tree[:2]>/<tree>.jsonl``, appended under ``flock`` (concurrent runs
never lose or interleave a line; a torn line is ignored).

**base-known** answers "is this red on main too" from the cache: per module, the latest
result of it (or of its classes) on the base's tree, with its age, without running anything.

**affected** prints ``tools/affected_tests.py --base`` once per (tree, base): concurrent
callers of the same key wait on one computation and read its output (stdout and the
selector's notes) instead of each running the selector. ``--sha`` selects for that commit in
a temporary detached checkout; a ``--root`` with uncommitted or untracked changes is
selected fresh and never cached.

**slots** prints the Spark slot queue: each slot held or free (from ``/proc/locks``, so runs
of any script version count), what each registered run is doing and for how long, and who
waits, so a job can look instead of waiting blind.

The cache directory is ``PLX_RUN_CACHE`` (default ``/data/scratch/runs/cache``).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Iterable, Iterator

CACHE = Path(os.environ.get("PLX_RUN_CACHE", "/data/scratch/runs/cache"))
TTL_HOURS = float(os.environ.get("PLX_RUN_CACHE_TTL_HOURS", "24"))
BASE_KNOWN_DAYS = 7.0
SLOT_DIR = Path(os.environ.get("SPARK_SLOT_DIR", "/data/scratch"))
SLOTS = int(os.environ.get("SPARK_SLOTS", "4"))
# tools/main_ci.py's result states; every one but FAIL and known red is a pass there
STATES = {"pass": True, "fixed (delete its KNOWN-RED row)": True, "flaky, passed alone": True,
          "timed out under load, passed on rerun": True, "FAIL": False, "known red": False}
RESULT = re.compile(r"^(" + "|".join(re.escape(s) for s in STATES) + r") +([0-9.]+)s ([A-Za-z_][A-Za-z0-9_.]*)$")
SUMMARY = re.compile(r"^(\d+) modules: \d+ pass, \d+ known red, \d+ FAIL")
TREE = re.compile(r"^[0-9a-f]{40}$")


def module_of(test_id: str) -> str:
    """``pkg.tests.test_x.SomeTests`` -> ``pkg.tests.test_x``; a module id is its own module."""
    head, _, last = test_id.rpartition(".")
    return head if head and last[:1].isupper() else test_id


def _results_file(cache: Path, tree: str) -> Path:
    if not TREE.match(tree):
        raise ValueError(f"not a tree hash: {tree!r}")
    return cache / "results" / tree[:2] / f"{tree}.jsonl"


def append(cache: Path, tree: str, records: list[dict]) -> int:
    """Appends records under an exclusive lock, one write: concurrent writers never interleave."""
    if not records:
        return 0
    path = _results_file(cache, tree)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records).encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, blob)
    finally:
        os.close(fd)        # closing releases the lock
    return len(records)


def read(cache: Path, tree: str) -> list[dict]:
    try:
        lines = _results_file(cache, tree).read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue        # a torn line (a writer killed mid-write) is no result
        if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("ok"), bool):
            out.append(r)
    return out


def parse_main_ci(text: str) -> list[tuple[str, bool, float]] | None:
    """(id, passed, seconds) of a complete main_ci report, or None when the report is not one:
    no summary line, or a count that does not match its result lines. Result lines are read
    only before the failure tails (``::error::``), whose output a test controls."""
    results: list[tuple[str, bool, float]] = []
    total, tails = None, False
    for line in text.splitlines():
        if line.startswith("::error::"):
            tails = True
        summary = SUMMARY.match(line)
        if summary:
            total = int(summary.group(1))
            continue
        hit = None if tails else RESULT.match(line)
        if hit:
            results.append((hit.group(3), STATES[hit.group(1)], float(hit.group(2))))
    if total is None or total != len(results) or len({r[0] for r in results}) != total:
        return None
    return results


def record(cache: Path, tree: str, env: str, logs: Iterable[Path], source: str = "") -> int:
    """Stores the results of each complete main_ci report among ``logs`` (one per host)."""
    now = time.time()
    n = 0
    for log in logs:
        try:
            parsed = parse_main_ci(Path(log).read_text(errors="replace"))
        except OSError:
            parsed = None
        if not parsed:
            continue
        n += append(cache, tree, [{"id": i, "ok": ok, "s": s, "env": env, "at": round(now),
                                   "src": source or str(log)} for i, ok, s in parsed])
    return n


def passed(cache: Path, tree: str, env: str, ttl_hours: float = TTL_HOURS,
           now: float | None = None) -> set[str]:
    """Ids whose every result on this tree and runner kind within the TTL passed."""
    cutoff = (time.time() if now is None else now) - ttl_hours * 3600
    good: set[str] = set()
    bad: set[str] = set()
    for r in read(cache, tree):
        if r.get("env") != env or float(r.get("at", 0)) < cutoff:
            continue
        (good if r["ok"] else bad).add(r["id"])
    return good - bad


def covered(test_id: str, done: set[str]) -> bool:
    """A passed module covers its classes; a passed class never covers its module."""
    return test_id in done or module_of(test_id) in done


def split(ids: list[str], done: set[str], shard: tuple[int, int] | None,
          timings: dict) -> tuple[list[str], list[str]]:
    """(this host's ids to run, cached ids this host reports). Cached ids weigh nothing in
    the greedy split by recorded seconds, and only shard 0 reports them (counted once)."""
    cached = [i for i in ids if covered(i, done)]
    rest = [i for i in ids if not covered(i, done)]
    if shard is None:
        return rest, cached
    index, count = shard
    # an id's own recorded seconds, else 5 like any unknown id: a class never takes its
    # whole module's (11-18: a 0.4 s class weighed its module's 900 s and
    # held a host to itself while the other host ran the 37 other ids in 439 s)
    weight = lambda m: float(timings.get(m) or 5)  # noqa: E731
    bins: list[list[str]] = [[] for _ in range(count)]
    load = [0.0] * count
    for m in sorted(rest, key=weight, reverse=True):
        k = load.index(min(load))
        bins[k].append(m)
        load[k] += weight(m)
    return bins[index], cached if index == 0 else []


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                          check=True, timeout=120).stdout.strip()


def tree_of(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", "-q", f"{rev}^{{tree}}")


def base_known(cache: Path, tree: str, modules: list[str], env: str | None = None,
               max_days: float = BASE_KNOWN_DAYS, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    rows = [r for r in read(cache, tree) if float(r.get("at", 0)) >= now - max_days * 86400
            and (env is None or r.get("env") == env)]
    lines = []
    for module in modules:
        latest: dict[str, dict] = {}
        for r in rows:
            if r["id"] == module or module_of(r["id"]) == module:
                if r["id"] not in latest or r["at"] >= latest[r["id"]]["at"]:
                    latest[r["id"]] = r
        if not latest:
            lines.append(f"{module}: not run on base (no result on its tree in {max_days:g} days)")
            continue
        red = sorted(i for i, r in latest.items() if not r["ok"])
        newest = max(latest.values(), key=lambda r: r["at"])
        age = f"{(now - newest['at']) / 3600:.1f} h ago on {newest.get('env')}"
        if red:
            lines.append(f"{module}: red on base ({', '.join(red)}; {age}; {newest.get('src', '')})")
        elif module in latest:
            lines.append(f"{module}: green on base ({age})")
        else:
            lines.append(f"{module}: green on base for {len(latest)} classes run separately ({age})")
    return lines


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def affected(cache: Path, base: str, *, root: Path | None = None, repo: Path | None = None,
             sha: str | None = None, extra: list[str] | None = None) -> tuple[int, str, str, bool]:
    """(exit code, stdout, stderr, served from the cache) of the selector for one key."""
    extra = list(extra or [])
    if sha:
        repo = Path(repo or root or ".")
        commit = git(repo, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}")
        tree = tree_of(repo, commit)
    else:
        root = Path(root or ".")
        repo = root
        if git(root, "status", "--porcelain", "--untracked-files=normal"):
            # uncommitted or untracked changes: the tree is not a commit's, never cached
            run = subprocess.run([sys.executable, str(root / "tools" / "affected_tests.py"),
                                  "--root", str(root), "--base", base, *extra],
                                 text=True, capture_output=True)
            return run.returncode, run.stdout, run.stderr + "run_cache: uncommitted changes, selected fresh (not cached)\n", False
        tree = tree_of(root, "HEAD")
    base_commit = git(repo, "rev-parse", "--verify", "-q", f"{base}^{{commit}}")
    key = hashlib.sha256("\0".join([tree, base_commit, *extra]).encode()).hexdigest()[:40]
    where = cache / "affected"
    out_file, err_file = where / f"{key}.out", where / f"{key}.err"
    with _locked(where / f"{key}.lock"):
        if out_file.is_file():
            return 0, out_file.read_text(), err_file.read_text() if err_file.is_file() else "", True
        checkout = None
        try:
            if sha:
                checkout = Path(tempfile.mkdtemp(prefix="affected-", dir=where))
                subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach",
                                str(checkout / "t"), commit], check=True, capture_output=True)
                root = checkout / "t"
            run = subprocess.run([sys.executable, str(Path(root) / "tools" / "affected_tests.py"),
                                  "--root", str(root), "--base", base_commit, *extra],
                                 text=True, capture_output=True, cwd=str(root))
        finally:
            if checkout is not None:
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                                str(checkout / "t")], capture_output=True)
                shutil.rmtree(checkout, ignore_errors=True)
        if run.returncode == 0:
            _write_atomic(err_file, run.stderr)
            _write_atomic(out_file, run.stdout)   # last: its presence means complete
        return run.returncode, run.stdout, run.stderr, False


def _flock_inodes(locks: Path = Path("/proc/locks")) -> set[int]:
    """Inodes holding an flock (any process): ``1: FLOCK ADVISORY WRITE <pid> maj:min:inode 0 EOF``."""
    held = set()
    try:
        for line in locks.read_text().splitlines():
            parts = line.split()
            if len(parts) > 5 and parts[1] == "FLOCK" and parts[3] == "WRITE" and parts[2] != "->":
                held.add(int(parts[5].rsplit(":", 1)[1]))
    except (OSError, ValueError, IndexError):
        pass
    return held


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def slots(slot_dir: Path = SLOT_DIR, count: int = SLOTS, now: float | None = None,
          locks: Path = Path("/proc/locks")) -> list[str]:
    now = time.time() if now is None else now
    held = _flock_inodes(locks)
    runs = []
    for f in sorted((slot_dir / "spark-runs").glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if _alive(int(r.get("pid", 0))):
            runs.append(r)
        else:
            f.unlink(missing_ok=True)      # a run that died without its exit trap
    by_slot = {str(r.get("slot")): r for r in runs if r.get("state") == "running"}
    mins = lambda r: f"{(now - float(r.get('since', now))) / 60:.0f} min"  # noqa: E731
    lines = []
    for n in range(1, count + 1):
        lock = slot_dir / f"spark-slot.{n}.lock"
        busy = lock.exists() and lock.stat().st_ino in held
        r = by_slot.get(str(n))
        if busy and r:
            lines.append(f"slot {n}: running {r.get('sha', '')[:10]} ({r.get('ids', '?')} ids, "
                         f"{r.get('who', '')}) for {mins(r)}")
        else:
            lines.append(f"slot {n}: " + ("held (a run without a status record)" if busy else "free"))
    for r in sorted((r for r in runs if r.get("state") == "running" and not r.get("slot")),
                    key=lambda r: r.get("since", 0)):
        lines.append(f"lander: running {r.get('sha', '')[:10]} ({r.get('ids', '?')} ids) for {mins(r)}")
    waiting = sorted((r for r in runs if r.get("state") == "waiting"), key=lambda r: r.get("since", 0))
    for k, r in enumerate(waiting, 1):
        lines.append(f"waiting {k}: {r.get('sha', '')[:10]} ({r.get('who', '')}) for {mins(r)}")
    free = sum(1 for line in lines[:count] if line.endswith("free"))
    lines.append(f"{free} of {count} slots free, {len(waiting)} waiting")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, default=CACHE)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record", help="store the results of complete main_ci reports")
    p.add_argument("--tree", required=True)
    p.add_argument("--env", required=True)
    p.add_argument("--source", default="")
    p.add_argument("logs", nargs="+", type=Path)
    p = sub.add_parser("passed", help="ids that passed on this tree (one per line)")
    p.add_argument("--tree", required=True)
    p.add_argument("--env", required=True)
    p = sub.add_parser("split", help="drop cached ids from a list file and take this host's share")
    p.add_argument("--mods", required=True, type=Path)
    p.add_argument("--skip", type=Path)
    p.add_argument("--shard")
    p.add_argument("--timings", type=Path)
    p.add_argument("--cached-out", required=True, type=Path)
    p = sub.add_parser("base-known", help="is this red on the base too, from the cache")
    p.add_argument("--base", required=True)
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--env")
    p.add_argument("modules", nargs="+")
    p = sub.add_parser("affected", help="tools/affected_tests.py --base, once per (tree, base)")
    p.add_argument("--base", required=True)
    p.add_argument("--root", type=Path)
    p.add_argument("--repo", type=Path)
    p.add_argument("--sha")
    p.add_argument("extra", nargs="*", help="further affected_tests.py flags, after --")
    sub.add_parser("slots", help="the Spark slot queue")
    args = parser.parse_args(argv)

    if args.cmd == "record":
        print(f"run_cache: recorded {record(args.cache, args.tree, args.env, args.logs, args.source)} results",
              file=sys.stderr)
    elif args.cmd == "passed":
        print("\n".join(sorted(passed(args.cache, args.tree, args.env))))
    elif args.cmd == "split":
        ids = [line.strip() for line in args.mods.read_text().splitlines() if line.strip()]
        done = set(args.skip.read_text().split()) if args.skip and args.skip.is_file() else set()
        shard = tuple(map(int, args.shard.split("/"))) if args.shard else None
        try:
            timings = json.loads(args.timings.read_text()) if args.timings else {}
        except (OSError, ValueError):
            timings = {}
        run, cached = split(ids, done, shard, timings)  # type: ignore[arg-type]
        args.mods.write_text("".join(i + "\n" for i in run))
        args.cached_out.write_text("".join(i + "\n" for i in cached))
    elif args.cmd == "base-known":
        print("\n".join(base_known(args.cache, tree_of(args.repo, args.base), args.modules, args.env)))
    elif args.cmd == "affected":
        code, out, err, hit = affected(args.cache, args.base, root=args.root, repo=args.repo,
                                       sha=args.sha, extra=args.extra)
        sys.stderr.write(err + ("run_cache: affected selection from the cache\n" if hit else ""))
        sys.stdout.write(out)
        return code
    elif args.cmd == "slots":
        print("\n".join(slots()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
