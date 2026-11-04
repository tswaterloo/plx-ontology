"""The ``text_index`` finishing step with the host's cores: its documents read by workers.

Measured on the live gen 8 build: the ``text_index`` step ran about 1.5 h on one core
(gen 7: 3,255 s) while the host's other cores stood idle. Per captured record the build's
process read the group's documents (a mail body blob, each attachment's kept reading, the
members of a zip), took each document's memo key and text facts, cut and compressed its
fields' text into chunks, and wrote them with their words. Here ``PLX_BUILD_WORKERS``
processes read the groups and derive, document by document, what the step derives from a
document alone (its memo key, ``text_index.document_chunks``: its facts and the chunks
of each field); the build's process runs ``text_index.store_text_index`` itself,
unchanged, in the serial order, deciding, counting, keeping in its memo and writing
exactly what the serial step does (``ontology/tests/test_text_parallel.py`` compares the
two: every store table, the word index's answers, the memo, the kept readings and the
report, failures included). ``ontology.text_index`` is not changed: its bytes are its memo
code (``text_index._memo_code``), so this module calls its functions and is imported by
``operating.build_steps`` only.

How (:func:`store_text_index`):

* The step's ``read_ahead`` is :class:`TextReader`'s. A group the memo holds whole is
  not read, as before (the step reads it itself only when a field is written again). Any
  other group whose load is a capture source's ``GroupLoad`` (perhaps wrapped by a
  module-level function: ``operating.build_steps._load_clean``) is sent to a worker that
  opens the source again under the build's watermark and reads the kept readings through
  its own views (``ontology.finish_documents`` does the same for ``mentions``; the build's
  process records what they hit and kept). The worker returns, per document, its memo key,
  citation, facts and chunks, never the document: the step is handed a stand-in
  (:class:`_Read`) whose key ``text_index.document_key`` answers while the step runs.
* A group a worker could not read or derive (an exception, a dead worker, a field whose
  text is not a string, a load that is not a ``GroupLoad``) is left to the step, which
  reads it as the serial step does, its failure counted the same way. The first
  ``min_groups`` groups to read are read by the step too: below that the workers cost
  more than they save (an incremental build that changed a few records).
* The word index takes its rows with a larger pending-term buffer (:data:`HASH_BYTES`,
  FTS5 ``hashsize``, kept in the index's config): fewer, larger segments to merge. The
  words, rows and answers are the same; the index's own segment layout is not part of
  the content digest (``ontology.store._TABLE_ORDER``).

Stdlib only.
"""

from __future__ import annotations

from collections import deque
import concurrent.futures as futures
import multiprocessing
import pickle
import time
from typing import Any, Callable, Iterable, Iterator

from ontology import text_index as T
from ontology.finish_documents import absorb_kept, portable_load

MIN_GROUPS = 256         # groups to read before the workers start (fewer: they cost more)
TASK_GROUPS = 16         # groups a worker reads in one task
WINDOW_TASKS = 4         # tasks per worker in flight ahead of the step
# The word index's pending-term buffer while the step writes (FTS5 ``hashsize``; SQLite's
# default is 1 MiB): measured on 120 MB of chunks, 5.0 s at 1 MiB, 3.1 s at 32 MiB.
HASH_BYTES = 32 * 1024 * 1024
# Measurement of the last run in this process (tests and the build log read it).
LAST: dict[str, Any] = {}


def workers(requested: int | None = None) -> int:
    """The processes the step may use (``ontology.project_parallel.workers``); 1 when that
    count is refused."""
    from ontology.finish_parallel import workers as finish_workers

    return finish_workers(requested)


def store_text_index(builder: Any, projector: Any, *, documents: Iterable[Any] | None = None,
                     processes: int | None = None, min_groups: int = MIN_GROUPS
                     ) -> dict[str, Any]:
    """``ontology.text_index.store_text_index``, its groups read and derived by
    ``processes`` worker processes (``workers``) when there are at least ``min_groups`` to
    read (module docstring); the same rows, the same report."""
    started = time.monotonic()
    LAST.clear()
    _bulk_writes(builder.connection)
    count = workers(processes)
    if count < 2 or documents is not None:
        LAST.update(mode="serial", reason="no_workers" if count < 2 else "documents_given")
        report = T.store_text_index(builder, projector, documents=documents)
        LAST["seconds"] = round(time.monotonic() - started, 3)
        return report
    context = getattr(projector, "build_context", None)
    memo = context.document_memo() if context is not None else None
    reader = TextReader(count=count, keyed=memo is not None, min_groups=min_groups)
    try:
        with reader.patched():
            report = T.store_text_index(builder, projector)
    finally:
        at = time.monotonic()
        reader.close()
        LAST["seconds.close"] = round(time.monotonic() - at, 3)
        LAST.update(mode="parallel" if reader.stats["groups"] else "serial",
                    reason=reader.reason, workers=count, **reader.stats,
                    seconds=round(time.monotonic() - started, 3))
    return report


