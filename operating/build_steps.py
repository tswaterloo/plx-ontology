"""The one list of ``post_project`` steps every Workbench projection build runs.

The host (``operating.host.build_projection``) and every check that builds a projection
(``operating.sharepoint_host_check``, ``scripts/eval_unfamiliar_sample.py``) build with
:func:`post_project`, so what a check proves about the projection is what the host
serves. The steps, each writing into the generation being built
(``ontology.project.finish_generation`` runs them in name order):

* ``confirmations``: confirmed document kinds and Portal metadata onto their SharePoint
  files (``operating.kind_confirmation.projection_step``; the document kinds the review
  store held before are imported into the confirmation store first);
* ``document_fields``: the references of document-field objects as ``lead`` links
  (``SupplierConfirmation.confirms`` ..., ``ontology.document_fields.store_document_links``);
* ``mentions``: identifier mentions of captured documents as ``lead`` links
  (``ontology.mentions.store_mention_links``, its corroboration by the build's workers:
  ``ontology.finish_parallel``);
* ``reviews``: a person's accept / reject link decisions as ``reviewed`` /
  ``contradicted`` links (``ontology.reviews.link_step``);
* ``sender_corroboration``: a document-field link (``SupplierConfirmation.confirms``) whose
  number the document or its carrying message names, sent from a distinctive domain of
  the target's own supplier, as a ``corroborated`` link beside the lead, and the carrying
  message's own subject/body number as a ``lead`` (``ontology.sender_corroboration``, its
  reads made by the build's workers: ``ontology.sender_parallel``; after ``mentions`` and
  ``reviews``, so a rejected link is never raised);
* ``text_index``: the words of captured document text ``plx.find`` searches
  (``ontology.text_index.store_text_index``, its documents read by the build's workers:
  ``ontology.text_parallel``).
* ``twins``: a Portal-BC purchase-order pair the schema joins only on the number text,
  corroborated by its supplier and its date or total, as a ``corroborated`` same-record
  link beside the lead (``ontology.identity_scoring.twin_step``; after ``reviews``, so a
  pair a person rejected is never raised);
* ``your_reference``: a BC purchase order's ``Your Reference`` (ODataV4 Page 50, the
  captured field page ``purchaseOrderReferences``) naming a Portal order's FM PO number,
  as a ``corroborated`` same-record link when the match is unique and the supplier or
  order date corroborates it, else as a ``lead``
  (``ontology.identity_scoring.your_reference_verdict``)
  (``ontology.identity_scoring.your_reference_step``; after ``twins`` and ``reviews``, so a
  rejected, firm or already corroborated pair is never linked again).

``mentions`` and ``document_fields`` resolve a mention through the generation's identifier
index and the reviewed aliases of the review store (``ontology.reviews.BuildAliases``):
a supplier number a person confirmed as a PO's alias links a later document naming it to
that PO. The review store (``<DIR>/reviews.sqlite``, the one ``plx.review`` writes and
``plx.find`` reads) and the confirmation store are build inputs read by each build as they
are then: their links and values are written again by every build, an incremental one
with no capture change included, so a review recorded since the last build is in the next.

A build never creates or writes the review store: it reads ``<DIR>/reviews.sqlite``
read-only when it is there (:class:`ReviewsIfPresent`; an older format through an in-memory
copy) and builds without reviews when it is not, so a check or an eval building over a
directory leaves no review store behind and never upgrades one. Only a missing file means no
reviews: a store that is there but cannot be read (locked past the retries, a hot journal,
not a review store) fails the build with :class:`ReviewStoreUnreadable`, since a generation
built without its decisions would serve a link a person rejected as a lead and drop the
reviewed aliases and allocations. The host keeps the previous generation served and reports
the cycle ``failed`` with that reason, the store's code and path (``operating.host``); a check
or an eval stops. The MCP host reads the store the same way, except that an unreadable store
reads there as no reviews, counted per thread (:meth:`ReviewsIfPresent.unreadable_reads`), so
each answer read without them says so with a ``review_store_unreadable`` gap
(``operating.ontology_host.CaseHostTools.call``), and a store created or repaired after it
started (the console's ``plx.review``) is read from the next call on, without a restart.
"""

from __future__ import annotations

from contextlib import closing
import dataclasses
from pathlib import Path
import functools
import re
import sqlite3
import threading
from typing import Any, Callable

from ontology import reviews
from ontology.identity_scoring import twin_step, your_reference_step
from ontology.document_fields import store_document_links
from ontology.finish_parallel import store_mention_links
from ontology.mentions import Document
from ontology.sender_parallel import sender_corroboration_step
from ontology.text_parallel import store_text_index

from . import kind_confirmation

Step = Callable[[Any, Any], dict[str, Any]]


# A review file that cannot be read as a review store this many times in a row is
# unreadable (never an unbounded retry); each attempt waits up to ``_BUSY_TIMEOUT`` seconds
# for a writer's lock.
_READ_ATTEMPTS = 2
_BUSY_TIMEOUT = 30
UNREADABLE = "review_store_unreadable"


