"""Temporary directories under ``/data/plx-tmp/<unit>/<pid>`` and their sweeper.

    python3 -m operating.tmp sweep [--dry-run] [--system-tmp-days 3]

Every product temp directory is made under its process's own directory
(``ontology.tmp_root``: ``mkdtemp``, ``temporary_directory``, ``process_dir``; a bare
``tempfile.mkdtemp`` outside tests fails ``tests/test_tmp_lint.py``), and a workspace job
started by ``tools/job.py`` gets ``TMPDIR`` there too (``job_env``), so a third-party
library's temp files land in the same place (perm-box-1114, Fable audit 11-14 item 12).

``sweep`` removes each ``<root>/<unit>/<pid>`` whose process is gone: no such pid, or a pid
whose start time differs from the directory's ``.start`` record (the pid was reused). A
directory without its record is kept until it is ``GRACE_S`` old (it may be being made).
An empty unit directory is removed. With ``system_tmp_days`` it also removes, from the
system temp dir, this user's ``mkdtemp``-named directories (``tmp`` or a ``plx-``/``plx_``
prefix, then 8 characters of ``[a-z0-9_]``) whose newest file is older than that many days and that no live
process holds open or runs in: what killed runs left before this module existed. Never a
path outside those two roots, never another user's, never through a symlink. Prints JSON
counts and bytes, never a name or content.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Iterable, Mapping, Optional

from ontology.tmp_root import (START_FILE, mkdtemp, process_dir, process_start, root,
                               temporary_directory, unit)

__all__ = ["mkdtemp", "temporary_directory", "process_dir", "job_env", "sweep", "unit"]

GRACE_S = 600.0
# What ``tempfile.mkdtemp`` names: a prefix and 8 characters of [a-z0-9_]. Shared fixed
# names (``plx-test-slots``, ``plx-workbench-<uid>``) never match: a ``-`` in the last 8.
SYSTEM_TMP_NAMES = re.compile(r"^(tmp|plx[-_][A-Za-z0-9_.-]*)[a-z0-9_]{8}$")
_SCAN_CAP = 20000   # files looked at per system-temp entry before it counts as in use


def job_env(workspace: Optional[str], environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The environment a launched job's temp files follow: ``TMPDIR`` (and ``TMP``,
    ``TEMP``) at the launcher's own directory under the workspace's unit, which the sweeper
    removes once the launcher is gone."""
    env = {**environ}
    if workspace:
        env["PLX_TMP_UNIT"] = workspace
    where = str(process_dir(env))
    out = {"TMPDIR": where, "TMP": where, "TEMP": where}
    if workspace:
        out["PLX_TMP_UNIT"] = workspace
    return out


def _alive_as(pid: int, start: Optional[str]) -> bool:
    now = process_start(pid)
    if now is None:
        return False
    return not start or start.strip() == str(now)


def _size(path: Path) -> int:
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def _remove(path: Path, dry_run: bool) -> int:
    size = _size(path)
    if not dry_run:
        shutil.rmtree(path, ignore_errors=True)
    return size


def _sweep_root(base: Path, now: float, dry_run: bool, out: dict[str, Any]) -> None:
    if not base.is_dir() or base.is_symlink():
        return
    for unit_dir in sorted(base.iterdir()):
        if unit_dir.is_symlink() or not unit_dir.is_dir():
            continue
        for pid_dir in sorted(unit_dir.iterdir()):
            if pid_dir.is_symlink() or not pid_dir.is_dir() or not pid_dir.name.isdigit():
                continue
            out["dirs"] += 1
            try:
                start = (pid_dir / START_FILE).read_text()
            except OSError:
                start = None
                try:
                    if now - pid_dir.stat().st_mtime < GRACE_S:
                        out["kept"] += 1
                        continue
                except OSError:
                    continue
            if _alive_as(int(pid_dir.name), start):
                out["kept"] += 1
                continue
            out["removed"] += 1
            out["removed_bytes"] += _remove(pid_dir, dry_run)
        if not dry_run:
            try:
                unit_dir.rmdir()   # only when empty
                out["units_removed"] += 1
            except OSError:
                pass


def _open_paths(proc: Path = Path("/proc")) -> set[str]:
    """Every path a live process of this user has open or runs in (cwd)."""
    held: set[str] = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            held.add(os.readlink(entry / "cwd"))
            for fd in (entry / "fd").iterdir():
                try:
                    held.add(os.readlink(fd))
                except OSError:
                    pass
        except OSError:
            continue
    return held


def _newest(path: Path, cap: int = _SCAN_CAP) -> Optional[float]:
    """The newest mtime in `path` (None: more than `cap` files, treat as in use)."""
    newest, seen = os.lstat(path).st_mtime, 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            seen += 1
            if seen > cap:
                return None
            try:
                newest = max(newest, os.lstat(os.path.join(base, name)).st_mtime)
            except OSError:
                pass
    return newest


def _sweep_system_tmp(base: Path, days: float, now: float, dry_run: bool,
                      out: dict[str, Any], held: Iterable[str]) -> None:
    held = list(held)
    for entry in sorted(base.iterdir()):
        if not SYSTEM_TMP_NAMES.match(entry.name) or entry.name == "plx-tmp":
            continue
        try:
            st = os.lstat(entry)
        except OSError:
            continue
        if st.st_uid != os.getuid() or entry.is_symlink() or not entry.is_dir():
            continue
        out["system_seen"] += 1
        prefix = str(entry)
        if any(p == prefix or p.startswith(prefix + os.sep) for p in held):
            continue
        newest = _newest(entry)
        if newest is None or now - newest < days * 86400:
            continue
        out["system_removed"] += 1
        out["system_removed_bytes"] += _remove(entry, dry_run)


def sweep(environ: Mapping[str, str] = os.environ, *, dry_run: bool = False,
          system_tmp_days: Optional[float] = None, system_tmp: Optional[Path] = None,
          now: Optional[float] = None) -> dict[str, Any]:
    """Remove the temp directories of gone processes (module docstring); counts only."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"dirs": 0, "kept": 0, "removed": 0, "removed_bytes": 0,
                           "units_removed": 0, "system_seen": 0, "system_removed": 0,
                           "system_removed_bytes": 0, "dry_run": dry_run}
    bases = [root(environ), Path(tempfile.gettempdir()) / "plx-tmp"]
    for base in dict.fromkeys(bases):
        _sweep_root(base, now, dry_run, out)
    if system_tmp_days is not None and system_tmp_days > 0:
        base = system_tmp or Path(tempfile.gettempdir())
        _sweep_system_tmp(base, system_tmp_days, now, dry_run, out, _open_paths())
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.tmp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sw = sub.add_parser("sweep")
    sw.add_argument("--dry-run", action="store_true")
    sw.add_argument("--system-tmp-days", type=float)
    args = parser.parse_args(argv)
    print(json.dumps(sweep(dry_run=args.dry_run, system_tmp_days=args.system_tmp_days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
