"""A worker process that ends with the process that started it.

A process an API or a facts read starts (the warm-up, the section slots, the facts read in
a process of its own) outlived its parent when that parent was stopped without its exit
handlers (a SIGTERM, a SIGKILL, an OOM kill): reparented to the session's reaper it kept
reading, and a facts read went on scanning Postgres and spilling gigabytes to temp files
for half an hour after its API was gone (2026-11-12). ``die_with`` watches the parent's
pid from a daemon thread and ends this process at once when it changes (Linux and macOS
alike; the parent-death signal follows the parent's starting thread, not the process)."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

POLL_SECONDS = 1.0
EXIT_CODE = 143   # as if terminated


def gone(parent: int) -> bool:
    """Whether ``parent`` is no longer this process's parent (it ended: this process was
    handed to another)."""
    return os.getppid() != parent


def die_with(parent: Optional[int], *, cleanup: Optional[Callable[[], None]] = None,
             poll: float = POLL_SECONDS) -> bool:
    """End this process (``os._exit``: no exit handler, no join of any thread) as soon as
    ``parent`` is no longer its parent, after ``cleanup`` (its own half-written files).
    None: nothing to watch (a run on a thread in tests). Already gone: ends now. Returns
    True when a watch started."""
    if parent is None:
        return False

    def end() -> None:
        try:
            if cleanup is not None:
                cleanup()
        finally:
            os._exit(EXIT_CODE)

    if gone(parent):
        end()

    def watch() -> None:
        while True:
            time.sleep(poll)
            if gone(parent):
                end()

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()
    return True


def spawned_parent() -> Optional[int]:
    """The pid of the process that started this one through ``multiprocessing``, None in
    a process no ``multiprocessing`` parent started."""
    import multiprocessing
    parent = multiprocessing.parent_process()
    return parent.pid if parent is not None else None


def _watch_then(init: Optional[Callable[..., Any]], args: tuple) -> None:
    """A pool worker's initializer (``pool_init``): watch the parent, then run ``init``."""
    die_with(spawned_parent())
    if init is not None:
        init(*args)


def pool_init(init: Optional[Callable[..., Any]] = None, initargs: tuple = ()
              ) -> dict[str, Any]:
    """The ``initializer`` / ``initargs`` keywords for a worker pool
    (``concurrent.futures.ProcessPoolExecutor`` or ``multiprocessing.Pool``) whose workers
    end with the process that started them, then run ``init(*initargs)`` as before. A
    parent killed without its exit handlers (an OOM kill, a SIGKILL, a stopped scope) left
    its workers running, reparented, for hours (11-13: five scopes, ~1.7 GB, nine hours)."""
    return {"initializer": _watch_then, "initargs": (init, tuple(initargs))}