class ReviewStoreUnreadable(RuntimeError):
    """The review store is there but could not be read as one; ``code`` is
    ``review_store_unreadable``, ``reason`` the last read error."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{UNREADABLE}: {path.name}: {reason}")
        self.code, self.path, self.reason = UNREADABLE, path, reason


class ReviewsIfPresent(reviews.ReviewStore):
    """The review store at ``path`` as it is at each read, never created and never written:
    every read opens the file read-only (``mode=ro``). A store in an older format is read
    through an in-memory copy brought to the current format (only the writer's own open,
    ``ReviewStore``, upgrades the file). No file reads as an empty store. A path that is
    there but cannot be read as a review store (locked past the busy timeout, a hot journal
    a read-only open cannot roll back, not a database, no review table; tried
    ``_READ_ATTEMPTS`` times) raises :class:`ReviewStoreUnreadable` when ``strict`` (every
    build: its decisions are never replaced by none) and otherwise reads as empty and is
    counted for the reading thread (the MCP readers; :meth:`unreadable_reads`). A store
    created or repaired later is read from then on.

    Deviation from the writer: a valid store the process may not write (a read-only file
    or directory) is read as it is, its reviews included, where ``ReviewStore`` could not
    open it (its open migrates the file) and ``plx.review`` could not record into it.
    ``record`` is refused."""

    def __init__(self, path: str | Path, *, strict: bool = False) -> None:
        self.path = Path(path)
        self.strict = strict
        self._unreadable = threading.local()

    def unreadable_reads(self) -> tuple[int, ReviewStoreUnreadable | None]:
        """How many reads of this thread found the store unreadable and read it as empty,
        and the last one's error: a count that grew over a call means its answer lacks the
        store's reviews."""
        return (getattr(self._unreadable, "count", 0),
                getattr(self._unreadable, "last", None))

    def _db(self) -> sqlite3.Connection:
        failed = None
        for _ in range(_READ_ATTEMPTS):
            if not self.path.exists():
                failed = None
                break
            try:
                db = self._read_only()
            except sqlite3.DatabaseError as error:   # OperationalError (locked, unreadable)
                failed = error
                continue
            db.row_factory = sqlite3.Row
            return db
        if failed is not None:
            error = ReviewStoreUnreadable(self.path, str(failed)[:200])
            if self.strict:
                raise error from failed
            self._unreadable.count = self.unreadable_reads()[0] + 1
            self._unreadable.last = error
        db = _current(sqlite3.connect(":memory:"))
        db.row_factory = sqlite3.Row
        return db

    def _read_only(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
                             timeout=_BUSY_TIMEOUT)
        try:
            with closing(db.execute("PRAGMA table_info(reviews)")) as columns:
                have = {row[1] for row in columns}
            if "quantity" in have:
                return db
            if not have:
                raise sqlite3.DatabaseError("review_table_missing")
            copy = sqlite3.connect(":memory:")
            db.backup(copy)
        except BaseException:
            db.close()
            raise
        db.close()
        try:
            return _current(copy)
        except BaseException:
            copy.close()
            raise

    def record(self, **_: Any) -> tuple[dict[str, Any], bool]:
        raise reviews.ReviewError("review_store_read_only")


def _current(db: sqlite3.Connection) -> sqlite3.Connection:
    """``db`` (in memory) brought to the current review store format, as the writer's open
    brings a file."""
    with db:
        reviews._migrate(db)
        for statement in reviews._DDL:
            db.execute(statement)
    return db


# A lone UTF-16 surrogate (a half emoji cut from a captured text, or a byte the source
# could not decode) cannot be encoded as UTF-8, so one such character in one document
# failed a whole generation's ``mentions`` step. The document steps read each document
# with every lone surrogate as U+FFFD; a document without one is the same object.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _clean_text(value: Any) -> Any:
    if isinstance(value, str) and _LONE_SURROGATE.search(value):
        return _LONE_SURROGATE.sub("\ufffd", value)
    return value


def clean_document(document: Any) -> Any:
    """``document`` with every lone surrogate of its texts and copy key as U+FFFD (the
    document itself when it holds none)."""
    if not isinstance(document, Document):
        return document
    fields = tuple(f if _clean_text(f.text) is f.text
                   else dataclasses.replace(f, text=_clean_text(f.text))
                   for f in document.fields)
    key = (None if document.copy_key is None
           else tuple(_clean_text(part) for part in document.copy_key))
    if (all(a is b for a, b in zip(fields, document.fields))
            and (key is None or all(a is b for a, b in zip(key, document.copy_key)))):
        return document
    return dataclasses.replace(document, fields=fields, copy_key=key)


