"""A job's command in its own transient systemd user scope inside the workspaces slice.

``tools/job.py`` and ``tools/testslot.py`` run an admitted job's command as ``systemd-run
--user --scope`` in ``plx-workspaces.slice`` (the lander's in ``plx-land.slice``), with the
cores and memory it declared as hard limits: ``CPUQuota`` = cores x 100 %, ``MemoryMax`` =
its memory (``MemoryHigh`` 90 % of it, so it is throttled before it is killed) and no swap, so a job that outgrows its declaration is
the one the kernel stops, inside its own scope, never Postgres or the API. ``systemd-run
--scope`` executes the command in place: its pid, process group and inherited file
descriptors (a test slot's lock) are the caller's child's, so the queue's signals, sweep and
preemption reach it unchanged. Without ``systemd-run`` or a user manager (a Mac, a
container, ``PLX_JOB_SCOPE=0``) the command runs as is: the queue's admission still holds.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Mapping, Sequence

WORKSPACES_SLICE = "plx-workspaces.slice"
LAND_SLICE = "plx-land.slice"
ENV = "PLX_JOB_SCOPE"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def available(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether a user scope can be made here (``PLX_JOB_SCOPE=0``: never)."""
    if environ.get(ENV, "") in ("0", "off", "no", "false"):
        return False
    if not shutil.which("systemd-run", path=environ.get("PATH")):
        return False
    runtime = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime, "systemd", "private").exists() or Path(runtime, "bus").exists()


def unit_name(workspace: str | None, label: str | None, pid: int | None = None) -> str:
    """``job-<workspace>-<label>-<pid>``, safe as a unit name."""
    stem = _UNSAFE.sub("-", "-".join(p for p in ("job", workspace, label) if p)).strip("-")[:180]
    return f"{stem}-{os.getpid() if pid is None else pid}"


def wrap(command: Sequence[str], *, unit: str, cores: float, mem_mb: int,
         slice_name: str = WORKSPACES_SLICE) -> list[str]:
    """The argv running `command` in its scope (module docstring)."""
    props = [f"CPUQuota={max(1, int(round(cores * 100)))}%"]
    if mem_mb > 0:
        props += [f"MemoryMax={int(mem_mb)}M", f"MemoryHigh={int(mem_mb * 0.9)}M",
                  "MemorySwapMax=0"]
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--slice={slice_name}", f"--unit={unit}"]
    for prop in props:
        argv += ["-p", prop]
    return argv + ["--", *command]


def thread_env(cores: float) -> dict[str, str]:
    """What a job's libraries read for their worker counts: its declared cores."""
    n = str(max(1, int(cores)))
    return {"PLX_JOB_CORES": n, "OMP_NUM_THREADS": n, "OPENBLAS_NUM_THREADS": n,
            "MKL_NUM_THREADS": n, "NUMEXPR_MAX_THREADS": n}
