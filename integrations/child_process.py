"""The one rule that ends a child process the workbench starts, and its private folders.

:func:`stop` ends a child on every path (it finished, timed out, the caller raised, the
work was abandoned, the interpreter exits): the child's whole process group is killed
when the child leads one (``operating.host`` starts its ``run`` child so, and that kills
the OCR children the run started too), else the child alone, and the child is reaped.
:func:`run` (``subprocess.run`` with captured output) and :func:`private_directory` (a
``0700`` temporary folder, as ``tempfile.TemporaryDirectory``) record what each thread
has running, so :func:`abandon` can end a thread whose result is no longer wanted (an
OCR read past its caller's time budget; ``scripts.eval_oracle_dev._within``): its
children are stopped, its folders (the page images of a customer document) removed, and
it starts no new ones. At a normal interpreter exit every thread is abandoned (atexit), so
a daemon thread still reading leaves nothing behind. ``os._exit`` and a SIGKILL run no
atexit handler: a caller that ends with ``os._exit`` calls :func:`abandon` first (the
``scripts.eval_dev_baseline`` watchdog), and whoever starts a process it may SIGKILL kills
its process group.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from typing import Iterator, Mapping, Sequence
import weakref


class Abandoned(Exception):
    """The calling thread's work was abandoned: it starts no new child or folder."""


_lock = threading.Lock()
_children: dict[subprocess.Popen, threading.Thread] = {}
_folders: dict[str, threading.Thread] = {}
_abandoned: "weakref.WeakSet[threading.Thread]" = weakref.WeakSet()
_closed = False   # every thread abandoned (interpreter exit)


def stop(child: subprocess.Popen) -> None:
    """Kill ``child`` (its process group when it leads one) unless already reaped, and
    reap it."""
    if child.returncode is None:
        try:
            if os.getpgid(child.pid) == child.pid:
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
        except ProcessLookupError:
            pass   # reaped meanwhile (another thread's wait)
    child.wait()


def _owner() -> threading.Thread:
    owner = threading.current_thread()
    if _closed or owner in _abandoned:
        raise Abandoned("child_process_abandoned")
    return owner


def run(command: Sequence[str], *, timeout: float,
        env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    """``subprocess.run(command, capture_output=True, timeout=timeout, check=False,
    env=env)`` with stdin closed, the child stopped and reaped on every path."""
    with _lock:
        owner = _owner()
        child = subprocess.Popen(list(command), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=None if env is None else dict(env))
        _children[child] = owner
    with child:
        try:
            out, err = child.communicate(timeout=timeout)
        finally:
            stop(child)
            with _lock:
                _children.pop(child, None)
    return subprocess.CompletedProcess(child.args, child.returncode, out, err)


@contextmanager
def private_directory(prefix: str) -> Iterator[str]:
    """A ``0700`` temporary folder, removed on exit or when its thread is abandoned."""
    with _lock:
        owner = _owner()
        path = tempfile.mkdtemp(prefix=prefix)
        _folders[path] = owner
    try:
        yield path
    finally:
        with _lock:
            _folders.pop(path, None)
        shutil.rmtree(path, ignore_errors=True)


def abandon(owner: threading.Thread | None = None) -> None:
    """End ``owner``'s work (every thread's when None): its running children are
    stopped, its folders removed, and it starts no new child or folder."""
    global _closed
    with _lock:
        if owner is None:
            _closed = True
        else:
            _abandoned.add(owner)
        children = [c for c, o in _children.items() if owner is None or o is owner]
        folders = [f for f, o in _folders.items() if owner is None or o is owner]
    for child in children:
        stop(child)
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)


atexit.register(abandon)


__all__ = ["Abandoned", "abandon", "private_directory", "run", "stop"]
