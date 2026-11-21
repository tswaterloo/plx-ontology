"""The rest of a long scan, read by OCR in the background into the person's kept file.

An Ask request OCRs at most ``doc_read.REQUEST_OCR_PAGES`` scanned pages (the answer must
come in seconds). When a PDF the person kept (``instruct_files.UploadDesk``: their own
file, ``file:<id>``) has more, ``start`` reads the rest in a child process and writes the
pages beside the file (``<file_id>.ocr.json`` in the store's ``files`` folder); the next
``plx.read_document`` of that file gets them (``known``) and reads nothing twice.

Bounds: one reading per file at a time across every API process (a lock file per file,
held by the child for its life: a second ``start`` of the same file does nothing), at
most ``MAX_CHILDREN`` children on the host (slot lock files; over that, ``start`` says
"later" and the pages stay unread), ``SECONDS`` for the whole reading (then it is killed,
and the file is not tried again for ``RETRY_SECONDS``), and the child is killed when the
kept file disappears (deleted outside the app: the store has no delete of its own; its
sidecar goes with it). A store without a folder (memory only) keeps nothing
to read into: no background reading. The child reads one local file and writes one file;
it holds no credential and makes no network call.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

SECONDS = 900.0           # the whole background reading of one file
RETRY_SECONDS = 3600.0    # a file whose reading timed out or failed is not tried at once again
POLL = 1.0
MAX_CHILDREN = 2          # background OCR children at once on this host (all API processes)
MAX_SIDECAR = 8 * 1024 * 1024
_ID = re.compile(r"f_[0-9a-f]{24}")
_LOCK = threading.Lock()
_RUNNING: dict[str, subprocess.Popen] = {}
_TRIED: dict[str, float] = {}


def _paths(desk: Any, file_id: str) -> Optional[tuple[Path, Path]]:
    directory = getattr(desk, "dir", None)
    if directory is None or not isinstance(file_id, str) or not _ID.fullmatch(file_id):
        return None
    folder = Path(directory) / "files"
    return folder / file_id, folder / f"{file_id}.ocr.json"


def _lock(directory: Path, name: str) -> Optional[int]:
    """An exclusive non-blocking lock on ``directory/name`` (the fd; held while an open
    descriptor of it lives, so a child that inherits it keeps it); None when taken."""
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / name, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def known(desk: Any, file_id: str) -> Optional[dict[int, str]]:
    """The pages the background reading of this kept file wrote (text by page number);
    None when it has not finished (or none was started): then nothing is assumed."""
    paths = _paths(desk, file_id)
    if paths is None or not paths[0].is_file():
        return None
    try:
        if paths[1].stat().st_size > MAX_SIDECAR:
            return None
        with open(paths[1], "rb") as handle:
            raw = json.loads(handle.read(MAX_SIDECAR))
        data = paths[0].read_bytes()
        if raw.get("digest") != hashlib.sha256(data).hexdigest():
            return None
        return {int(n): str(t) for n, t in dict(raw["pages"]).items()}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def running(file_id: str) -> bool:
    with _LOCK:
        proc = _RUNNING.get(file_id)
        return proc is not None and proc.poll() is None


def start(desk: Any, file_id: str, *, seconds: float = SECONDS) -> str:
    """Read the rest of the kept PDF ``file_id`` in the background: "started", "running"
    (one per file), "done" (its pages are there), "later" (tried lately and failed, or the host
    already runs ``MAX_CHILDREN``) or "none" (no folder to keep them in, or no such file)."""
    paths = _paths(desk, file_id)
    if paths is None or not paths[0].is_file():
        return "none"
    source, out = paths
    with _LOCK:
        proc = _RUNNING.get(file_id)
        if proc is not None and proc.poll() is None:
            return "running"
        if out.is_file() and known(desk, file_id) is not None:
            return "done"
        if time.monotonic() - _TRIED.get(file_id, -RETRY_SECONDS) < RETRY_SECONDS:
            return "later"
        locks = source.parent.parent / "ocr-locks"
        file_fd = _lock(locks, file_id + ".lock")
        if file_fd is None:
            return "running"           # another API process is reading this file
        slot_fd = next((fd for fd in (_lock(locks, f"slot{i}.lock")
                                      for i in range(MAX_CHILDREN)) if fd is not None), None)
        if slot_fd is None:
            os.close(file_fd)
            return "later"             # the host's background OCR is busy: not read now
        root = Path(__file__).resolve().parents[1]
        env = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": str(root),
               "OMP_THREAD_LIMIT": "2", "LANG": "C.UTF-8"}
        try:
            proc = subprocess.Popen(_command(source, out), cwd=str(root), env=env,
                                    start_new_session=True,
                                    pass_fds=(file_fd, slot_fd),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            _TRIED[file_id] = time.monotonic()
            return "none"
        finally:
            for fd in (file_fd, slot_fd):
                os.close(fd)           # the child's copies keep the locks while it lives
        _RUNNING[file_id] = proc
    threading.Thread(target=_watch, args=(file_id, proc, source, out, seconds),
                     name="ocr-background", daemon=True).start()
    return "started"


def _command(source: Path, out: Path) -> list[str]:
    cmd = [sys.executable, "-m", "operator_api.ocr_background", str(source), str(out)]
    return ["nice", "-n", "10"] + cmd if shutil.which("nice") else cmd


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _watch(file_id: str, proc: subprocess.Popen, source: Path, out: Path,
           seconds: float) -> None:
    """Until the child ends: kill it when the kept file is deleted or ``seconds`` pass."""
    deadline = time.monotonic() + seconds
    failed = False
    try:
        while proc.poll() is None:
            if not source.exists():
                _kill(proc)
                break
            if time.monotonic() > deadline:
                _kill(proc)
                failed = True
                break
            time.sleep(POLL)
        else:
            failed = proc.returncode != 0
        if not source.exists():
            for leftover in (out, Path(f"{out}.{proc.pid}.tmp")):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        elif failed:
            try:
                Path(f"{out}.{proc.pid}.tmp").unlink()
            except OSError:
                pass
    finally:
        with _LOCK:
            _RUNNING.pop(file_id, None)
            if failed:
                _TRIED[file_id] = time.monotonic()


def _child(source: str, out: str) -> int:
    from operating import doc_read
    data = Path(source).read_bytes()
    pages = doc_read.ocr_rest(data)
    tmp = f"{out}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"digest": hashlib.sha256(data).hexdigest(),
                   "pages": {str(n): t for n, t in sorted(pages.items())}}, handle)
    os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(_child(sys.argv[1], sys.argv[2]))