def _bulk_writes(conn: Any) -> None:
    """The word index's pending-term buffer for this step's writes (:data:`HASH_BYTES`)."""
    conn.execute("INSERT INTO chunk_text (chunk_text, rank) VALUES ('hashsize', ?)",
                 (HASH_BYTES,))


class _Read:
    """A document a worker read, as the step sees it: its citation and memo key (the step
    reads nothing else of a document whose facts and chunks it was handed)."""

    __slots__ = ("citation", "key")

    def __init__(self, citation: str, key: str | None) -> None:
        self.citation, self.key = citation, key


class _Ready:
    """What the worker derived from one document (``mentions.read_ahead``'s ``Derived``)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value


class TextReader:
    """The groups of a ``text_index`` step read and derived by ``count`` worker processes
    (module docstring). Use ``patched()`` around ``text_index.store_text_index``; ``close``
    always."""

    def __init__(self, *, count: int, keyed: bool, min_groups: int = MIN_GROUPS) -> None:
        self.count, self.keyed, self.min_groups = count, keyed, min_groups
        self.reason: str | None = None
        self.stats = {"tasks": 0, "groups": 0, "groups_held": 0, "groups_here": 0,
                      "failed_tasks": 0, "seconds_waited": 0.0}
        self._pool: Any = None
        self._broken = False
        self._kept: list[Any] = []
        self._specs: dict[int, Any] = {}
        self._wraps: dict[Any, bool] = {}

    # ---------------------------------------------------------------- set up

    def _started(self) -> bool:
        from ontology import project_parallel as par
        from ontology.store import main_not_imported

        if self._pool is None and not self._broken:
            try:
                self._kept = par._kept_stores()
                payload = pickle.dumps({"kept": self._kept, "keyed": self.keyed},
                                       protocol=pickle.HIGHEST_PROTOCOL)
                with main_not_imported():
                    self._pool = futures.ProcessPoolExecutor(
                        max_workers=self.count,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=_init, initargs=(payload,))
            except Exception as exc:  # noqa: BLE001 - every group read by the step
                self.reason = f"workers_failed:{type(exc).__name__}"
                self._broken = True
        return self._pool is not None and not self._broken

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    def patched(self) -> Any:
        """``text_index``'s read-ahead and memo key answered by this reader while the step
        runs."""
        import contextlib

        @contextlib.contextmanager
        def patch() -> Iterator[None]:
            real = {name: getattr(T, name) for name in ("read_ahead", "document_key")}

            def document_key(document: Any) -> str:
                if type(document) is _Read and document.key is not None:
                    return document.key
                return real["document_key"](document)
            T.read_ahead = self.read_ahead          # type: ignore[assignment]
            T.document_key = document_key           # type: ignore[assignment]
            try:
                yield
            finally:
                for name, function in real.items():
                    setattr(T, name, function)
        return patch()

    # ---------------------------------------------------------------- the groups

    def read_ahead(self, groups: Iterable[tuple[Any, Any]], _task: str,
                   holds: Callable[[str], bool] | None, *_: Any, **__: Any
                   ) -> Iterator[tuple[Any, Any, Any]]:
        """``mentions.read_ahead`` for the step: each group in its order, ``read`` (its
        stand-in documents, whether they are remembered, what was derived from each) for
        a group a worker read, else None (the step reads it, as serially)."""
        from ontology.store import main_not_imported

        entries: deque[list[Any]] = deque()   # [group, load, task, index, held]
        batch: list[list[Any]] = []
        items: list[tuple[Any, ...]] = []
        spec_of_batch: Any = None
        in_flight = 0
        to_read = 0
        limit = self.count * WINDOW_TASKS
        pending = object()

        def submit() -> None:
            nonlocal in_flight, spec_of_batch
            if not batch:
                return
            task: dict[str, Any] = {"future": None, "results": None}
            if self._started():
                try:
                    with main_not_imported():   # a worker starts on a submit
                        task["future"] = self._pool.submit(_task_groups, spec_of_batch,
                                                           list(items))
                    in_flight += 1
                    self.stats["tasks"] += 1
                except Exception:  # noqa: BLE001 - read by the step
                    self._broken = True
            for index, entry in enumerate(batch):
                entry[2], entry[3] = task, index
            batch.clear()
            items.clear()
            spec_of_batch = None

        def resolve(entry: list[Any]) -> tuple[Any, Any, Any]:
            nonlocal in_flight
            if entry[2] is pending:   # in the batch not sent yet
                submit()
            task = entry[2]
            if task is not None and task["future"] is not None:
                future, task["future"] = task["future"], None
                in_flight -= 1
                try:
                    at = time.monotonic()
                    results, taken = future.result()
                    self.stats["seconds_waited"] = round(
                        self.stats["seconds_waited"] + time.monotonic() - at, 3)
                    absorb_kept(self._kept, taken)
                    task["results"] = results
                except Exception as exc:  # noqa: BLE001 - its groups are read by the step
                    self._broken = self._broken or isinstance(exc, futures.BrokenExecutor)
                    self.stats["failed_tasks"] += 1
                    task["results"] = []
            results = task["results"] if task is not None else None
            found = results[entry[3]] if results and entry[3] < len(results) else None
            if found is None:
                if not entry[4]:   # read by the step (not one the memo holds)
                    self.stats["groups_here"] += 1
                return entry[0], entry[1], None
            self.stats["groups"] += 1
            remembered, derived = found
            return entry[0], entry[1], (
                [_Read(citation, key) for key, citation, _f, _p in derived], remembered,
                [_Ready((facts, pieces)) for _k, _c, facts, pieces in derived])

        for group, load in groups:
            entry = [group, load, None, 0, False]
            if holds is not None and group is not None and holds(group):
                self.stats["groups_held"] += 1   # never read, as serially
                entry[4] = True
            else:
                to_read += 1
                portable = (None if self._broken or to_read < self.min_groups
                            else portable_load(load, self._specs, self._wraps))
                if portable is not None:
                    spec, method, args, wrap = portable
                    if batch and spec is not spec_of_batch:
                        submit()
                    spec_of_batch = spec
                    entry[2] = pending
                    batch.append(entry)
                    items.append((group, method, args, wrap))
                    if len(batch) >= TASK_GROUPS:
                        submit()
            entries.append(entry)
            while entries and (in_flight >= limit or len(entries) > limit * TASK_GROUPS):
                yield resolve(entries.popleft())
        submit()
        while entries:
            yield resolve(entries.popleft())
        if self.reason is None and not self.stats["groups"]:
            self.reason = "few_groups" if to_read < self.min_groups else "not_portable"


# --------------------------------------------------------------------------- the workers

_W: dict[str, Any] = {}


class _ReadHere(Exception):
    """A group the build's process reads itself (a field whose text is not a string)."""


