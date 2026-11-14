"""A build's capture fingerprints and shard prints computed by worker processes.

Before a build projects anything it fingerprints every relation its bindings read
(``project_incremental._pin``) and cuts each isolated binding into shard prints
(``project_incremental._shard_prints``). Both ran on the build's one thread: on the live
gen 7 build ``fingerprint`` 1,750 s and ``fingerprint.shards`` 12,851 s, and on the live
build of 2026-11-04 the build's thread sat in the mail attachments' read at about a
quarter of one CPU (the read is SQLite waiting on the disk: each message's latest row
looked up by its row id in a 17 GB file, sorted in a temporary B-tree) while the other
cores were idle.

Here the same functions run on worker processes, one (source, relation) at a time, so the
relations are read side by side and the disk is asked for many pages at once:

* **Relation fingerprints** (:func:`pinned`): before ``_pin`` runs, each source's
  relations that ``_pin`` will fingerprint (not held by watermark or relation mark) are
  sent to the workers, each worker computing one relation's fingerprint with the source's
  own ``fingerprint`` (or ``stream_fingerprint`` for a source without one), as ``_pin``
  does; ``_pin`` then runs unchanged on sources whose ``fingerprint`` takes those values
  (:class:`_Ahead`), and computes any relation a worker did not itself.
* **Shard prints** (:func:`shard_prints`): ``_shard_prints`` already reads the bindings
  over one relation of a source in one pass, independent of every other pass. Each pass
  of a plain relation is run by a worker calling ``_shard_prints`` itself over just that
  pass's bindings, while the build's own process runs the passes over relations derived
  from captured documents (``project_parallel.DERIVED``: their documents parsed by the
  build's document workers, ``project_parallel.DocumentPool``, whose kept fields have one
  writer) and those of sources a worker cannot open again.

So every value is the one the serial functions compute: the same code over the same
bytes. A worker opens a source again from how the build opened it
(``ontology.project.Reopenable``) and reads it only when its watermark equals the one the
build pinned (equal marks, equal bytes); a source without one, a relation a worker could
not read (any exception, a worker that died), or a watermark that moved is computed by the
build's own process as before, and a failure that is the source's raises there as it would
serially. No code a fingerprint or a shard print is computed by changes: ``_pin``,
``_shard_prints`` and the sources' readers keep their digests
(``build_keys.reader_digest``, ``shard_code_digest``), so a build's held prints stay held.

Workers: the build's (``project_incremental._parallel_count``), started with ``spawn`` and
closed before the projection. Memory: each worker holds one pass's bucket digests
(4,096 per binding) and the sources it opened.

Stdlib only.
"""

from __future__ import annotations

import concurrent.futures as futures
import multiprocessing
import pickle
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from ontology.store import canonical, main_not_imported
from ontology import parent_watch

# A worker's sources opened again, by spec: (source, its records' map) or the exception.
_OPENED: dict[bytes, Any] = {}


def _open(spec: tuple[Any, ...]) -> tuple[Any, Mapping[str, Any]]:
    """The source of ``spec`` (``project_parallel._source_spec``) opened again in this
    worker and its records read in one snapshot whose watermark is the build's; else
    raises (the build then computes it itself)."""
    from ontology.project_parallel import ParallelUnavailable

    key = pickle.dumps(spec)
    if key not in _OPENED:
        kind, args, kwargs, relations, mark = spec
        try:
            source = kind(*args, **kwargs)
            records = source.records(list(relations))
            own = source.watermark()
            _OPENED[key] = (source, records) if own is not None and canonical(own) == mark \
                else ParallelUnavailable("source_changed")
        except Exception as exc:  # noqa: BLE001 - computed by the build instead
            _OPENED[key] = ParallelUnavailable(f"source_not_reopened:{type(exc).__name__}")
    found = _OPENED[key]
    if isinstance(found, Exception):
        raise found
    return found


def _relation_print(spec: tuple[Any, ...], relation: str) -> str | None:
    """One relation's fingerprint as ``_pin`` computes it (its source's ``fingerprint``,
    else ``stream_fingerprint`` of its records); None when the source does not give the
    relation (``_pin`` then leaves it out)."""
    from ontology.project import stream_fingerprint

    source, records = _open(spec)
    own = getattr(source, "fingerprint", None)
    if callable(own):
        return own([relation]).get(relation)
    return stream_fingerprint(records[relation]) if relation in records else None


def _shard_pass(spec: tuple[Any, ...], name: str, bindings: Mapping[str, Any],
                prints: Mapping[str, str | None], held: Mapping[str, Any],
                shard_records: int) -> dict[str, dict[str, Any]]:
    """``_shard_prints`` of one pass: the bindings over one relation of source ``name``,
    cut by the build's ``SHARD_RECORDS`` (``shard_records``)."""
    from ontology import project_incremental as inc
    from ontology.project_incremental import _Pinned, _shard_prints

    inc.SHARD_RECORDS = shard_records
    source, records = _open(spec)
    return _shard_prints(_sub_plan(bindings), {name: _Pinned(source, records)}, prints, held)


