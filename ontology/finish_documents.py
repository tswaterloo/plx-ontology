"""The ``mentions`` step's documents read, and their links found, by the build's workers.

Measured on the live gen 8 build: the ``mentions`` step's first phase
(``ontology.mentions._store_links``) ran on one core for more than half an hour while the
host's other cores stood idle: per captured record it read the group's documents (a mail
body blob, each attachment's kept reading, the members of a zip), extracted their
mentions, looked each document up in the step's memo and resolved every mention through
the generation's identifier index. Here ``PLX_BUILD_WORKERS`` processes do that per-group
work; the build's process runs ``mentions._store_links`` itself, unchanged, in the
serial order, and is handed what each worker found instead of computing it again. So the
step decides, counts, batches, keeps in its memo and writes exactly what the serial step
does (``ontology/tests/test_finish_documents.py`` compares the two, memo hits, misses and
failures included). ``ontology.mentions`` and ``ontology.mention_memo`` are not changed:
their bytes are digested (``ontology.code_equivalence``, the memo code), so this module
calls their functions and is imported by ``ontology.finish_parallel`` only.

How (:class:`GroupReader`):

* The build's rows are committed first (as ``ontology.finish_parallel`` does to corroborate),
  so a worker's read-only connection to the build's file reads what the build's process
  reads: the step's own links go to a working file (``finish_parallel._Diverted``) and
  nothing else is written to the build's file while the workers read it.
* A group whose load is a capture source's ``GroupLoad`` (``ontology.project``; wrapped
  by a module-level function such as ``operating.build_steps._load_clean``) is sent to a
  worker that opens the source again under the build's watermark
  (``project_parallel._opened``) and reads the kept readings through its own views
  (``project_parallel._init_parse``; the build's process records what they hit and kept,
  as for ``DocumentPool``). The worker reads the memo read-only, and for each document
  does what the serial ``derive`` would: the document key, the memo entry, whether a kept
  resolution still holds (``mentions._still_resolves``), the mentions and the link rows
  (``mentions._document_links``).
* While ``mentions._store_links`` runs, its ``read_ahead`` is this reader's (the groups in
  their order, each with its documents and mentions), and ``document_key``,
  ``_still_resolves`` and ``_document_links`` answer from the worker's result when called
  with exactly the inputs the worker used (the same document, mentions, memo entry, build
  connection, declared types, aliases and change test), else compute as before. The memo
  is read through :class:`_MemoView`: an entry this step has not written is the one the
  worker read; one it wrote is read from the memo itself.
* A group a worker could not read or derive (an exception, a dead worker, a source that
  changed, a load that is not a ``GroupLoad``) is left to the build's process, which reads
  and derives it as the serial step does, its failure counted the same way.

Stdlib only.
"""

from __future__ import annotations

from collections import deque
import concurrent.futures as futures
import contextlib
import functools
import multiprocessing
from pathlib import Path
import pickle
import sqlite3
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

from ontology import mentions as M

TASK_GROUPS = 8          # groups a worker reads in one task
# tasks per worker in flight ahead of the step: the step takes the groups in order, so
# one slow group stalls every worker once the window is spent (4: workers ~20% busy
# on 3.3M mail groups; 64 measured ~2x the rate, about 8k groups ahead on 16 workers)
WINDOW_TASKS = 64
MIN_GROUPS = 256         # fewer groups: the workers cost more than they save
WORKER_CACHE_KIB = 65536
_MISSING = object()


def _plain_load(load: Any) -> tuple[Any, Callable[..., Any] | None] | None:
    """(the ``GroupLoad``, the module-level function it is wrapped by or None) of a
    group's load; None when it is neither."""
    from ontology.project import GroupLoad

    if isinstance(load, GroupLoad):
        return load, None
    if (isinstance(load, functools.partial) and not load.keywords and len(load.args) == 1
            and isinstance(load.args[0], GroupLoad)):
        return load.args[0], load.func
    return None