class _CleanSource:
    """A capture source whose ``document_groups()`` and ``documents()`` give cleaned
    documents (``clean_document``); everything else is the source's own."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._source, name)
        if name == "document_groups" and callable(attr):
            def groups(*args: Any, **kwargs: Any) -> Any:
                for key, load in attr(*args, **kwargs):
                    yield key, functools.partial(_load_clean, load)
            return groups
        if name == "documents" and callable(attr):
            def documents(*args: Any, **kwargs: Any) -> Any:
                return (clean_document(d) for d in attr(*args, **kwargs))
            return documents
        return attr


def _load_clean(load: Callable[[], Any]) -> Any:
    documents, remembered = load()
    return [clean_document(d) for d in documents], remembered


class _CleanProjector:
    """``projector`` as the document steps see it: its sources' documents cleaned."""

    def __init__(self, projector: Any) -> None:
        self._projector = projector
        self.sources = {name: _CleanSource(source) for name, source
                        in (getattr(projector, "sources", None) or {}).items()}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._projector, name)


def _documents_cleaned(step: Step) -> Step:
    @functools.wraps(step)
    def run(builder: Any, projector: Any) -> dict[str, Any]:
        return step(builder, _CleanProjector(projector))
    return run


def post_project(state_dir: str | Path, tenant: str, *, scratch: str | Path | None = None
                 ) -> dict[str, Step]:
    """The ``post_project`` steps of a build over the Workbench state directory
    ``state_dir`` (its review and confirmation stores) for ``tenant``; the review store
    is read if present, never created, and a store that is there but cannot be read fails
    the step reading it first with :class:`ReviewStoreUnreadable` (``strict``).

    ``scratch`` (a directory): a read-only build (the case workspace's projection). The
    ``confirmations`` step is the same step over a copy of the confirmation store made in
    ``scratch`` and removed after it, so the review store's document kinds are imported
    into the copy and the build projects what the host's next build would, while the
    state directory's confirmation store is only read (and never created)."""
    review_store = ReviewsIfPresent(reviews.store_path(state_dir), strict=True)
    store = kind_confirmation.store_path(state_dir)
    confirmations = (kind_confirmation.projection_step(store, tenant, reviews=review_store)
                     if scratch is None else _on_copy(store, Path(scratch), tenant, review_store))

    def mentions(builder: Any, projector: Any) -> dict[str, Any]:
        return store_mention_links(builder, projector,
                                   aliases=reviews.BuildAliases(review_store, tenant))

    def document_fields(builder: Any, projector: Any) -> dict[str, Any]:
        return store_document_links(builder, projector,
                                    aliases=reviews.BuildAliases(review_store, tenant))

    return {name: _named(name, step) for name, step in {
        "confirmations": confirmations,
        "document_fields": document_fields, "mentions": _documents_cleaned(mentions),
        "reviews": reviews.link_step(review_store, tenant),
        "sender_corroboration": sender_corroboration_step,
        "text_index": _documents_cleaned(_with_chunk_families(store_text_index)),
        "twins": twin_step,
        "your_reference": your_reference_step}.items()}


def _with_chunk_families(step: Step) -> Step:
    """The ``text_index`` step, then the chunk id runs of each audience family it left
    (``ontology.chunk_families``: what a documents search of a few sources reads), in its
    report as ``chunk_family_runs`` (None: left empty, nothing narrowed)."""
    from ontology.chunk_families import store_chunk_families

    @functools.wraps(step)
    def run(builder: Any, projector: Any) -> dict[str, Any]:
        report = step(builder, projector)
        return {**report, "chunk_family_runs": store_chunk_families(builder.connection)}
    return run


def _named(name: str, step: Step) -> Step:
    """``step``, naming itself on an exception it raises (``build_step``: the step's
    name, never its data), so a failed build says where (``operating.host``'s reason)."""
    @functools.wraps(step)
    def run(builder: Any, projector: Any) -> dict[str, Any]:
        try:
            return step(builder, projector)
        except Exception as exc:
            if getattr(exc, "build_step", None) is None:
                try:
                    exc.build_step = name   # type: ignore[attr-defined]
                except (AttributeError, TypeError):
                    pass
            raise
    return run


def _on_copy(store: Path, scratch: Path, tenant: str, review_store: Any) -> Step:
    """The ``confirmations`` step over a copy of the confirmation store ``store`` (read
    read-only; none when it is not there) in a temporary directory under ``scratch``."""
    import tempfile

    def step(builder: Any, projector: Any) -> dict[str, Any]:
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".confirmations-", dir=scratch) as temp:
            copy = Path(temp) / store.name
            if store.is_file():
                with closing(sqlite3.connect(store.resolve().as_uri() + "?mode=ro", uri=True,
                                             timeout=_BUSY_TIMEOUT)) as source, \
                        closing(sqlite3.connect(copy)) as target:
                    source.backup(target)
            return kind_confirmation.projection_step(copy, tenant, reviews=review_store)(
                builder, projector)
    return step


__all__ = ["ReviewStoreUnreadable", "ReviewsIfPresent", "Step", "post_project"]
