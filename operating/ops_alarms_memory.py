"""What holds the host's memory (``memory_consumers``), read for ``ops_alarms``."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

TOP_CONSUMERS = 3
CGROUP_DEPTH = 8


def _stat_bytes(text: str, keys: Sequence[str]) -> int:
    total = 0
    for row in text.splitlines():
        parts = row.split()
        if len(parts) == 2 and parts[0] in keys and parts[1].isdigit():
            total += int(parts[1])
    return total


def memory_consumers(proc: Path = Path("/proc"), cgroups: Path = Path("/sys/fs/cgroup"),
                     top: int = TOP_CONSUMERS) -> dict[str, list[dict[str, Any]]]:
    """The leaf cgroups and the processes holding the most memory that reclaim cannot
    free: a cgroup's ``anon`` + ``shmem`` (page cache left out: it is given back), a
    process's ``RssAnon`` (its shared memory is its cgroup's). Names only (the cgroup's
    last part, a process's ``comm`` and ``-m`` module, never its arguments); what cannot
    be read is skipped."""
    groups: list[dict[str, Any]] = []
    root_depth = len(cgroups.parts)
    for here, dirs, files in os.walk(cgroups):
        depth = len(Path(here).parts) - root_depth
        if depth >= CGROUP_DEPTH:
            dirs[:] = []
        if depth == 0 or "memory.stat" not in files:
            continue
        if any((Path(here) / d / "memory.stat").exists() for d in dirs):
            continue   # not a leaf: its children are ranked instead
        try:
            used = _stat_bytes((Path(here) / "memory.stat").read_text(), ("anon", "shmem"))
        except OSError:
            continue
        groups.append({"name": Path(here).name, "bytes": used})
    procs: list[dict[str, Any]] = []
    try:
        pids = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        base = proc / pid
        try:
            status = (base / "status").read_text()
        except OSError:
            continue
        anon = 0
        for row in status.splitlines():
            if row.startswith("RssAnon:"):
                parts = row.split()
                anon = int(parts[1]) * 1024 if len(parts) > 1 and parts[1].isdigit() else 0
                break
        if not anon:
            continue
        try:
            name = (base / "comm").read_text().strip()
        except OSError:
            name = "?"
        try:
            argv = (base / "cmdline").read_bytes().split(b"\0")
            if b"-m" in argv[:-1]:
                module = argv[argv.index(b"-m") + 1].decode("ascii", "replace")
                if module.replace(".", "").replace("_", "").isalnum():
                    name = f"{name} -m {module}"
        except (OSError, ValueError):
            pass
        try:
            group = (base / "cgroup").read_text().strip().splitlines()[0].rsplit("/", 1)[-1]
        except (OSError, IndexError):
            group = ""
        procs.append({"pid": int(pid), "name": name, "cgroup": group,
                      "bytes": anon})
    groups.sort(key=lambda g: (-g["bytes"], g["name"]))   # rank before rounding
    procs.sort(key=lambda p: (-p["bytes"], p["pid"]))

    def gb(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict({k: v for k, v in r.items() if k != "bytes"},
                     gb=round(r["bytes"] / 1024 ** 3, 2)) for r in rows[:top]]
    return {"cgroups": gb(groups), "processes": gb(procs)}