class _Current:
    """What the workers found for the group the step is at."""

    def __init__(self) -> None:
        self.found: dict[str, Any] = {}       # document key -> memo entry (or None)
        self.groups: dict[str, Any] = {}      # group key -> kept document keys (or None)
        self.keys: dict[int, tuple[Any, str]] = {}   # id(document) -> (document, key)
        self.derived: dict[str, list[tuple[Any, ...]]] = {}   # citation -> results

    def clear(self) -> None:
        self.found, self.groups, self.keys, self.derived = {}, {}, {}, {}


class _MemoView:
    """The step's memo as ``mentions._store_links`` reads it: an entry (and a group) this
    step has not written is the one the worker read for the current group; anything else
    is the memo's own. Writes are the memo's."""

    def __init__(self, memo: Any, current: _Current) -> None:
        self._memo, self._current = memo, current
        self._written: set[str] = set()
        self._groups_written: set[str] = set()

    def mentions(self, key: str, code: str) -> Any:
        if key not in self._written:
            found = self._current.found.get((key, code), _MISSING)
            if found is not _MISSING:
                return found
        return self._memo.mentions(key, code)

    def group(self, key: str) -> Any:
        if key not in self._groups_written:
            found = self._current.groups.get(key, _MISSING)
            if found is not _MISSING:
                if found is not None:
                    self._memo._used("groups", key)   # as ``DocumentMemo.group`` marks it
                return found
        return self._memo.group(key)

    def keep_mentions(self, key: str, code: str, entry: Mapping[str, Any]) -> None:
        self._written.add(key)
        self._memo.keep_mentions(key, code, entry)

    def still_valid(self, key: str) -> None:
        self._written.add(key)
        self._memo.still_valid(key)

    def keep_group(self, key: str, documents: Any) -> None:
        self._groups_written.add(key)
        self._memo.keep_group(key, documents)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._memo, name)