def _sub_plan(bindings: Mapping[str, Any]) -> Any:
    """What ``_shard_prints`` reads of a plan (its isolated bindings and their
    declarations), over ``bindings`` only."""
    return SimpleNamespace(isolated=frozenset(bindings), bindings=dict(bindings))


class PrintPool:
    """The worker processes a build's prints are computed with (module docstring);
    ``count`` at most, started on first use. ``tasks``: sent to a worker; ``fallback``:
    computed by the build because its worker failed or could not read the source."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.tasks = self.fallback = 0
        self.broken = False
        self._pool: Any = None

    def usable(self) -> bool:
        return self.count > 1 and not self.broken

    def submit(self, fn: Any, *args: Any) -> Any:
        """A future of ``fn(*args)`` on a worker; None when none can take it."""
        if not self.usable():
            return None
        if self._pool is None:
            self._pool = futures.ProcessPoolExecutor(
                max_workers=self.count, mp_context=multiprocessing.get_context("spawn"),
                **parent_watch.pool_init())
        try:
            with main_not_imported():
                future = self._pool.submit(fn, *args)
        except Exception as exc:  # noqa: BLE001 - computed by the build instead
            self.broken = self.broken or isinstance(exc, (futures.BrokenExecutor,
                                                          RuntimeError))
            return None
        self.tasks += 1
        return future

    def result(self, future: Any) -> tuple[bool, Any]:
        """(True, the value) of a worker's future; (False, None) when it raised or died."""
        if future is None:
            return False, None
        try:
            return True, future.result()
        except Exception as exc:  # noqa: BLE001 - computed by the build instead
            self.broken = self.broken or isinstance(exc, futures.BrokenExecutor)
            self.fallback += 1
            return False, None

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def _spec(name: str, source: Any, relations: Sequence[str], mark: Any
          ) -> tuple[Any, ...] | None:
    from ontology.project_parallel import ParallelUnavailable, _source_spec

    try:
        return _source_spec(name, source, relations, mark)
    except ParallelUnavailable:
        return None


def _wanted(sources: Mapping[str, Any], the_plan: Any) -> dict[str, list[str]]:
    """source -> the relations its bindings read, sorted (as ``_pin`` reads them)."""
    relations: dict[str, set[str]] = {}
    for _, b in the_plan.bindings.values():
        if b["source"] in sources:
            relations.setdefault(b["source"], set()).add(b["relation"])
    return {name: sorted(wanted) for name, wanted in sorted(relations.items())}


class _Ahead:
    """A capture source whose ``fingerprint`` takes the relation fingerprints workers
    computed ahead, in the snapshot of the watermark they were computed under; any other
    is computed by the source as ``_pin`` would. Everything else is the source's."""

    def __init__(self, inner: Any, ahead: Mapping[str, tuple[Any, Any]],
                 pool: PrintPool) -> None:
        self._inner, self._ahead, self._pool = inner, ahead, pool
        self._records: Any = None
        self.source = getattr(inner, "source", None)

    def records(self, relations: Any) -> Any:
        self._records = self._inner.records(relations)
        return self._records

    def fingerprint(self, relations: Sequence[str]) -> dict[str, str]:
        from ontology.project import stream_fingerprint

        watermark = getattr(self._inner, "watermark", None)
        mark = watermark() if callable(watermark) else None
        mark = canonical(mark) if mark is not None else None
        out: dict[str, str] = {}
        rest = []
        for relation in dict.fromkeys(relations):
            sent = self._ahead.get(relation)
            ok, value = (False, None)
            if sent is not None and mark is not None and sent[0] == mark:
                ok, value = self._pool.result(sent[1])
            if ok:
                if value is not None:
                    out[relation] = value
            else:
                rest.append(relation)
        if rest:
            own = getattr(self._inner, "fingerprint", None)
            if callable(own):
                out.update(own(rest))
            else:
                out.update({relation: stream_fingerprint(self._records[relation])
                            for relation in rest if relation in self._records})
        return {relation: out[relation] for relation in dict.fromkeys(relations)
                if relation in out}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def ahead_marks(sources: Mapping[str, Any], the_plan: Any) -> dict[str, Any]:
    """source -> ``{"mark": its watermark}`` of a snapshot opened now (what
    ``project_incremental._parallel_count`` sizes a build by)."""
    out = {}
    for name, wanted in _wanted(sources, the_plan).items():
        source = sources[name]
        source.records(wanted)
        watermark = getattr(source, "watermark", None)
        mark = watermark() if callable(watermark) else None
        out[name] = {"mark": canonical(mark) if mark is not None else None}
    return out


