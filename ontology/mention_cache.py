"""The ``mentions`` step's extracted mentions kept between migrations, keyed by the
extractor's code alone, so the next migration resolves them without reading the mail.

Measured 2026-11-09 on the live host: the step migration's ``mentions`` step reads every
captured document again (95 min warm, 3.5-4 h cold, 16 workers), because the generation
memo (``ontology.mention_memo``, ``mentions.sqlite``) keys an entry by the code of the
whole step (``mentions._memo_code``: the extractor, the resolver, the declared link
types), and the step migration hands the step no memo at all. Re-resolving cached
mentions costs 3.28 ms a document on one core (12,000 random memo entries against the kept
generation store): 5.67M documents in about 22 min on 14 workers.

Here an :class:`ExtractionCache` (one file, ``0600``) keeps, per document
(``mention_memo.document_key``: the digest of the document's whole value), what is a
function of the document and the extractor alone: its mentions and the facts resolving
reads of it (``mentions._document_facts``), under :func:`extractor_code` (the source of
``ontology/mentions.py`` up to its document readers, ``ontology/identifiers.py``,
``ontology/units.py``, the identifier grammar, ``integrations/captured_pages.py``, the
extractor version and the Python version); and per captured record (``groups``) the keys
of its documents. It is a ``DocumentMemo`` to the step: its ``mentions`` view shows each
entry kept under this extractor code as a ``mentions`` entry of the step's own code
(:meth:`ExtractionCache.for_step`), never valid in a base generation, so the step (and
its workers, ``ontology.finish_documents``, which read the file read-only) takes the
mentions from it and resolves them again against this build's rows; a record whose
documents are all held is not read. Nothing it resolved is kept: a resolution is a
function of the resolver, the link types and the rows, which a migration changes.

A mail record's group key (``MailCaptures.document_groups``) holds the repr of a frozenset
of the internal domains, whose order follows the process's string hash seed: the keys a
prefill keeps are the step's only when both run under one fixed ``PYTHONHASHSEED``
(measured on the 1% sample: every mail group read again under another seed; its mentions
still came from the cache). ``/data/scratch/cfg/fast_build.sh`` runs every part with 0.

Two writers:

* :func:`cached_step`: the step migration's ``mentions`` step with the cache as its memo
  (``python3 -m ontology.step_layer_workers build ... --mention-cache FILE``): every
  document it reads is kept, so the next migration's step reads none;
* :func:`prefill` (``python3 -m ontology.mention_cache prefill``): every document of the
  step's sources read and extracted by the document workers, nothing resolved, run while
  the unit layer builds (it needs neither the unit layer nor the step input).

Why a module of its own: ``ontology.mentions``, ``ontology.mention_memo`` and
``ontology.finish_documents`` are derivation code (the kept fields' key) and step code
(the plan's digests); nothing either covers imports this module.

Stdlib only.
"""

from __future__ import annotations

import contextlib
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from ontology.mention_memo import DocumentMemo, _pack

FORMAT = "plx-mention-extract/v1"
ENV = "PLX_MENTION_CACHE"
FILE = "mention-extract.sqlite"
_READERS_MARK = "\ndef mail_documents("