class _Ready:
    """A document's mentions a worker extracted (``read_ahead``'s ``Derived``)."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value


class GroupReader:
    """The groups of a ``mentions`` step read and derived by ``count`` worker processes
    (module docstring). Use: ``start`` (False: serial), then ``patched()`` around
    ``mentions._store_links`` with ``memo_view`` as its memo; ``close`` always."""

    def __init__(self, *, store: str, conn: sqlite3.Connection, scratch: Path,
                 declared: Mapping[tuple[str, str], str], aliases: Any, memo: Any,
                 changed: Callable[[str, str], bool] | None, count: int,
                 min_groups: int = MIN_GROUPS) -> None:
        self.store, self.conn, self.scratch = store, conn, scratch
        self.declared, self.aliases, self.memo, self.changed = declared, aliases, memo, changed
        self.count, self.min_groups = count, min_groups
        self._payload = b""
        self.current = _Current()
        self.memo_view = _MemoView(memo, self.current) if memo is not None else None
        self.code = M._memo_code(declared) if memo is not None else ""
        self.extractor = M.extractor_version()
        self.reason: str | None = None
        self.stats = {"tasks": 0, "groups": 0, "groups_held": 0, "groups_here": 0,
                      "failed_tasks": 0, "seconds_waited": 0.0}
        self._pool: Any = None
        self._kept: list[Any] = []
        self._specs: dict[int, Any] = {}
        self._wraps: dict[Any, bool] = {}
        self._broken = False
        self._changed_path = scratch.with_name(scratch.name + ".changed")

    # ---------------------------------------------------------------- set up

    def _config(self) -> dict[str, Any] | None:
        from ontology import project_parallel as par

        memo = None
        if self.memo is not None:
            path = _memo_path(self.memo)
            if path is None:
                self.reason = "memo_path_unknown"
                return None
            memo = (path, self.memo.base, self.memo.token, self.memo.number)
        try:
            aliases = pickle.dumps(self.aliases, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:  # noqa: BLE001 - derived here instead
            self.reason = f"aliases_not_portable:{type(exc).__name__}"
            return None
        changed = self._changed_spec()
        if changed is _MISSING:
            return None
        self._kept = par._kept_stores()
        return {"store": self.store, "memo": memo, "code": self.code, "declared":
                dict(self.declared), "aliases": aliases, "changed": changed,
                "extractor": self.extractor, "kept": self._kept}

    def _changed_spec(self) -> Any:
        """How a worker tells a changed identifier key: None (no change test), ``all``
        (every key), or the path of a copy of the build's changed keys."""
        from ontology.project_incremental import BuildContext

        changed = self.changed
        if changed is None:
            return None
        context = getattr(changed, "__self__", None)
        if not (isinstance(context, BuildContext)
                and getattr(changed, "__func__", None) is BuildContext.changed_key):
            self.reason = "change_test_unknown"
            return _MISSING
        if context.base is None:
            return "all"
        path = self._changed_path
        with contextlib.closing(sqlite3.connect(str(path), isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode = OFF")
            db.execute("CREATE TABLE changed (identifier_kind TEXT, normalized TEXT)")
            db.execute("BEGIN")
            cursor = context._conn.execute(
                "SELECT identifier_kind, normalized FROM temp.plx_changed_keys")
            while True:
                rows = cursor.fetchmany(10000)
                if not rows:
                    break
                db.executemany("INSERT INTO changed VALUES (?, ?)", rows)
            db.execute("CREATE INDEX changed_key ON changed(identifier_kind, normalized)")
            db.execute("COMMIT")
        return str(path)

    def start(self) -> bool:
        """Ready the workers (started once ``min_groups`` groups are ahead); False (the
        step runs serially) when they cannot read this build."""
        try:
            config = self._config()
            if config is None:
                return False
            self._payload = pickle.dumps(config, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:  # noqa: BLE001 - the step runs serially, as before
            self.reason = f"workers_failed:{type(exc).__name__}"
            self.close()
            return False
        return True

    def _started(self) -> bool:
        from ontology.store import main_not_imported

        if self._pool is None and not self._broken:
            try:
                with main_not_imported():
                    self._pool = futures.ProcessPoolExecutor(
                        max_workers=self.count,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=_init, initargs=(self._payload,))
            except Exception as exc:  # noqa: BLE001 - every group read here
                self.reason = f"workers_failed:{type(exc).__name__}"
                self._broken = True
        return self._pool is not None and not self._broken

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        try:
            self._changed_path.unlink()
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------------- the groups

    def _portable(self, load: Any) -> tuple[Any, ...] | None:
        """(source spec, method, args, wrap) a worker reads ``load`` by; None: read here."""
        return portable_load(load, self._specs, self._wraps)

    def read_ahead(self, groups: Iterable[M.DocumentGroup], *_: Any, **__: Any
                   ) -> Iterator[tuple[Any, Any, Any]]:
        """``mentions.read_ahead`` for ``_store_links``: each group in its order, as the
        workers read it (module docstring)."""
        entries: deque[list[Any]] = deque()   # [group, load, task, index]
        batch: list[list[Any]] = []
        items: list[tuple[Any, ...]] = []
        spec_of_batch: Any = None
        in_flight = 0
        limit = self.count * WINDOW_TASKS

        from ontology.store import main_not_imported

        def submit() -> None:
            nonlocal in_flight, spec_of_batch
            if not batch:
                return
            task: dict[str, Any] = {"future": None, "results": None}
            if self._started():
                try:
                    with main_not_imported():   # a worker starts on a submit
                        task["future"] = self._pool.submit(_task, spec_of_batch,
                                                           list(items))
                    in_flight += 1
                    self.stats["tasks"] += 1
                except Exception:  # noqa: BLE001 - read here
                    self._broken = True
            for index, entry in enumerate(batch):
                entry[2], entry[3] = task, index
            batch.clear()
            items.clear()
            spec_of_batch = None

        def resolve(entry: list[Any]) -> tuple[Any, Any, Any]:
            nonlocal in_flight
            if entry[2] is _MISSING:   # in the batch not sent yet
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
                    self._absorb(taken)
                    task["results"] = results
                except Exception as exc:  # noqa: BLE001 - its groups are read here
                    self._broken = self._broken or isinstance(exc, futures.BrokenExecutor)
                    self.stats["failed_tasks"] += 1
                    task["results"] = []
            results = task["results"] if task is not None else None
            found = results[entry[3]] if results and entry[3] < len(results) else None
            return self._yield(entry[0], entry[1], found)

        groups = iter(groups)
        head = []   # the first groups: fewer than ``min_groups`` are all read here
        for pair in groups:
            head.append(pair)
            if len(head) >= self.min_groups:
                break
        if len(head) < self.min_groups:
            self.reason = "few_groups"
            for group, load in head:
                yield self._yield(group, load, None)
            self.current.clear()
            return
        for group, load in (pair for part in (head, groups) for pair in part):
            entry = [group, load, None, 0]
            portable = None if self._broken else self._portable(load)
            if portable is not None:
                spec, method, args, wrap = portable
                if batch and spec is not spec_of_batch:
                    submit()
                spec_of_batch = spec
                entry[2] = _MISSING
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
        self.current.clear()

    def _absorb(self, taken: Any) -> None:
        absorb_kept(self._kept, taken)

    def _yield(self, group: Any, load: Any, found: Any) -> tuple[Any, Any, Any]:
        """Set what the workers found for this group and give it to the step."""
        current = self.current
        current.clear()
        if found is None:   # read here, as the serial step reads it
            self.stats["groups_here"] += 1
            return group, load, None
        self.stats["groups"] += 1
        kind, kept, memo_found, documents, remembered, results = found
        if self.memo is not None:
            current.found = {(key, self.code): value for key, value in memo_found.items()}
            if group is not None:
                current.groups = {group: kept}
        for document, (key, still, mentioned, resolved, used) in zip(
                documents if kind == "read" else [None] * len(results), results):
            if kind == "read" and key is not None:
                current.keys[id(document)] = (document, key)
            facts = document if document is not None else _MISSING
            current.derived.setdefault(_citation(document, used), []).append(
                (facts, used, still, mentioned, resolved))
        if kind == "held":
            self.stats["groups_held"] += 1
            return group, load, None
        return group, load, (documents, remembered, [_Ready(r[2]) for r in results])

    # ---------------------------------------------------------------- the step's functions

    @contextlib.contextmanager
    def patched(self) -> Iterator[None]:
        """``mentions``' read-ahead and per-document functions answered from the workers'
        results while ``_store_links`` runs (module docstring)."""
        real = {name: getattr(M, name) for name in
                ("read_ahead", "document_key", "_still_resolves", "_document_links")}
        current = self.current

        def document_key(document: Any) -> str:
            hit = current.keys.get(id(document))
            if hit is not None and hit[0] is document:
                return hit[1]
            return real["document_key"](document)

        def same(document: Any, facts: Any, used: Any) -> bool:
            if facts is not _MISSING:
                return facts is document
            return document == used[0]

        def still_resolves(conn: Any, document: Any, resolved: Any, aliases: Any,
                           changed: Any) -> bool:
            if conn is self.conn and aliases is self.aliases and changed is self.changed:
                for facts, used, still, _m, _r in current.derived.get(
                        getattr(document, "citation", None), ()):
                    if still is not None and same(document, facts, used) and \
                            resolved is current.found_resolved(used, facts):
                        return still
            return real["_still_resolves"](conn, document, resolved, aliases, changed)

        def document_links(conn: Any, declared: Any, document: Any, found: Any,
                           aliases: Any, extractor: str) -> Any:
            if conn is self.conn and declared is self.declared and \
                    aliases is self.aliases and extractor == self.extractor:
                for facts, used, _s, mentioned, resolved in current.derived.get(
                        getattr(document, "citation", None), ()):
                    if resolved is not None and same(document, facts, used) and (
                            mentioned is found or mentioned == found):
                        return resolved
            return real["_document_links"](conn, declared, document, found, aliases,
                                           extractor)

        current.found_resolved = self._found_resolved   # type: ignore[attr-defined]
        M.read_ahead = self.read_ahead                  # type: ignore[assignment]
        M.document_key = document_key                   # type: ignore[assignment]
        M._still_resolves = still_resolves              # type: ignore[assignment]
        M._document_links = document_links              # type: ignore[assignment]
        try:
            yield
        finally:
            for name, function in real.items():
                setattr(M, name, function)
            current.clear()

    def _found_resolved(self, used: Any, facts: Any) -> Any:
        """The memo entry's ``resolved`` the worker tested for the document (its key)."""
        key = used[1] if isinstance(used, tuple) else None
        found = self.current.found.get((key, self.code))
        return None if found is None else found[0].get("resolved")


def portable_load(load: Any, specs: dict[int, Any], wraps: dict[Any, bool]
                  ) -> tuple[Any, ...] | None:
    """(source spec, method, args, wrap) a worker reads a group's ``load`` by (a capture
    source's ``GroupLoad``, perhaps wrapped by a module-level function); None: it is read
    by the build's process. ``specs`` and ``wraps``: the caller's caches, by source and
    by wrapping function."""
    from ontology import project_parallel as par
    from ontology.store import canonical

    plain = _plain_load(load)
    if plain is None:
        return None
    group_load, wrap = plain
    if wrap is not None:
        if wrap not in wraps:
            try:
                pickle.dumps(wrap)
                wraps[wrap] = True
            except Exception:  # noqa: BLE001 - not a module-level function
                wraps[wrap] = False
        if not wraps[wrap]:
            return None
    source = group_load.source
    if id(source) not in specs:
        spec = None
        pool = par.DOCUMENTS
        if pool is not None:
            spec = getattr(pool, "_specs", {}).get(id(source))
        if spec is None:   # outside a generation build: the source as it is open now
            try:
                mark = source.watermark()
                spec = None if mark is None else par._source_spec(
                    "", source, (), canonical(mark))
            except Exception:  # noqa: BLE001 - read here
                spec = None
        specs[id(source)] = spec
    spec = specs[id(source)]
    if spec is None:
        return None
    try:
        method, args = group_load.portable()
    except Exception:  # noqa: BLE001 - read here
        return None
    return spec, method, args, wrap


def absorb_kept(kept: Any, taken: Any) -> None:
    """Record in the build's kept readings what a worker's views of them hit and kept
    (``project_parallel._init_parse``)."""
    from ontology import project_parallel as par

    for (module, memo, *_), (used, new) in zip(kept, taken):
        par._kept_memo(module, memo).backing.absorb(used, new)


def _citation(document: Any, used: Any) -> Any:
    if document is not None:
        return document.citation
    return used[0].citation if isinstance(used, tuple) else None


def _memo_path(memo: Any) -> str | None:
    """The file of a ``mention_memo.DocumentMemo`` (its connection's main database)."""
    try:
        for _seq, name, path in memo._db.execute("PRAGMA database_list"):
            if name == "main" and path:
                return path
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------- the workers

_W: dict[str, Any] = {}


def _init(payload: bytes) -> None:
    from ontology import project_parallel as par
    from ontology.mention_memo import DocumentMemo

    config = pickle.loads(payload)
    par._init_parse(config["kept"])   # this worker's views of the kept readings
    _W.clear()
    _W.update(config)
    conn = sqlite3.connect(Path(config["store"]).as_uri() + "?mode=ro", uri=True, timeout=60)
    conn.execute(f"PRAGMA cache_size = -{WORKER_CACHE_KIB}")
    _W["conn"] = conn
    _W["aliases"] = pickle.loads(config["aliases"])
    memo = None
    if config["memo"] is not None:
        path, base, token, number = config["memo"]
        # The memo's own reads (``mentions``, ``peek_group``) over a read-only connection:
        # nothing is written from a worker.
        memo = DocumentMemo.__new__(DocumentMemo)
        memo.base, memo.token, memo.number, memo._pending = base, token, number, 0
        memo._db = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True,
                                   isolation_level=None, timeout=60)
    _W["memo"] = memo
    changed = config["changed"]
    if changed is None:
        _W["changed"] = None
    elif changed == "all":
        _W["changed"] = _every_key
    else:
        db = sqlite3.connect(Path(changed).as_uri() + "?mode=ro", uri=True)

        def is_changed(kind: str, normalized: str) -> bool:
            return db.execute("SELECT 1 FROM changed WHERE identifier_kind = ? AND "
                              "normalized = ?", (kind, normalized)).fetchone() is not None
        _W["changed"] = is_changed


def _every_key(_kind: str, _normalized: str) -> bool:
    return True


def _task(spec: tuple[Any, ...], items: list[tuple[Any, ...]]) -> tuple[list[Any], list[Any]]:
    """Each group of ``items`` read and derived (:func:`_group`; None for one that raised:
    the step reads it itself), and what this worker's kept views hit and kept."""
    from ontology import project_parallel as par

    out = []
    for group, method, args, wrap in items:
        try:
            out.append(_group(spec, group, method, args, wrap))
        except Exception:  # noqa: BLE001 - the build's process reads it, as serially
            out.append(None)
    return out, [view.take() for view in par._STATE.get("kept_views", [])]


def _group(spec: tuple[Any, ...], group: Any, method: str, args: tuple[Any, ...],
           wrap: Any) -> tuple[Any, ...]:
    """What ``mentions._store_links`` does with one group, read-only: ``("held", kept
    keys, memo entries, [], True, results)`` for a group the memo holds whole (its
    documents are not read), else ``("read", kept keys or None, memo entries, documents,
    remembered, results)``; a result per document is (key, still resolves or None,
    mentions, link rows (``_document_links``' value) or None, the document a held
    group's result is for)."""
    from ontology import project_parallel as par
    from ontology.project import GROUP_LOADS

    memo, code = _W["memo"], _W["code"]
    found: dict[str, Any] = {}
    kept = None
    if memo is not None and group is not None:
        kept = memo.peek_group(group)
        if kept is not None:
            for key in kept:
                found[key] = memo.mentions(key, code)
            if all(found[key] is not None for key in kept):
                results = []
                for key in kept:
                    document = M._facts_document(found[key][0]["document"])
                    results.append(_derive(key, found[key], document, (document, key)))
                return "held", kept, found, [], True, results
    source = par._opened(spec)
    if method not in GROUP_LOADS:
        raise ValueError(f"group_load_unknown:{method}")

    def load() -> Any:
        return getattr(source, method)(*args)
    documents, remembered = wrap(load) if wrap is not None else load()
    results = []
    for document in documents:
        key = M.document_key(document) if memo is not None else None
        if memo is not None and key not in found:
            found[key] = memo.mentions(key, code)
        results.append(_derive(key, found.get(key), document, (None, key)))
    return "read", kept, found, documents, remembered, results


def _derive(key: Any, found: Any, document: Any, used: Any) -> tuple[Any, ...]:
    """``_store_links``' ``derive`` of one document, read-only (``_group``)."""
    conn, aliases, changed = _W["conn"], _W["aliases"], _W["changed"]
    still = None
    if found is not None:
        entry, valid = found
        if valid and changed is not None:
            still = M._still_resolves(conn, document, entry["resolved"], aliases, changed)
            if still:
                return key, True, None, None, used
    mentioned = found[0]["mentions"] if found is not None else M.document_mentions(document)
    resolved = M._document_links(conn, _W["declared"], document, mentioned, aliases,
                                 _W["extractor"])
    return key, still, mentioned, resolved, used


__all__ = ["GroupReader", "MIN_GROUPS", "TASK_GROUPS", "WINDOW_TASKS", "absorb_kept",
           "portable_load"]