def pinned(pin: Any, sources: Mapping[str, Any], the_plan: Any, held: Any,
           pool: PrintPool | None, marks: Mapping[str, Any]
           ) -> tuple[dict[str, Any], dict[str, str | None], dict[str, Any]]:
    """``pin(sources, the_plan, held)`` (``project_incremental._pin``) with the relation
    fingerprints it computes sent to ``pool``'s workers first (module docstring); its
    result is ``pin``'s own, its pinned sources the build's. ``marks``: each source's
    watermark of a snapshot opened just before (:func:`ahead_marks`); a source whose
    watermark has moved since is fingerprinted by ``pin`` itself."""
    from ontology.project_incremental import _Pinned

    if pool is None or not pool.usable():
        return pin(sources, the_plan, held)
    wrapped: dict[str, Any] = dict(sources)
    for name, wanted in _wanted(sources, the_plan).items():
        source, mark = sources[name], (marks.get(name) or {}).get("mark")
        spec = _spec(name, source, wanted, mark)
        if spec is None:
            continue
        kept = (held or {}).get(name) or {}
        if kept.get("mark") == mark and set(wanted) <= set(kept.get("prints") or {}):
            continue   # held whole: ``pin`` reads its prints back
        relation_marks = getattr(source, "relation_marks", None)
        per = relation_marks(wanted) if callable(relation_marks) else None
        per = {r: canonical(m) for r, m in per.items()} if per is not None else None
        rest = [relation for relation in wanted
                if not (per is not None and relation in (kept.get("prints") or {})
                        and (kept.get("relations") or {}).get(relation) == per[relation])]
        ahead = {relation: (mark, pool.submit(_relation_print, spec, relation))
                 for relation in rest}
        wrapped[name] = _Ahead(source, ahead, pool)
    found, prints, out_marks = pin(wrapped, the_plan, held)
    for name, value in found.items():   # the build's own sources, as ``pin`` pinned them
        if isinstance(value, _Pinned) and isinstance(value._inner, _Ahead):
            found[name] = _Pinned(value._inner._inner, value._records)
    return found, prints, out_marks


def shard_prints(shard: Any, the_plan: Any, pinned_sources: Mapping[str, Any],
                 fingerprints: Mapping[str, str | None], held: Mapping[str, Any] | None,
                 pool: PrintPool | None, marks: Mapping[str, Any]
                 ) -> dict[str, dict[str, Any]]:
    """``shard(the_plan, pinned_sources, fingerprints, held)``
    (``project_incremental._shard_prints``) with each pass over a plain relation of a
    source a worker can open again run by a worker (module docstring), the others by the
    build meanwhile; the same entries in the same order."""
    from ontology import project_incremental as inc
    from ontology.project_parallel import DERIVED

    if pool is None or not pool.usable():
        return shard(the_plan, pinned_sources, fingerprints, held)
    passes: dict[tuple[str, str], dict[str, Any]] = {}
    for bid in sorted(the_plan.isolated):
        _, b = the_plan.bindings[bid]
        passes.setdefault((b["source"], b["relation"]), {})[bid] = the_plan.bindings[bid]
    wanted = _wanted(pinned_sources, the_plan)
    here: dict[str, Any] = {}
    sent: list[tuple[dict[str, Any], Any]] = []
    for (name, relation), bindings in passes.items():
        source = pinned_sources.get(name)
        reads = source is not None and any(
            fingerprints.get(bid) is not None and not (
                ((held or {}).get(bid) or {}).get("relation") == fingerprints.get(bid)
                and isinstance(((held or {}).get(bid) or {}).get("prints"), list))
            for bid in bindings)
        spec = _spec(name, source, wanted.get(name, ()), (marks.get(name) or {}).get("mark")) \
            if reads and relation not in DERIVED else None
        future = None if spec is None else pool.submit(
            _shard_pass, spec, name, bindings, {bid: fingerprints.get(bid) for bid in bindings},
            {bid: held[bid] for bid in bindings if held and bid in held}, inc.SHARD_RECORDS)
        if future is None:
            here.update(bindings)
        else:
            sent.append((bindings, future))
    out = shard(_sub_plan(here), pinned_sources, fingerprints, held) if here else {}
    for bindings, future in sent:
        ok, value = pool.result(future)
        out.update(value if ok else shard(_sub_plan(bindings), pinned_sources, fingerprints,
                                          held))
    return {bid: out[bid] for bid in sorted(the_plan.isolated) if bid in out}


__all__ = ["PrintPool", "ahead_marks", "pinned", "shard_prints"]