def _init(payload: bytes) -> None:
    from ontology import project_parallel as par

    config = pickle.loads(payload)
    par._init_parse(config["kept"])   # this worker's views of the kept readings
    _W.clear()
    _W.update(config)


def _task_groups(spec: tuple[Any, ...], items: list[tuple[Any, ...]]
                 ) -> tuple[list[Any], list[Any]]:
    """Each group of ``items`` read and derived (:func:`_group`; None for one that raised:
    the step reads it itself), and what this worker's kept views hit and kept."""
    from ontology import project_parallel as par

    out = []
    for group, method, args, wrap in items:
        try:
            out.append(_group(spec, method, args, wrap))
        except Exception:  # noqa: BLE001 - the build's process reads it, as serially
            out.append(None)
    return out, [view.take() for view in par._STATE.get("kept_views", [])]


def _group(spec: tuple[Any, ...], method: str, args: tuple[Any, ...], wrap: Any
           ) -> tuple[bool, list[tuple[Any, ...]]]:
    """(remembered, per document (memo key or None, citation, facts, chunks by field)) of
    one group: what ``text_index.store_text_index`` derives from its documents alone."""
    from ontology import project_parallel as par
    from ontology.project import GROUP_LOADS

    if method not in GROUP_LOADS:
        raise ValueError(f"group_load_unknown:{method}")
    source = par._opened(spec)

    def load() -> Any:
        return getattr(source, method)(*args)
    documents, remembered = wrap(load) if wrap is not None else load()
    out = []
    for document in documents:
        facts, pieces = T.document_chunks(document)
        if any(found is None for found in pieces.values()):
            raise _ReadHere(document.citation)
        out.append((T.document_key(document) if _W["keyed"] else None, document.citation,
                    facts, pieces))
    return remembered, out


__all__ = ["HASH_BYTES", "LAST", "MIN_GROUPS", "TASK_GROUPS", "TextReader",
           "store_text_index", "workers"]
