"""Where a process's temporary directories go: ``/data/plx-tmp/<unit>/<pid>``.

Every temporary directory of the product is made under its process's own directory
(perm-box-1114, Fable audit 11-14 item 12: ``/tmp`` held 17,804 entries, 11 GB, from runs
killed before their cleanup): ``<root>/<unit>/<pid>``, where ``<root>`` is ``PLX_TMP_ROOT``
(default ``/data/plx-tmp``, the data volume, not the 96 GB root disk), ``<unit>`` names
who made it (``PLX_TMP_UNIT``, else ``PLX_WORKSPACE`` or the older ``PLX_LANE``, else the
systemd unit the process runs in, else ``adhoc``) and ``<pid>`` is the process. The pid directory holds a ``.start``
file with the process's start time, so ``operating.tmp.sweep`` removes a directory whose
process is gone (or whose pid was reused) and never one still in use. This module is the
lowest layer's (stdlib only) so ontology and integrations use it as well; the sweeper and
the launcher's environment live in ``operating/tmp.py``. Where the root cannot be made (a
Mac, a read-only volume) the system temp dir's ``plx-tmp`` is used the same way.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Mapping, Optional

ROOT_ENV = "PLX_TMP_ROOT"
UNIT_ENV = "PLX_TMP_UNIT"
DEFAULT_ROOT = "/data/plx-tmp"
START_FILE = ".start"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.@-]+")


def process_start(pid: int) -> Optional[int]:
    """The process's start time in clock ticks since boot (``/proc/<pid>/stat`` field 22)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return None


def _systemd_unit() -> Optional[str]:
    try:
        line = Path("/proc/self/cgroup").read_text().strip().splitlines()[-1]
    except (OSError, IndexError):
        return None
    leaf = line.rsplit("/", 1)[-1]
    if leaf.endswith((".service", ".scope")):
        return leaf.rsplit(".", 1)[0]
    return None


def unit(environ: Mapping[str, str] = os.environ) -> str:
    """Who this process's temp directories belong to (module docstring)."""
    name = (environ.get(UNIT_ENV) or environ.get("PLX_WORKSPACE")
            or environ.get("PLX_LANE") or _systemd_unit() or "adhoc")
    return _UNSAFE.sub("-", name).strip("-.")[:80] or "adhoc"


def root(environ: Mapping[str, str] = os.environ) -> Path:
    return Path(environ.get(ROOT_ENV) or DEFAULT_ROOT)


def _usable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK | os.X_OK)


def process_dir(environ: Mapping[str, str] = os.environ, pid: Optional[int] = None) -> Path:
    """``<root>/<unit>/<pid>`` (0700), made on first use, with its ``.start`` record."""
    pid = os.getpid() if pid is None else pid
    base = root(environ)
    if not _usable(base):
        base = Path(tempfile.gettempdir()) / "plx-tmp"
    here = base / unit(environ) / str(pid)
    if not (here / START_FILE).is_file():
        here.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(here, 0o700)
        start = process_start(pid)
        tmp = here / f"{START_FILE}.{os.getpid()}"
        tmp.write_text("" if start is None else str(start))
        os.replace(tmp, here / START_FILE)
    return here


def mkdtemp(prefix: Optional[str] = None, suffix: Optional[str] = None,
            environ: Mapping[str, str] = os.environ) -> str:
    """``tempfile.mkdtemp`` under this process's directory: what every product temp
    directory uses instead of a bare ``mkdtemp`` (the lint in tests/test_tmp_lint.py)."""
    return tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=process_dir(environ))


def temporary_directory(prefix: Optional[str] = None, suffix: Optional[str] = None,
                        environ: Mapping[str, str] = os.environ) -> tempfile.TemporaryDirectory:
    """``tempfile.TemporaryDirectory`` under this process's directory."""
    return tempfile.TemporaryDirectory(prefix=prefix, suffix=suffix, dir=process_dir(environ))
