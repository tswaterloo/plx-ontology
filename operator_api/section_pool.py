"""A record page's sections read at once, each slot its own process.

A page's sections (``backend.object_section``) are CPU-bound Python; threads under one
interpreter lock read them one after another, so each slot of this pool is a process: the
console's builder run again there (``case_console.main``: its own case runtime, stores
and read-only SQLite connections, every check again), reading one section at a time for
the reader the parent names, exactly as the read worker does (``object_section`` over the
reader's own workspace). A section kind always goes to the same slot (its kept quick
read and sections are found again there); a slot serves readers fairly: a reader's k-th
section queued or running there waits behind every reader's earlier ones. Until a slot is
up, or once its process is gone, its sections are read on the read worker, as before.

Nothing is written; no slot shares a connection; a result is the section model the
parent labels and journals as before. Evidence: isolated-fixture (the console's fixture
world) and the measured run on a scratch read-only instance.
"""

from __future__ import annotations

from concurrent.futures import Future
import itertools
import logging
import multiprocessing
import queue
import threading
from typing import Any, Callable, Mapping, Optional, Sequence

LOG = logging.getLogger("operator_api.sections")
START_SECONDS = 600     # how long a slot's runtime may take to come up


class SlotGone(Exception):
    """The slot's process is not serving: the caller reads the section itself."""


class _Slot:
    """One child process and the parent thread feeding it, lowest priority first."""

    def __init__(self, number: int, start: Callable[[Any], Any]) -> None:
        self.number = number
        self.jobs: "queue.PriorityQueue[tuple[int, int, Any]]" = queue.PriorityQueue()
        self.ready = threading.Event()
        self.gone = threading.Event()
        self.running = 0
        self._start = start
        self.process: Any = None
        self.conn: Any = None

    def run(self) -> None:
        try:
            self.conn, self.process = self._start(self.number)
            if not self.conn.poll(START_SECONDS) or self.conn.recv() != ("ready",):
                raise SlotGone("not_ready")
            self.ready.set()
            while True:
                _, _, item = self.jobs.get()
                if item is None:
                    return
                future, message = item
                if not future.set_running_or_notify_cancel():
                    continue
                self.running += 1
                try:
                    self.conn.send(message)
                    status, value = self.conn.recv()
                except (EOFError, OSError) as error:
                    future.set_exception(SlotGone(type(error).__name__))
                    raise SlotGone("process_ended") from None
                finally:
                    self.running -= 1
                if status == "ok":
                    future.set_result(value)
                else:
                    future.set_exception(value)
        except BaseException as error:  # noqa: BLE001 - logged; its sections go home
            LOG.error("section slot %s stopped: %s", self.number, type(error).__name__)
        finally:
            self.gone.set()
            self._drain()
            self.stop_process()

    def _drain(self) -> None:
        while True:
            try:
                _, _, item = self.jobs.get_nowait()
            except queue.Empty:
                return
            if item is not None and item[0].set_running_or_notify_cancel():
                item[0].set_exception(SlotGone("slot_stopped"))

    def stop_process(self) -> None:
        try:
            if self.conn is not None:
                self.conn.send(None)
        except (OSError, ValueError):
            pass
        if self.process is not None:
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.terminate()


class SectionPool:
    """``size`` slots (``start(number)`` -> (connection, process) of one slot's child),
    each fed by its own thread."""

    def __init__(self, size: int, start: Callable[[int], Any]) -> None:
        self.slots = [_Slot(n, start) for n in range(max(0, size))]
        self._load: dict[tuple[int, str], int] = {}
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        for slot in self.slots:
            threading.Thread(target=slot.run, name=f"app-section-{slot.number}",
                             daemon=True).start()

    def slot_for(self, section: str, sections: Sequence[str]) -> Optional[_Slot]:
        """The slot ``section`` always goes to, when it serves now."""
        if not self.slots:
            return None
        index = list(sections).index(section) if section in sections else 0
        slot = self.slots[index % len(self.slots)]
        return slot if slot.ready.is_set() and not slot.gone.is_set() else None

    def submit(self, slot: _Slot, reader_key: str, message: Any) -> Future:
        """``message`` to ``slot``: queued behind every reader's earlier sections there."""
        key = (slot.number, reader_key)
        with self._lock:
            ahead = self._load.get(key, 0)
            self._load[key] = ahead + 1
            seq = next(self._seq)

        def done(_: Future) -> None:
            with self._lock:
                left = self._load.get(key, 1) - 1
                if left > 0:
                    self._load[key] = left
                else:
                    self._load.pop(key, None)
        future: Future = Future()
        future.add_done_callback(done)
        if slot.gone.is_set():
            future.set_exception(SlotGone("slot_stopped"))
            return future
        slot.jobs.put((ahead, seq, (future, message)))
        if slot.gone.is_set():
            slot._drain()   # stopped meanwhile: never left waiting
        return future

    def busy(self) -> bool:
        return any(s.running > 0 or not s.jobs.empty() for s in self.slots)

    def stop(self) -> None:
        for slot in self.slots:
            slot.jobs.put((-1, 0, None))


def spawn(argv: Sequence[str], settings: Mapping[str, Any]) -> Callable[[int], Any]:
    """``SectionPool``'s ``start``: a fresh interpreter (``spawn``: no thread or lock of
    the parent is inherited) running ``child`` over the console arguments ``argv``. The
    parent's main module must guard its start (``if __name__ == "__main__"``), as
    ``python3 -m operator_api`` does."""
    context = multiprocessing.get_context("spawn")

    def start(number: int) -> Any:
        parent, child_end = context.Pipe()
        process = context.Process(target=child, args=(child_end, list(argv), dict(settings)),
                                  name=f"plx-app-section-{number}", daemon=True)
        process.start()
        child_end.close()
        return parent, process
    return start


def child(conn: Any, argv: list[str], settings: dict[str, Any],
          fixtures: Any = None) -> int:
    """One slot: build the console's runtime here (``fixtures``: the fixture world's
    module, for a slot run on a thread in tests; the pool's processes have none), then
    answer ``(reader, object_type, object_id, section)`` with
    ``("ok", section)`` or ``("error", exception)`` until the parent sends None or goes
    away."""
    from operator_ui import case_console
    from . import backend as be

    def serve(ctx: Mapping[str, Any]) -> int:
        backend = be.WorkspaceBackend(ctx["objects_for"], **settings)
        worker = backend.reads
        backend.warm_store()   # the generation's reader-independent facts, once here
        conn.send(("ready",))
        try:
            while True:
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    return 0
                if message is None:
                    return 0
                reader, object_type, object_id, section = message
                try:
                    view = worker._workspace(reader.session)
                    out: tuple[str, Any] = ("ok", be.object_section(
                        backend, view, object_type, object_id, section))
                except Exception as error:  # noqa: BLE001 - handed to the parent's route
                    out = ("error", error)
                try:
                    conn.send(out)
                except Exception:  # noqa: BLE001 - an error that does not pickle
                    conn.send(("error", RuntimeError(type(out[1]).__name__)))
        finally:
            while worker.cache:
                worker.cache.pop(next(iter(worker.cache))).close()

    return case_console.main(argv, serve_with=serve, fixtures=fixtures)