def extractor_code() -> str:
    """The digest of everything a document's extracted mentions are a function of
    besides the document: the extraction part of ``ontology/mentions.py`` (all of it
    when its document readers cannot be told apart), the identifier grammar and its
    modules, the page layout and the Python version."""
    from ontology import identifiers as ids
    from ontology import mentions as M
    root = Path(M.__file__).resolve().parents[1]
    source = Path(M.__file__).read_bytes()
    cut = source.find(_READERS_MARK.encode())
    digest = sha256(f"{FORMAT}\n{sys.version_info[:2]}\n{M.extractor_version()}\n".encode())
    digest.update(source[:cut] if cut > 0 else source)
    for path in (Path(ids.__file__), root / "ontology" / "units.py", Path(ids.SCHEMA_PATH),
                 root / "integrations" / "captured_pages.py"):
        digest.update(f"\n{path.name}\n".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _literal(text: str) -> str:
    if not all(c in "0123456789abcdef" for c in text):
        raise ValueError("code_not_hex")
    return f"'{text}'"


class ExtractionCache(DocumentMemo):
    """The cache file at ``path`` (module docstring) as a ``DocumentMemo``."""

    _BATCH = 2000

    def __init__(self, path: str | Path, xcode: str | None = None) -> None:
        self.base, self.token, self.number = None, "extract", 0
        self.xcode = xcode or extractor_code()
        self.path = Path(path)
        self._pending = 0
        self.counts = {"held": 0, "kept": 0, "groups_kept": 0}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        self._db = sqlite3.connect(str(self.path), isolation_level=None, timeout=600)
        try:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = NORMAL")
            from operating import declared_schema
            from operating.declared_schema import mention_cache as declared
            declared_schema.apply(self._db, declared)
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('format', ?)", (FORMAT,))
            self.for_step(None)
            self._db.execute("BEGIN")
        except BaseException:
            self._db.close()
            raise

    def for_step(self, code: str | None) -> None:
        """Show the entries of this extractor code as ``mentions`` entries of the step's
        memo ``code`` (``mentions._memo_code``; None: of no code), to this process and
        to every reader of the file (the step's workers open it read-only)."""
        open_before = self._db.in_transaction
        if open_before:
            self._db.execute("COMMIT")
        shown = _literal(code) if code is not None else "NULL"
        self._db.execute("DROP VIEW IF EXISTS mentions")
        self._db.execute(
            f"CREATE VIEW mentions AS SELECT key, {shown} AS code, NULL AS valid_at, value, "
            f"used FROM extracted WHERE xcode = {_literal(self.xcode)}")
        if open_before:
            self._db.execute("BEGIN")

    # -- what the step reads -------------------------------------------------------

    def holds(self, key: str) -> bool:
        try:
            return self._db.execute("SELECT 1 FROM extracted WHERE key = ? AND xcode = ?",
                                    (key, self.xcode)).fetchone() is not None
        except sqlite3.Error:
            return False

    def holds_whole(self, group: str) -> bool:
        """Whether every document of captured record ``group`` is held."""
        found = self.peek_group(group)
        return found is not None and all(self.holds(key) for key in found)

    def mentions(self, key: str, code: str) -> tuple[dict[str, Any], bool] | None:
        found = super().mentions(key, code)
        return None if found is None else (found[0], False)   # never a base's resolution

    # -- what the step writes ------------------------------------------------------

    def keep_mentions(self, key: str, code: str, entry: Mapping[str, Any]) -> None:
        # an entry is a function of the document and the extractor: one held is this one
        if self.holds(key):
            self.counts["held"] += 1
            return
        self.keep(key, entry["document"], entry["mentions"])

    def keep(self, key: str, facts: Mapping[str, Any], mentioned: Sequence[Any]) -> None:
        try:
            self._db.execute(
                "INSERT INTO extracted (key, xcode, value, used) VALUES (?, ?, ?, 0) "
                "ON CONFLICT (key) DO UPDATE SET xcode = excluded.xcode, "
                "value = excluded.value", (key, self.xcode,
                                           _pack({"document": dict(facts),
                                                  "mentions": list(mentioned)})))
            self.counts["kept"] += 1
            self._wrote()
        except sqlite3.Error:   # not kept: read again next time
            return

    def keep_group(self, key: str, documents: Sequence[str]) -> None:
        super().keep_group(key, documents)
        self.counts["groups_kept"] += 1

    def still_valid(self, key: str) -> None:
        return

    def _used(self, table: str, key: str, valid: bool = False) -> None:
        return   # entries are dropped by extractor code, not by use

    def text(self, key: str, code: str) -> Any:
        return None

    def keep_text(self, key: str, code: str, value: Any) -> None:
        return

    def peek_text(self, key: str, code: str) -> bool:
        return False

    def close(self, *, prune: bool = False) -> None:
        """Commit and close; ``prune``: drop the entries of other extractor codes."""
        try:
            if prune:
                self._db.execute("DELETE FROM extracted WHERE xcode <> ?", (self.xcode,))
            self._db.execute("COMMIT")
        except sqlite3.Error:
            pass
        finally:
            self._db.close()


class MigrationContext:
    """The ``build_context`` the step migration's ``mentions`` step reads: the cache as
    its memo, every identifier key changed (no kept resolution is ever taken)."""

    changed_key = None

    def __init__(self, cache: ExtractionCache) -> None:
        self.cache = cache

    def document_memo(self) -> ExtractionCache:
        return self.cache


LAST: dict[str, Any] = {}


def cached_step(step: Callable[[Any, Any], Any], path: str | Path
                ) -> Callable[[Any, Any], Any]:
    """The ``mentions`` step ``step`` with the cache at ``path`` as its memo."""
    import functools

    @functools.wraps(step)
    def run(builder: Any, projector: Any) -> Any:
        from ontology import mentions as M
        cache = ExtractionCache(path)
        try:
            cache.for_step(M._memo_code(M.mention_link_types(
                getattr(projector, "links", {}) or {})))
            held = getattr(projector, "build_context", None)
            projector.build_context = MigrationContext(cache)
            try:
                result = step(builder, projector)
            finally:
                projector.build_context = held
            LAST.clear()
            LAST.update(cache.counts, hash_seed_fixed=not sys.flags.hash_randomization)
            print(f"mention cache {path}: {json.dumps(LAST, sort_keys=True)}",
                  file=sys.stderr, flush=True)
            return result
        finally:
            cache.close(prune=False)
    return run


# --------------------------------------------------------------------------- prefill

def prefill(sources: Mapping[str, Any], new: Any, path: str | Path, *,
            workers: int | None = None, log: Any = None) -> dict[str, Any]:
    """Read every document of ``sources`` (the ``mentions`` step's capture sources) not
    held yet, extract its mentions (by the document workers when there are) and keep
    them; nothing is resolved. A document or record that fails is left to the step."""
    from ontology import mentions as M
    from ontology import project_parallel as par
    from ontology.mention_memo import document_key
    from ontology.project import Projector
    from ontology.store import canonical
    from ontology.unit_layer_workers import document_workers
    from operating.build_steps import clean_document

    started = time.monotonic()
    cache = ExtractionCache(path)
    report = {"groups": 0, "groups_held": 0, "documents": 0, "documents_held": 0,
              "extracted": 0, "failed_groups": 0, "failed_documents": 0}
    try:
        projector = Projector(new, dict(sources))
        with document_workers(workers) as pool:
            if pool is not None:
                marks, relations = {}, {}
                for name, source in sources.items():
                    try:
                        mark = source.watermark()
                    except Exception:  # noqa: BLE001 - read in this process
                        mark = None
                    marks[name] = {"mark": canonical(mark) if mark is not None else None}
                    relations[name] = []
                pool.know(sources, marks, relations)
            ahead = M.read_ahead(M.source_document_groups(projector), "mentions",
                                 cache.holds_whole,
                                 lambda d: not cache.holds(document_key(clean_document(d))))
            for group, load, read in ahead:
                report["groups"] += 1
                if read is None and group is not None and cache.holds_whole(group):
                    report["groups_held"] += 1
                    continue
                try:
                    if read is not None:
                        documents, remembered, derived = read
                    else:
                        (documents, remembered), derived = load(), None
                except Exception:  # noqa: BLE001 - the step reads it again
                    report["failed_groups"] += 1
                    continue
                keys: list[str] = []
                whole = remembered
                for index, raw in enumerate(documents):
                    report["documents"] += 1
                    try:
                        document = clean_document(raw)
                        key = document_key(document)
                        keys.append(key)
                        if cache.holds(key):
                            report["documents_held"] += 1
                            continue
                        mentioned = None
                        if document is raw and derived is not None and \
                                derived[index] is not None:
                            mentioned = derived[index].get()
                        if mentioned is None:
                            mentioned = M.document_mentions(document)
                        cache.keep(key, M._document_facts(document), mentioned)
                        report["extracted"] += 1
                    except Exception:  # noqa: BLE001 - the step derives it again
                        report["failed_documents"] += 1
                        whole = False
                if group is not None and whole:
                    cache.keep_group(group, keys)
                if log is not None and report["groups"] % 50_000 == 0:
                    print(f"prefill: {json.dumps(report, sort_keys=True)} "
                          f"{round(time.monotonic() - started)} s", file=log, flush=True)
            if pool is not None:
                report["pool"] = {"tasks": pool.tasks, "fallback": pool.fallback,
                                  "broken": pool.broken}
    finally:
        cache.close(prune=False)
    report["seconds"] = round(time.monotonic() - started, 3)
    # a mail group's key holds repr(frozenset(internal domains)): its order, so the key,
    # follows the string hash seed; another process's step finds the groups kept here only
    # under the same fixed seed (PYTHONHASHSEED; fast_build.sh sets 0)
    report["hash_seed_fixed"] = not sys.flags.hash_randomization
    return report


@contextlib.contextmanager
def step_sources(from_ref: str, state: Path) -> Iterator[tuple[Any, Any]]:
    """(the ``mentions`` step's capture sources, the new schema) as the step migration
    opens them (``step_migrate.build_from_state``), read-only, with the kept readings."""
    from integrations.captured_text import kept_readings
    from integrations.kept_members import kept_members
    from ontology import step_migrate as sm
    from ontology.delta import _internal_domains
    from ontology.project_incremental import MEMBERS, READINGS
    from ontology.schema import load_schema
    from operating.host import state_captures

    the_plan = sm.plan(from_ref)
    run = the_plan["steps"]["run"]
    new = load_schema()
    projection = state / "projection"
    with contextlib.ExitStack() as stack:
        sources = state_captures(state, stack, internal_domains=_internal_domains(os.environ))
        sm.open_snapshots(sources)
        blobs = [s.blobs for s in sources.values()
                 if isinstance(getattr(s, "blobs", None), (str, Path))]
        stack.enter_context(kept_readings(projection / READINGS))
        stack.enter_context(kept_members(projection / MEMBERS, blobs))
        if "mentions" not in run:
            yield {}, new
            return
        yield sm._sources_of(new, sources, run["mentions"]), new


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m ontology.mention_cache")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prefill", help="extract every document's mentions into the cache")
    p.add_argument("--from-ref", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--cache", default=os.environ.get(ENV))
    p.add_argument("--workers", type=int, default=None)
    s = sub.add_parser("stats", help="entries held under this extractor code")
    s.add_argument("--cache", default=os.environ.get(ENV))
    args = parser.parse_args(argv)
    if not args.cache:
        parser.error(f"--cache (or {ENV}) is required")
    if args.command == "stats":
        cache = ExtractionCache(args.cache)
        try:
            out = {"xcode": cache.xcode, "held": cache._db.execute(
                "SELECT count(*) FROM extracted WHERE xcode = ?", (cache.xcode,)).fetchone()[0],
                "other": cache._db.execute("SELECT count(*) FROM extracted WHERE xcode <> ?",
                                           (cache.xcode,)).fetchone()[0],
                "groups": cache._db.execute("SELECT count(*) FROM groups").fetchone()[0]}
        finally:
            cache.close()
        print(json.dumps(out, sort_keys=True))
        return 0
    with step_sources(args.from_ref, Path(args.state_dir)) as (sources, new):
        report = prefill(sources, new, args.cache, workers=args.workers, log=sys.stderr)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
