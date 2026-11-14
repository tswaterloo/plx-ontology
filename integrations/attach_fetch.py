"""The attachment fast path: deferred mail attachments fetched and read beside ingest.

The mail capture deferred ~220k attachments while its OCR backlog was full
(``capture_mail.ATTACHMENT_DEFERRED``); reading them inside ingest's mail child runs at
1-2 a second. This module does the same work as three decoupled steps, each the one
writer of its own file, so capture is never paused and the mail store keeps one writer:

* ``plan``: the work list, read from the mail store opened read-only
  (``MailCaptureStore.open_read_only``): every deferred attachment of a current message
  version, business types first (PDF, then office files, then others, images last), the
  first copy of a name and size not already captured before its repeats, newest first.
  Written to this store's ``fetch.sqlite``.
* ``download``: bytes only, no extraction. Guarded Graph reads (``GraphMailReader`` gate:
  directory lookup, audience, owner ACL per mailbox; the people exclusion prepared and
  fail-closed, a withheld mailbox never requested), many mailboxes in parallel and at most
  ``per_mailbox`` requests in flight on each (Exchange throttles per mailbox), with JSON
  ``$batch`` of GETs only (``SourceApi.get_batch``; ``live_source_guard.check_batch_body``
  refuses any other inner request). Per chunk of a mailbox: one batch of the messages'
  exclusion fields first (sender, recipients, folder, change key), decided like
  ``GraphMailReader.get_message`` (a message an excluded person sent or received is
  ``excluded`` and its attachments never requested; one the guard cannot decide is left;
  one whose version moved on is ``changed_since``), then batches of the attachments of
  the messages that passed. A 429 backs that mailbox off for its ``Retry-After``. Bytes
  are content-addressed files (``blobs/<aa>/<sha256>``), so a file forwarded many times is
  stored once.
* ``read``: a process pool over the fetched bytes, one reading per distinct (bytes, type,
  name): the capture's own extractor (``capture_mail.extract_attachment``: a PDF's text
  layer first, OCR only of pages without one; images OCR-read only at or above
  ``min_image_bytes`` and the capture's size rule). Results go to ``readings.sqlite`` (this
  step its one writer); no credential reaches it (scrubbed environment). It follows the
  downloads as they land (``--follow``), so neither step waits for the other.

Ingest applies the results between capture runs (``capture_mail.apply_fetched_pass``, the
mail child's ``--apply-fetched-dir``), through the capture's own write path, re-checking
that each row is still deferred on a current message version of a mailbox not withheld.

Counts only on stdout, never names, ids or text. Live-unverified until
live check ``mail.attach_fetch`` passes; tests use an injected transport.
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.core import Actor

SCHEMA = "plx-attach-fetch/v1"
FETCH_DB = "fetch.sqlite"
READINGS_DB = "readings.sqlite"
BLOB_DIR = "blobs"
# Attachments up to this size go into a $batch (its whole response stays under the
# guard's 28 MB bound: at most BATCH_RAW_BYTES of raw bytes, base64 +33%); a larger one is
# one GET of its own.
BATCH_ITEM_MAX_BYTES = 4_000_000
BATCH_RAW_BYTES = 16_000_000
BATCH_SIZE = 20
MAX_TRIES = 3
MAX_REQUEUES = 20   # throttled or transient answers of one row within one run
MIN_IMAGE_BYTES = 20_000
DEFAULT_MIN_FREE_BYTES = 30 << 30
MESSAGE_SELECT = ("id,changeKey,parentFolderId,internetMessageId,from,sender,"
                  "toRecipients,ccRecipients,bccRecipients")

# Work row states. ``todo`` waits; ``fetched`` has bytes; ``filled`` a terminal content
# status (``content_status``: not a file attachment, size mismatch, over the cap);
# ``excluded`` the people exclusion refused its message; ``not_visible``, ``changed_since``
# ``left`` and ``failed`` stay deferred in the mail store (its own pass reads them).
TODO, FETCHED, FILLED, EXCLUDED = "todo", "fetched", "filled", "excluded"
NOT_VISIBLE, CHANGED, LEFT, FAILED = "not_visible", "changed_since", "left", "failed"
APPLIABLE = (FETCHED, FILLED, EXCLUDED)

_IMMUTABLE = {"Prefer": 'IdType="ImmutableId"'}
_PDF = (".pdf",)
_OFFICE = (".xlsx", ".xls", ".xlsm", ".csv", ".docx", ".doc", ".pptx", ".msg", ".eml",
           ".zip", ".txt", ".rtf")
_IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic")


def type_rank(name: Any, content_type: Any) -> int:
    """Business types first: 0 PDF, 1 office files and mail/containers, 2 other, 3 images."""
    lower = name.lower() if isinstance(name, str) else ""
    kind = content_type.lower() if isinstance(content_type, str) else ""
    if lower.endswith(_PDF) or kind == "application/pdf":
        return 0
    if lower.endswith(_OFFICE):
        return 1
    if lower.endswith(_IMAGE) or kind.startswith("image/"):
        return 3
    return 2


def read_key(digest: str, metadata: Mapping[str, Any]) -> str:
    """One reading per (bytes, MIME type, name): the extractor's readings are keyed by
    type and name, so two names of the same bytes are read once each."""
    return sha256(json.dumps([digest, metadata.get("content_type"), metadata.get("name")],
                             ensure_ascii=False).encode("utf-8")).hexdigest()


def blob_file(root: Path, digest: str) -> Path:
    return root / BLOB_DIR / digest[:2] / digest


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30,
                             check_same_thread=False)
    else:
        db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    return db


class FetchStore:
    """``<root>/fetch.sqlite`` (the work list and what each row came to, written by
    ``plan`` and ``download``), ``<root>/blobs`` (bytes by SHA-256) and
    ``<root>/readings.sqlite`` (written by ``read`` only). Never the mail store."""

    def __init__(self, root: str | Path, *, read_only: bool = False) -> None:
        self.root = Path(root).expanduser().resolve()
        self.lock = threading.Lock()
        if read_only:
            if not (self.root / FETCH_DB).is_file():
                raise FileNotFoundError("fetch_store_missing")
            self.db = _connect(self.root / FETCH_DB, read_only=True)
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / BLOB_DIR).mkdir(exist_ok=True, mode=0o700)
        self.db = _connect(self.root / FETCH_DB)
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS work (
                    message_handle TEXT NOT NULL, attachment_id TEXT NOT NULL,
                    mailbox_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    version_id INTEGER NOT NULL, metadata TEXT NOT NULL,
                    size INTEGER, priority INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'todo', content_status TEXT,
                    content_sha256 TEXT, read_meta TEXT, read_key TEXT,
                    tries INTEGER NOT NULL DEFAULT 0, done_at REAL,
                    PRIMARY KEY (message_handle, attachment_id));
                CREATE INDEX IF NOT EXISTS work_status ON work (status, priority,
                                                               version_id DESC);
                CREATE INDEX IF NOT EXISTS work_key ON work (read_key);
            """)
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (SCHEMA,))

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ plan
    def plan(self, mail: Any) -> dict[str, int]:
        """Add every deferred attachment of a current message version of ``mail`` (a
        ``MailCaptureStore``, read-only) not yet listed; rows already listed keep their
        state. Counts only."""
        from .capture_mail import ATTACHMENT_DEFERRED
        captured: set[tuple[str, int]] = set()
        for name, size in mail.db.execute(
                "SELECT lower(json_extract(metadata,'$.name')), json_extract(metadata,"
                "'$.size') FROM attachments WHERE content_status='captured'"):
            if isinstance(name, str) and isinstance(size, int):
                captured.add((name, size))
        rows = mail.db.execute(
            "SELECT a.message_handle, a.attachment_id, a.mailbox_id, m.message_id, "
            "m.version_id, a.metadata FROM attachments a JOIN messages m ON "
            "m.l1_handle=a.message_handle AND m.superseded_at IS NULL WHERE "
            "a.content_status=? ORDER BY m.version_id DESC, a.attachment_id",
            (ATTACHMENT_DEFERRED,)).fetchall()
        seen: set[tuple[str, int]] = set()
        out = []
        unique = 0
        for row in rows:
            try:
                meta = json.loads(row["metadata"])
            except (TypeError, ValueError):
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            name, size = meta.get("name"), meta.get("size")
            key = (name.lower() if isinstance(name, str) else "",
                   size if isinstance(size, int) else -1)
            first = key not in seen and key not in captured
            seen.add(key)
            unique += first
            out.append((row["message_handle"], row["attachment_id"], row["mailbox_id"],
                        row["message_id"], row["version_id"],
                        json.dumps(meta, sort_keys=True),
                        size if isinstance(size, int) else None,
                        type_rank(name, meta.get("content_type")) + (0 if first else 10)))
        with self.lock, self.db:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO work (message_handle, attachment_id, mailbox_id, "
                "message_id, version_id, metadata, size, priority) VALUES (?,?,?,?,?,?,?,?)",
                out)
            added = self.db.total_changes - before
        return {"deferred": len(rows), "unique_new": unique, "added": added}

    # ------------------------------------------------------------------ rows
    def todo(self, limit: int | None = None) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM work WHERE status IN ('todo','left') AND tries < ? "
            "ORDER BY priority, version_id DESC" + ("" if limit is None else " LIMIT ?"),
            (MAX_TRIES, *(() if limit is None else (limit,)))).fetchall()

    def put_bytes(self, content: bytes) -> str:
        digest = sha256(content).hexdigest()
        target = blob_file(self.root, digest)
        if not target.exists():
            target.parent.mkdir(exist_ok=True, mode=0o700)
            temporary = target.with_name(f".{digest}.{os.getpid()}.{threading.get_ident()}")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.replace(temporary, target)
        return digest

    def bytes_of(self, digest: str) -> bytes:
        content = blob_file(self.root, digest).read_bytes()
        if sha256(content).hexdigest() != digest:
            raise ValueError("fetched_digest_mismatch")
        return content

    def settle(self, rows: Iterable[tuple[sqlite3.Row, Mapping[str, Any]]]) -> None:
        """Record what each row came to (``status`` and, as it applies, ``content_status``,
        ``content_sha256``, ``read_meta``); a ``left`` row counts one more try."""
        now = time.time()
        with self.lock, self.db:
            for row, done in rows:
                meta = done.get("read_meta")
                self.db.execute(
                    "UPDATE work SET status=?, content_status=?, content_sha256=?, "
                    "read_meta=?, read_key=?, tries=tries+?, done_at=? WHERE "
                    "message_handle=? AND attachment_id=?",
                    (done["status"], done.get("content_status"), done.get("content_sha256"),
                     None if meta is None else json.dumps(meta, sort_keys=True),
                     None if meta is None else read_key(done["content_sha256"], meta),
                     1 if done["status"] in (LEFT, FAILED) else 0, now,
                     row["message_handle"], row["attachment_id"]))

    def counts(self) -> dict[str, Any]:
        found = {status: n for status, n in self.db.execute(
            "SELECT status, COUNT(*) FROM work GROUP BY status")}
        fetched_bytes = self.db.execute(
            "SELECT COALESCE(SUM(json_extract(read_meta,'$.bytes')),0) FROM work "
            "WHERE status='fetched'").fetchone()[0]
        return {"rows": sum(found.values()), "by_status": found, "fetched_bytes": fetched_bytes}


# ---------------------------------------------------------------------- download

class _Scheduler:
    """Per-mailbox queues of chunks (one chunk: <= BATCH_SIZE attachments of whole
    messages), handed out best priority first to a mailbox under its in-flight cap and
    out of its back-off."""

    def __init__(self, rows: Sequence[sqlite3.Row], per_mailbox: int,
                 clock: Callable[[], float] = time.monotonic,
                 deadline: float = float("inf")) -> None:
        self.per_mailbox, self.clock, self.deadline = per_mailbox, clock, deadline
        self.queues: dict[str, deque[list[sqlite3.Row]]] = {}
        by_box: dict[str, list[sqlite3.Row]] = {}
        for row in rows:   # already in priority order
            by_box.setdefault(row["mailbox_id"], []).append(row)
        for box, items in by_box.items():
            chunks: list[list[sqlite3.Row]] = []
            current: list[sqlite3.Row] = []
            index: dict[str, list[sqlite3.Row]] = {}
            for row in items:   # a message's attachments travel together
                index.setdefault(row["message_id"], []).append(row)
            done: set[str] = set()
            for row in items:
                if row["message_id"] in done:
                    continue
                done.add(row["message_id"])
                group = index[row["message_id"]]
                if current and len(current) + len(group) > BATCH_SIZE:
                    chunks.append(current)
                    current = []
                current.extend(group)
            if current:
                chunks.append(current)
            self.queues[box] = deque(chunks)
        self.inflight: dict[str, int] = {box: 0 for box in self.queues}
        self.until: dict[str, float] = {}
        self.cond = threading.Condition()
        self.stopped = False

    def take(self) -> tuple[str, list[sqlite3.Row]] | None:
        with self.cond:
            while True:
                if self.stopped or self.clock() >= self.deadline:
                    return None
                now, best, wait_for = self.clock(), None, None
                for box, queue in self.queues.items():
                    if not queue or self.inflight[box] >= self.per_mailbox:
                        continue
                    until = self.until.get(box, 0.0)
                    if until > now:
                        wait_for = min(wait_for or until - now, until - now)
                        continue
                    rank = (queue[0][0]["priority"], -queue[0][0]["version_id"])
                    if best is None or rank < best[0]:
                        best = (rank, box)
                if best is not None:
                    box = best[1]
                    self.inflight[box] += 1
                    return box, self.queues[box].popleft()
                if not any(self.queues.values()) and not any(self.inflight.values()):
                    return None
                self.cond.wait(timeout=min(wait_for if wait_for is not None else 1.0, 5.0))

    def finish(self, box: str, again: Sequence[sqlite3.Row] = (),
               back_off: float | None = None) -> None:
        with self.cond:
            self.inflight[box] -= 1
            if again:
                self.queues[box].appendleft(list(again))
            if back_off:
                self.until[box] = max(self.until.get(box, 0.0), self.clock() + back_off)
            self.cond.notify_all()

    def drop(self, box: str) -> list[sqlite3.Row]:
        """Every queued row of a mailbox (it is not readable): the caller settles them."""
        with self.cond:
            rows = [row for chunk in self.queues[box] for row in chunk]
            self.queues[box].clear()
            self.cond.notify_all()
            return rows

    def stop(self) -> None:
        with self.cond:
            self.stopped = True
            self.cond.notify_all()


def _retry_after(headers: Mapping[str, str], default: float = 10.0) -> float:
    try:
        return max(1.0, min(float(headers.get("retry-after", default)), 300.0))
    except (TypeError, ValueError):
        return default


class _GuardLost(Exception):
    """The people guard stopped deciding mid-run: the run stops, its rows wait."""


class Downloader:
    """Bytes only: the guarded reads of the module docstring, ``threads`` in all and
    ``per_mailbox`` on one mailbox at once."""

    def __init__(self, fetch: FetchStore, reader: Any, actor: Actor, *,
                 threads: int = 64, per_mailbox: int = 1, batch: bool = True,
                 batch_size: int = 4,
                 min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
                 free_bytes: Callable[[Path], int] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        from .graph_evidence import MAX_ATTACHMENT_BYTES
        if not 1 <= threads <= 256 or not 1 <= per_mailbox <= 8 \
                or not 2 <= batch_size <= 20:
            raise ValueError("threads 1..256, per_mailbox 1..8, batch_size 2..20")
        # Exchange runs at most 4 requests on one mailbox at once and a batch's inner
        # requests run in parallel: per_mailbox x batch_size is what one mailbox sees.
        self.batch_size = batch_size
        self.fetch, self.reader, self.actor = fetch, reader, actor
        self.api = reader._api
        self.threads, self.per_mailbox, self.batch = threads, per_mailbox, batch
        self.max_bytes = MAX_ATTACHMENT_BYTES
        self.min_free = min_free_bytes
        self.free = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.clock = clock
        self.boxes: dict[str, Any] = {}
        self.folders: dict[str, dict[str, str | None]] = {}
        self.box_lock = threading.Lock()
        self.stats: dict[str, Any] = {"requests": 0, "batches": 0, "throttled": 0,
                                      "bytes": 0, **{s: 0 for s in (
                                          FETCHED, FILLED, EXCLUDED, NOT_VISIBLE, CHANGED,
                                          LEFT, FAILED)}}
        self.stats_lock = threading.Lock()

    def _count(self, key: str, n: int = 1) -> None:
        with self.stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def _box(self, mailbox: str) -> Any:
        with self.box_lock:
            if mailbox in self.boxes:
                return self.boxes[mailbox]
        try:
            box = self.reader._gate(self.actor, mailbox)
        except Exception:  # noqa: BLE001 - NotVisible and any refusal: that mailbox is not read
            box = None
        with self.box_lock:
            self.boxes[mailbox] = box
            self.folders.setdefault(mailbox, {})
        return box

    # one GET or a $batch of them: (id -> (status, headers, body))
    def _gets(self, requests: list[tuple[str, str]]) -> dict[str, tuple[int, Mapping[str, str], Any]]:
        from .live_source_guard import SourceNotFound, SourceUnavailable, is_transport_failure
        self._count("requests", len(requests))
        found: dict[str, tuple[int, Mapping[str, str], Any]] = {}
        single = requests
        if self.batch and len(requests) > 1:
            single = []
            for start in range(0, len(requests), self.batch_size):
                part = requests[start:start + self.batch_size]
                if len(part) == 1:
                    single += part
                    continue
                self._count("batches")
                try:
                    found.update(self.api.get_batch([(i, url, _IMMUTABLE) for i, url in part]))
                except SourceUnavailable as exc:
                    if getattr(exc, "http_status", None) is not None or is_transport_failure(exc):
                        raise
                    single += part   # e.g. a response over the bound: one GET each
        for ident, url in single:
            try:
                bound = ((self.max_bytes * 4) // 3 + 64_000 if "/attachments/" in url
                         else 4_000_000)
                found[ident] = (200, {}, self.api.get_json(url, headers=_IMMUTABLE,
                                                           max_bytes=bound))
            except SourceNotFound:
                found[ident] = (404, {}, None)
            except SourceUnavailable as exc:
                status = getattr(exc, "http_status", None)
                retry = getattr(exc, "retry_after_seconds", None)
                if not isinstance(status, int):   # no answer: transient, or over the bound
                    status = 503 if is_transport_failure(exc) else (
                        413 if "exceeds" in str(exc) else 0)
                found[ident] = (status, {"retry-after": str(retry)} if retry else {}, None)
        return found

    def run_chunk(self, mailbox: str, rows: list[sqlite3.Row]
                  ) -> tuple[list[tuple[sqlite3.Row, dict[str, Any]]], list[sqlite3.Row], float | None]:
        """(settled rows, rows to queue again, back-off seconds) of one chunk."""
        from . import people_exclusion
        from .graph_evidence import decode_file_attachment
        from .graph_read import mail_citation
        from . import live_source_grammar as grammar
        from .live_source_guard import SourceUnavailable

        box = self._box(mailbox)
        if box is None:
            return [(row, {"status": NOT_VISIBLE}) for row in rows], [], None
        settled: list[tuple[sqlite3.Row, dict[str, Any]]] = []
        messages = list(dict.fromkeys(row["message_id"] for row in rows))
        ids = {f"m{i}": mid for i, mid in enumerate(messages)}
        try:
            answers = self._gets([(key, grammar.url("graph.message", {"$select": MESSAGE_SELECT},
                                                   user=box.object_id, message=mid))
                                  for key, mid in ids.items()])
        except SourceUnavailable as exc:
            status = getattr(exc, "http_status", None)
            if status in (None, 429, 502, 503, 504):   # throttled or transient: again
                if status == 429:
                    self._count("throttled")
                return [], list(rows), getattr(exc, "retry_after_seconds", None) or 10.0
            return [(row, {"status": LEFT}) for row in rows], [], None
        passed: set[str] = set()
        again: list[sqlite3.Row] = []
        back_off = None
        cache = self.folders[mailbox]
        for key, mid in ids.items():
            status, headers, body = answers[key]
            mine = [row for row in rows if row["message_id"] == mid]
            if status in (429, 502, 503, 504):
                again += mine
                if status == 429:
                    self._count("throttled")
                    back_off = max(back_off or 0, _retry_after(headers))
                continue
            if status == 404:
                settled += [(row, {"status": NOT_VISIBLE}) for row in mine]
                continue
            if status != 200 or not isinstance(body, Mapping) or body.get("id") != mid:
                settled += [(row, {"status": LEFT}) for row in mine]
                continue
            if not self.reader._belongs(box, body, cache):
                settled += [(row, {"status": NOT_VISIBLE}) for row in mine]
                continue
            try:
                party = self.reader._excluded_party(body)
            except people_exclusion.ExclusionUnavailable:
                # the guard itself went unready: nothing more is requested this run and
                # the chunk waits as it was
                raise _GuardLost() from None
            if party == "withheld":   # undecided now: left, read again on a later run
                settled += [(row, {"status": LEFT}) for row in mine]
                continue
            if party:
                settled += [(row, {"status": EXCLUDED}) for row in mine]
                continue
            change = body.get("changeKey")
            if not isinstance(change, str) or not change:
                settled += [(row, {"status": LEFT}) for row in mine]
                continue
            current = mail_citation(box.object_id, mid, change)
            for row in mine:
                if current != row["message_handle"]:
                    settled.append((row, {"status": CHANGED}))
                else:
                    passed.add(row["attachment_id"] + "\0" + mid)
        todo = [row for row in rows if row["attachment_id"] + "\0" + row["message_id"] in passed]
        # attachment GETs: small ones batched up to BATCH_RAW_BYTES, a large one alone
        groups: list[list[sqlite3.Row]] = []
        current_group: list[sqlite3.Row] = []
        raw = 0
        for row in todo:
            size = row["size"]
            if not isinstance(size, int) or size > BATCH_ITEM_MAX_BYTES:
                groups.append([row])
                continue
            if current_group and (raw + size > BATCH_RAW_BYTES
                                  or len(current_group) >= BATCH_SIZE):
                groups.append(current_group)
                current_group, raw = [], 0
            current_group.append(row)
            raw += size
        if current_group:
            groups.append(current_group)
        for group in groups:
            if self.free(self.fetch.root) < self.min_free:
                again += group
                continue
            keyed = {f"a{i}": row for i, row in enumerate(group)}
            try:
                answers = self._gets([(key, grammar.url(
                    "graph.message_attachment", None, user=box.object_id,
                    message=row["message_id"], attachment=row["attachment_id"]))
                    for key, row in keyed.items()])
            except SourceUnavailable as exc:
                status = getattr(exc, "http_status", None)
                if status == 429:
                    self._count("throttled")
                    back_off = max(back_off or 0, getattr(exc, "retry_after_seconds", None)
                                   or 10.0)
                again += group if status in (None, 429, 502, 503, 504) else []
                if status not in (None, 429, 502, 503, 504):
                    settled += [(row, {"status": LEFT}) for row in group]
                continue
            for key, row in keyed.items():
                status, headers, body = answers[key]
                if status in (429, 502, 503, 504):
                    again.append(row)
                    if status == 429:
                        self._count("throttled")
                        back_off = max(back_off or 0, _retry_after(headers))
                    continue
                if status == 404:
                    settled.append((row, {"status": NOT_VISIBLE}))
                    continue
                if status == 413:   # over the byte bound, as the capture records it
                    settled.append((row, {"status": FILLED, "content_status": "exceeds_cap"}))
                    continue
                if status != 200:
                    settled.append((row, {"status": LEFT}))
                    continue
                settled.append((row, self._decoded(row, body, decode_file_attachment,
                                                   SourceUnavailable)))
        return settled, again, back_off

    def _decoded(self, row: sqlite3.Row, body: Any, decode: Callable[..., Any],
                 unavailable: type) -> dict[str, Any]:
        from .capture_mail import MISMATCH_SIZE
        try:
            meta, content = decode(body, row["attachment_id"], max_bytes=self.max_bytes)
        except unavailable as exc:
            text = str(exc)
            if "not a file attachment" in text:
                return {"status": FILLED, "content_status": "not_file_attachment"}
            if "exceed its reported size" in text:
                return {"status": FILLED, "content_status": MISMATCH_SIZE}
            if "byte bound" in text:
                return {"status": FILLED, "content_status": "exceeds_cap"}
            return {"status": LEFT}
        listed, reported = json.loads(row["metadata"]).get("size"), meta.get("size")
        if listed is not None and reported is not None and listed != reported:
            return {"status": FILLED, "content_status": MISMATCH_SIZE}
        digest = self.fetch.put_bytes(content)
        if digest != meta["content_sha256"]:
            return {"status": LEFT}
        self._count("bytes", len(content))
        return {"status": FETCHED, "content_sha256": digest, "read_meta": dict(meta)}

    def run(self, rows: Sequence[sqlite3.Row], *, withheld: Iterable[str],
            budget_seconds: float, progress: Callable[[dict[str, Any]], None] | None = None,
            progress_seconds: float = 60.0) -> dict[str, Any]:
        held = {str(m).lower() for m in withheld}
        skipped = [row for row in rows if str(row["mailbox_id"]).lower() in held]
        rows = [row for row in rows if str(row["mailbox_id"]).lower() not in held]
        self.stats["withheld_skipped"] = len(skipped)   # never requested, left deferred
        started = self.clock()
        deadline = started + budget_seconds
        scheduler = _Scheduler(rows, self.per_mailbox, self.clock, deadline)
        errors: dict[str, int] = {}
        attempts: dict[tuple[str, str], int] = {}

        def worker() -> None:
            while True:
                if self.clock() >= deadline:
                    scheduler.stop()
                    return
                if self.free(self.fetch.root) < self.min_free:
                    self.stats["disk_low"] = 1
                    scheduler.stop()
                    return
                taken = scheduler.take()
                if taken is None:
                    return
                box, chunk = taken
                again, back_off = [], None
                try:
                    settled, again, back_off = self.run_chunk(box, chunk)
                except _GuardLost:
                    self.stats["people_guard_lost"] = 1
                    scheduler.stop()
                    scheduler.finish(box, chunk)
                    return
                except Exception as exc:  # noqa: BLE001 - counted; the rows stay todo
                    kind = type(exc).__name__
                    with self.stats_lock:
                        errors[kind] = errors.get(kind, 0) + 1
                    settled = [(row, {"status": LEFT}) for row in chunk]
                    back_off = 5.0
                kept = []
                for row in again:   # a row asked again too often is left for later
                    key = (row["message_handle"], row["attachment_id"])
                    with self.stats_lock:
                        attempts[key] = attempts.get(key, 0) + 1
                        over = attempts[key] > MAX_REQUEUES
                    if over:
                        settled.append((row, {"status": LEFT}))
                    else:
                        kept.append(row)
                again = kept
                if settled:
                    self.fetch.settle(settled)
                    for _row, done in settled:
                        self._count(done["status"])
                if self._box(box) is None:
                    gone = scheduler.drop(box)
                    if gone:
                        self.fetch.settle([(row, {"status": NOT_VISIBLE}) for row in gone])
                        self._count(NOT_VISIBLE, len(gone))
                scheduler.finish(box, again, back_off)

        pool = [threading.Thread(target=worker, name=f"attach-fetch-{i}", daemon=True)
                for i in range(self.threads)]
        for thread in pool:
            thread.start()
        last = self.clock()
        while any(thread.is_alive() for thread in pool):
            for thread in pool:
                thread.join(timeout=1.0)
            if progress is not None and self.clock() - last >= progress_seconds:
                last = self.clock()
                progress(self.report(started))
        if errors:
            self.stats["errors"] = errors
        return self.report(started)

    def report(self, started: float) -> dict[str, Any]:
        with self.stats_lock:
            out = dict(self.stats)
        seconds = max(self.clock() - started, 1e-9)
        out["seconds"] = round(seconds, 1)
        out["per_second"] = round(out[FETCHED] / seconds, 2)
        out["mb_per_second"] = round(out["bytes"] / seconds / 1e6, 2)
        return out


# ---------------------------------------------------------------------- read

class Readings:
    """``<root>/readings.sqlite``: one reading per read key (written by ``read`` only)."""

    def __init__(self, root: str | Path, *, read_only: bool = False) -> None:
        path = Path(root).expanduser().resolve() / READINGS_DB
        if read_only:
            if not path.is_file():
                raise FileNotFoundError("readings_missing")
            self.db = _connect(path, read_only=True)
            return
        self.db = _connect(path)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS readings (read_key TEXT PRIMARY KEY, "
                            "sha256 TEXT NOT NULL, text_status TEXT NOT NULL, text TEXT, "
                            "readings TEXT, seconds REAL, read_at REAL)")

        with self.db:   # why a reading is partial or was not made (NOTE_*), by read key
            self.db.execute("CREATE TABLE IF NOT EXISTS notes (read_key TEXT PRIMARY KEY, "
                            "note TEXT NOT NULL, detail TEXT)")

    def get(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM readings WHERE read_key=?", (key,)).fetchone()


STORED_DB = "stored.sqlite"
# Kinds of a stored item (``plan-stored``): an image the capture kept unread
# (``unsupported``) and a scanned PDF it left for OCR (``ocr_pending``).
STORED_KINDS = {"unsupported": "image", "ocr_pending": "scan"}


class StoredPlan:
    """``<root>/stored.sqlite``: the mail store's own unread images and scans (bytes
    already on disk) the ``read`` pool reads beside the downloads: one row per read key,
    the bytes read from the mail store opened read-only. Written by ``plan-stored`` only;
    ingest applies the readings (``capture_mail.apply_stored_pass``)."""

    def __init__(self, root: str | Path, *, read_only: bool = False) -> None:
        path = Path(root).expanduser().resolve() / STORED_DB
        if read_only:
            if not path.is_file():
                raise FileNotFoundError("stored_plan_missing")
            self.db = _connect(path, read_only=True)
            return
        self.db = _connect(path)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS stored (read_key TEXT PRIMARY KEY, "
                            "content_sha256 TEXT NOT NULL, kind TEXT NOT NULL, "
                            "read_meta TEXT NOT NULL, priority INTEGER NOT NULL, "
                            "planned_at REAL NOT NULL)")

    def plan(self, mail: Any, withheld: Iterable[str]) -> dict[str, int]:
        """List every distinct (bytes, type, name) of a current message version's captured
        attachment the capture left unread: an image (``unsupported``, an OCR image type,
        at or above the capture's size rule is decided at read time) or a paged file left
        for OCR (``ocr_pending``), never one of a ``withheld`` mailbox. Counts only."""
        from .captured_text import _ocr_image, is_paged_file
        held = {str(m).lower() for m in withheld}
        counts = {"seen": 0, "withheld": 0, "listed": 0, "images": 0, "scans": 0}
        rows = mail.db.execute(
            "SELECT a.content_sha256, a.metadata, a.text_status, a.mailbox_id FROM "
            "attachments a JOIN messages m ON m.l1_handle=a.message_handle AND "
            "m.superseded_at IS NULL WHERE a.content_status='captured' AND "
            "a.text_status IN ('unsupported','ocr_pending') AND a.content_sha256 IS NOT NULL")
        now, batch = time.time(), []
        for row in rows:
            counts["seen"] += 1
            if str(row["mailbox_id"]).lower() in held:
                counts["withheld"] += 1
                continue
            meta = json.loads(row["metadata"])
            name, kind = str(meta.get("name") or ""), str(meta.get("content_type") or "")
            what = STORED_KINDS[row["text_status"]]
            if what == "image" and not _ocr_image(name, kind):
                continue
            if what == "scan" and not is_paged_file({"mime_type": kind, "name": name}):
                continue
            batch.append((read_key(row["content_sha256"], meta), row["content_sha256"], what,
                          json.dumps(meta, sort_keys=True),
                          0 if what == "scan" else 3, now))
        with self.db:
            before = self.db.execute("SELECT COUNT(*) FROM stored").fetchone()[0]
            self.db.executemany("INSERT OR IGNORE INTO stored VALUES (?,?,?,?,?,?)", batch)
            counts["listed"] = self.db.execute("SELECT COUNT(*) FROM stored").fetchone()[0] \
                - before
        for kind, n in self.db.execute("SELECT kind, COUNT(*) FROM stored GROUP BY kind"):
            counts[kind + "s"] = n
        return counts


def image_ocr_rule(content: bytes, metadata: Mapping[str, Any], min_image_bytes: int) -> bool:
    """Whether an image is OCR-read: at or above ``min_image_bytes`` and the capture's
    own size rule (``captured_text.image_worth_reading``: pixels, not a logo or icon)."""
    from .captured_text import image_worth_reading
    return len(content) >= min_image_bytes and image_worth_reading(content, metadata)


# The text-likelihood prefilter (``read`` with ``prefilter``): an image the size rule
# passes is still not OCR-read when its look says it holds no text: a photo, a logo or a
# signature banner. Measured on a greyscale thumbnail of at most THUMB_SIDE pixels:
# the share of pixels on a sharp edge (text is dense in strokes) and the share of the
# most common brightness band (a page or screenshot has a background). See
# :func:`text_likely`. An image it skips keeps the capture's "not read" status
# (``unsupported``, as a logo the size rule leaves) and the fast path records why
# (``notes``: :data:`NOTE_NO_TEXT_LIKELY`); the capture's own image pass
# (``capture_mail.image_ocr_pass``, which reads ``unsupported`` images) can still read it.
NOTE_NO_TEXT_LIKELY = "skipped: no text likely"
NOTE_PAGE_CAP = "not read: page cap"
# A page-capped reading read again in full (``read --recap``): ingest replaces the capped
# text with it wherever an attachment still holds the capped text (``detail``:
# ``capped_text_sha256``).
NOTE_FULL_AFTER_CAP = "read in full after page cap"
# Readings a full re-read may replace a capped one with (a timeout or failure keeps it).
_FULL_READ = ("extracted", "extracted_ocr", "ocr_no_text")


def _text_digest(text: str | None) -> str | None:
    return None if text is None else sha256(text.encode("utf-8")).hexdigest()
THUMB_SIDE = 256
MIN_EDGE_SHARE = 0.02
MIN_BACKGROUND_SHARE = 0.15


def text_likely(content: bytes) -> bool:
    """Whether a raster image likely holds text (:data:`MIN_EDGE_SHARE` of its thumbnail's
    pixels on a sharp edge and :data:`MIN_BACKGROUND_SHARE` in its most common brightness
    band). An image Pillow cannot read is likely (the extractor decides), never skipped."""
    try:
        import io
        from PIL import Image, ImageFilter
        with Image.open(io.BytesIO(content)) as image:
            image.draft("L", (THUMB_SIDE * 2, THUMB_SIDE * 2))   # a JPEG decodes small
            grey = image.convert("L")
        grey.thumbnail((THUMB_SIDE, THUMB_SIDE))
        pixels = grey.size[0] * grey.size[1]
        if pixels < 64:
            return True
        edges = grey.filter(ImageFilter.FIND_EDGES).histogram()
        edge_share = sum(edges[65:]) / pixels
        bands = grey.histogram()
        background = max(sum(bands[i:i + 16]) for i in range(0, 256, 16)) / pixels
    except Exception:  # noqa: BLE001 - not judged: read as before
        return True
    return edge_share >= MIN_EDGE_SHARE and background >= MIN_BACKGROUND_SHARE


def _is_image(metadata: Mapping[str, Any]) -> bool:
    return type_rank(metadata.get("name"), metadata.get("content_type")) == 3


def inline_reader(content: bytes, metadata: Mapping[str, Any], image_ocr: bool,
                  ocr_limits: tuple[int, int] | None = None, *, defer_ocr: bool = False
                  ) -> tuple[str, str | None, dict[str, Any] | None]:
    """The capture's extractor in this process (tests; the CLI uses processes)."""
    import contextlib
    from .capture_mail import extract_attachment
    from .document_understanding import ocr_limits as limited
    with (limited(*ocr_limits) if ocr_limits is not None else contextlib.nullcontext()):
        return extract_attachment(content, metadata, defer_ocr, False, image_ocr=image_ocr)


def _capped_pages(content: bytes, metadata: Mapping[str, Any], status: str,
                  text: str | None) -> tuple[int, int] | None:
    """The (first, last) pages a page-capped reading left unread (``NOTE_PAGE_CAP``):
    a paged reading whose text holds fewer pages than the file; else None."""
    from .captured_pages import text_pages
    from .captured_text import OCR_PENDING, TEXT_OCR, is_paged_file
    from .document_understanding import pdf_page_count
    if status not in (TEXT_OCR, OCR_PENDING) or not is_paged_file(
            {"mime_type": metadata.get("content_type") or "", "name": metadata.get("name") or ""}):
        return None
    try:
        count = pdf_page_count(content)
    except BaseException as error:  # noqa: BLE001 - a parser panic: no note (the reading
        if isinstance(error, (KeyboardInterrupt, SystemExit)):   # itself names its pages)
            raise
        return None
    read = len(text_pages(text)) if text is not None else 0
    return (read + 1, count) if count is not None and count > read else None


def read_pass(root: str | Path, *, workers: int = 12, budget_seconds: float,
              follow: bool = False, idle_seconds: float = 300.0, nice: int = 10,
              min_image_bytes: int = MIN_IMAGE_BYTES, seconds_each: float | None = None,
              progress: Callable[[dict[str, Any]], None] | None = None,
              progress_seconds: float = 60.0,
              reader: Callable[[bytes, Mapping[str, Any], bool],
                               tuple[str, str | None, dict[str, Any] | None]] | None = None,
              ocr_limits: tuple[int, int] | None = None, prefilter: bool = False,
              mail: Any = None, text_first: bool = False) -> dict[str, Any]:
    """Read every fetched (bytes, type, name) not read yet, best priority first, with
    ``workers`` extraction processes (``capture_mail._Extractors``: scrubbed environment,
    a reading past ``seconds_each`` or whose process died is the capture's own gap
    status, ``extraction_timed_out`` / ``extraction_crashed``, and its process is
    replaced; the capture's gap pass reads such a file again later). ``follow``: keep
    taking new downloads until none came for ``idle_seconds`` (or the budget ends).
    Bytes whose hash no longer matches are not read and not recorded. Counts only.

    ``ocr_limits`` (pages, dpi): page-capped OCR (``document_understanding.ocr_limits``);
    a reading it cut short is noted ``NOTE_PAGE_CAP`` with its unread pages, which the
    reading itself names (``skipped_by_cap``, cap ``ocr_pages``). ``prefilter``: an image
    :func:`text_likely` judges textless is not OCR-read (``NOTE_NO_TEXT_LIKELY``).
    ``mail`` (a ``MailCaptureStore`` opened read-only): the stored plan's images and
    scans (``StoredPlan``) are read too, by the same pool and priority, their bytes from
    the mail store (re-hashed).

    ``text_first`` (``read --ocr-pages 0``): no OCR at all. Each file's text layer is read
    (the capture's ``defer_ocr``); a file that needs OCR (``ocr_pending``) is not recorded,
    so it stays unread for a later OCR pass over the same store, which reads it and ingest
    applies as any reading (counted ``left_for_ocr``). Not with ``ocr_limits`` or ``mail``
    (the stored plan holds only images and scans)."""
    from concurrent.futures import ThreadPoolExecutor
    from .capture_mail import EXTRACT_SECONDS, _Extractors
    from .captured_text import OCR_PENDING
    if text_first and (ocr_limits is not None or mail is not None):
        raise ValueError("text_first_reads_downloads_without_ocr")
    fetch = FetchStore(root, read_only=True)
    readings = Readings(root)
    stored = None
    if mail is not None:
        try:
            stored = StoredPlan(root, read_only=True)
        except FileNotFoundError:
            stored = None
    mail_lock = threading.Lock()
    started = time.monotonic()
    deadline = started + budget_seconds
    counts: dict[str, Any] = {"read": 0, "by_status": {}, "seconds_reading": 0.0,
                              "digest_mismatch": 0, "stored_read": 0, "page_capped": 0, "deduped": 0,
                              "no_text_likely": 0, "left_for_ocr": 0}
    extractors = None
    if reader is None:
        extractors = _Extractors(workers, seconds_each or EXTRACT_SECONDS, nice=nice)

        def reader(content, metadata, image_ocr):
            return extractors.read(content, metadata, defer_ocr=text_first, defer_all=False,
                                   image_ocr=image_ocr, ocr_limits=ocr_limits)

    def one(digest: str, metadata: dict[str, Any], source: str
            ) -> tuple[str, str | None, Any, float, tuple[str, Any] | None] | None:
        began = time.monotonic()
        try:
            if source == "stored":
                with mail_lock:
                    content = mail.blob(digest)   # re-hashed by the store
            else:
                content = fetch.bytes_of(digest)
        except Exception:  # noqa: BLE001 - bytes gone or altered: nothing recorded
            return None
        image_ocr = image_ocr_rule(content, metadata, min_image_bytes)
        note = None
        if image_ocr and prefilter and _is_image(metadata) and not text_likely(content):
            image_ocr, note = False, (NOTE_NO_TEXT_LIKELY, None)
        status, text, made = reader(content, metadata, image_ocr)
        if ocr_limits is not None and note is None:
            capped = _capped_pages(content, metadata, status, text)
            if capped is not None:
                note = (NOTE_PAGE_CAP, {"first_page": capped[0], "last_page": capped[1],
                                        "cap": "ocr_pages", "limit": ocr_limits[0],
                                        "text_sha256": _text_digest(text)})
        return status, text, made, time.monotonic() - began, note

    # The candidates, best priority first: a heap fed incrementally (downloads that
    # landed since the last look, stored rows past the last one listed), so each look
    # costs the new rows only, never the whole list again. A read key is queued once.
    import heapq
    queue: list[tuple[int, int, str, str, str, str]] = []
    queued: set[str] = set()
    twins: dict[tuple[str, int], list[str]] = {}   # (digest, kind) -> other keys
    heads: dict[str, tuple[str, int]] = {}         # read key -> its (digest, kind)
    since = {"done_at": float("-inf"), "rowid": 0, "looked": float("-inf")}

    def look() -> int:
        found = 0
        rows = [(r["read_key"], r["content_sha256"], r["read_meta"], r["priority"],
                 "fetched", r["done_at"]) for r in fetch.db.execute(
                     "SELECT read_key, content_sha256, read_meta, priority, done_at FROM work "
                     "WHERE status='fetched' AND read_key IS NOT NULL AND done_at >= ?",
                     (since["done_at"],))]
        if stored is not None:
            for r in stored.db.execute(
                    "SELECT rowid, read_key, content_sha256, read_meta, priority FROM stored "
                    "WHERE rowid > ? ORDER BY rowid", (since["rowid"],)):
                since["rowid"] = max(since["rowid"], r["rowid"])
                rows.append((r["read_key"], r["content_sha256"], r["read_meta"],
                             r["priority"], "stored", None))
        for key, digest, meta, priority, source, done_at in rows:
            if done_at is not None:
                since["done_at"] = max(since["done_at"], done_at)
            if key in queued:
                continue
            queued.add(key)   # read before (a restart) or now: never looked at again
            if readings.get(key):
                continue
            # Content dedupe: the same bytes of the same kind are read once; the other
            # names get that reading when it is recorded (``twins``).
            twin = (digest, type_rank(*(lambda m: (m.get("name"), m.get("content_type")))(
                json.loads(meta))))
            if twin in twins:
                twins[twin].append(key)
                continue
            twins[twin] = []
            heads[key] = twin
            heapq.heappush(queue, (priority, len(queued), key, digest, meta, source))
            found += 1
        since["looked"] = time.monotonic()
        return found

    inflight: dict[Any, tuple[str, str, str]] = {}
    last_new = last_report = time.monotonic()
    try:
        with ThreadPoolExecutor(workers, thread_name_prefix="attach-read") as pool:
            while True:
                now = time.monotonic()
                if now < deadline and len(inflight) < 2 * workers:
                    if (len(queue) < 2 * workers and now - since["looked"] >= 2.0) or \
                            now - since["looked"] >= 30.0:
                        if look():
                            last_new = now
                    while queue and len(inflight) < 2 * workers:
                        _p, _n, key, digest, meta, source = heapq.heappop(queue)
                        future = pool.submit(one, digest, json.loads(meta), source)
                        inflight[future] = (key, digest, source)
                if not inflight and not (queue and now < deadline):
                    if now >= deadline or not follow or now - last_new >= idle_seconds:
                        break
                    time.sleep(2.0)
                    continue
                done, _ = wait(list(inflight), timeout=2.0, return_when=FIRST_COMPLETED)
                for future in done:
                    key, digest, source = inflight.pop(future)
                    answer = future.result()
                    if answer is None:   # bytes gone or altered: nothing recorded
                        counts["digest_mismatch"] += 1
                        continue
                    status, text, made, seconds, note = answer
                    if text_first and status == OCR_PENDING:
                        # needs OCR: left unread (with its twins) for the OCR pass
                        counts["left_for_ocr"] += 1 + len(twins.pop(heads.pop(key, None), ()))
                        counts["seconds_reading"] += seconds
                        continue
                    with readings.db:
                        for same in (key, *twins.get(heads.get(key), ())):
                            readings.db.execute(
                                "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?)",
                                # a twin's file readings are keyed by the head's name:
                                # none for it (the capture then keeps its original)
                                (same, digest, status, text,
                                 None if made is None or same != key else json.dumps(made),
                                 seconds,
                                 time.time()))
                            if note is not None:
                                readings.db.execute(
                                    "INSERT OR REPLACE INTO notes VALUES (?,?,?)",
                                    (same, note[0], None if note[1] is None
                                     else json.dumps(note[1], sort_keys=True)))
                        counts["deduped"] += len(twins.get(heads.get(key), ()))
                        # a twin that arrives later is read on its own
                        twins.pop(heads.pop(key, None), None)
                    counts["read"] += 1
                    counts["stored_read"] += source == "stored"
                    counts["seconds_reading"] += seconds
                    counts["by_status"][status] = counts["by_status"].get(status, 0) + 1
                    if note is not None:
                        counts["page_capped" if note[0] == NOTE_PAGE_CAP
                               else "no_text_likely"] += 1
                if progress is not None and time.monotonic() - last_report >= progress_seconds:
                    last_report = time.monotonic()
                    progress(_read_report(counts, started))
    finally:
        if extractors is not None:
            extractors.close()
        fetch.close()
    return _read_report(counts, started)


def recap_pass(root: str | Path, *, workers: int = 12, budget_seconds: float,
               nice: int = 10, mail: Any = None, seconds_each: float | None = None,
               reader: Callable[[bytes, Mapping[str, Any], bool],
                                tuple[str, str | None, dict[str, Any] | None]] | None = None
               ) -> dict[str, Any]:
    """The pages a page-capped reading left (``NOTE_PAGE_CAP``) read after all: each such
    reading read again in full (no limits), best priority first. A full reading replaces
    the capped one and its note becomes ``NOTE_FULL_AFTER_CAP`` (the capped text's digest
    kept, so ingest replaces only that text); a timeout or failure keeps the capped one
    and its note for a later run. Bytes from this store, or the mail store (``mail``)
    for a stored item. Counts only."""
    from concurrent.futures import ThreadPoolExecutor
    from .capture_mail import EXTRACT_SECONDS, _Extractors
    fetch = FetchStore(root, read_only=True)
    readings = Readings(root)
    stored = None
    try:
        stored = StoredPlan(root, read_only=True)
    except FileNotFoundError:
        pass
    started = time.monotonic()
    deadline = started + budget_seconds
    counts: dict[str, Any] = {"read": 0, "by_status": {}, "seconds_reading": 0.0,
                              "digest_mismatch": 0, "replaced": 0, "kept_capped": 0}
    todo = []
    for key, detail in readings.db.execute(
            "SELECT read_key, detail FROM notes WHERE note=?", (NOTE_PAGE_CAP,)).fetchall():
        row = fetch.db.execute("SELECT content_sha256, read_meta, priority FROM work WHERE "
                               "read_key=? AND status='fetched' LIMIT 1", (key,)).fetchone()
        source = "fetched"
        if row is None and stored is not None:
            row = stored.db.execute("SELECT content_sha256, read_meta, priority FROM stored "
                                    "WHERE read_key=?", (key,)).fetchone()
            source = "stored"
        if row is None or (source == "stored" and mail is None):
            continue
        todo.append((row["priority"], key, row["content_sha256"], row["read_meta"], source,
                     json.loads(detail or "{}").get("text_sha256")))
    todo.sort()
    extractors = None
    if reader is None:
        extractors = _Extractors(workers, seconds_each or EXTRACT_SECONDS, nice=nice)

        def reader(content, metadata, image_ocr):
            return extractors.read(content, metadata, defer_ocr=False, defer_all=False,
                                   image_ocr=image_ocr)
    mail_lock = threading.Lock()

    def one(item):
        _p, key, digest, meta, source, capped = item
        if time.monotonic() >= deadline:
            return None
        began = time.monotonic()
        try:
            if source == "stored":
                with mail_lock:
                    content = mail.blob(digest)
            else:
                content = fetch.bytes_of(digest)
        except Exception:  # noqa: BLE001 - bytes gone or altered
            return key, digest, None, capped, 0.0
        return key, digest, reader(content, json.loads(meta), False), capped, \
            time.monotonic() - began

    try:
        with ThreadPoolExecutor(workers, thread_name_prefix="attach-recap") as pool:
            for answer in pool.map(one, todo):
                if answer is None:
                    continue
                key, digest, found, capped, seconds = answer
                if found is None:
                    counts["digest_mismatch"] += 1
                    continue
                status, text, made = found
                counts["read"] += 1
                counts["seconds_reading"] += seconds
                counts["by_status"][status] = counts["by_status"].get(status, 0) + 1
                if status not in _FULL_READ:
                    counts["kept_capped"] += 1
                    continue
                with readings.db:
                    readings.db.execute(
                        "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?)",
                        (key, digest, status, text,
                         None if made is None else json.dumps(made), seconds, time.time()))
                    readings.db.execute(
                        "INSERT OR REPLACE INTO notes VALUES (?,?,?)",
                        (key, NOTE_FULL_AFTER_CAP,
                         json.dumps({"capped_text_sha256": capped}, sort_keys=True)))
                counts["replaced"] += 1
    finally:
        if extractors is not None:
            extractors.close()
        fetch.close()
    return _read_report(counts, started)


def _read_report(counts: Mapping[str, Any], started: float) -> dict[str, Any]:
    seconds = max(time.monotonic() - started, 1e-9)
    return {**counts, "seconds_reading": round(counts["seconds_reading"], 1),
            "seconds": round(seconds, 1), "per_second": round(counts["read"] / seconds, 2)}


# ---------------------------------------------------------------------- CLI

def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def _scrubbed_environment() -> None:
    """The read pool holds no credential: only what an extraction process gets."""
    from .capture_mail import extraction_env
    kept = extraction_env(os.environ)
    os.environ.clear()
    os.environ.update(kept)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.attach_fetch",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="list the mail store's deferred attachments")
    plan.add_argument("--state-dir", required=True, help="the workbench state (mail store)")
    plan.add_argument("--fetch-dir", required=True)
    env_in = os.environ if environ is None else environ
    down = sub.add_parser("download", help="fetch the listed attachments' bytes")
    for flag, name in (("--state-dir", "PLX_WORKBENCH_STATE"),
                       ("--fetch-dir", "PLX_ATTACH_FETCH_DIR"),
                       ("--live-audience", "PLX_MAIL_LIVE_AUDIENCE")):
        down.add_argument(flag, default=env_in.get(name) or None,
                          required=not env_in.get(name), help=f"default: ${name}")
    down.add_argument("--principal", default=None)
    down.add_argument("--budget-seconds", type=float, required=True)
    down.add_argument("--threads", type=int, default=64)
    down.add_argument("--per-mailbox", type=int, default=1,
                      help="chunks in flight on one mailbox (x --batch-size requests)")
    down.add_argument("--batch-size", type=int, default=4)
    down.add_argument("--no-batch", action="store_true")
    down.add_argument("--limit", type=int, default=None)
    down.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_BYTES / (1 << 30))
    down.add_argument("--progress-seconds", type=float, default=60.0)
    read = sub.add_parser("read", help="read the fetched bytes (text layer, OCR)")
    read.add_argument("--fetch-dir", required=True)
    read.add_argument("--budget-seconds", type=float, required=True)
    read.add_argument("--workers", type=int, default=12)
    read.add_argument("--follow", action="store_true")
    read.add_argument("--idle-seconds", type=float, default=300.0)
    read.add_argument("--min-image-bytes", type=int, default=MIN_IMAGE_BYTES)
    read.add_argument("--nice", type=int, default=10)
    read.add_argument("--progress-seconds", type=float, default=60.0)
    read.add_argument("--state-dir", default=None,
                      help="also read the stored plan's images and scans from this mail store")
    read.add_argument("--full-ocr", action="store_true",
                      help="no page cap: every image-only page at full resolution")
    read.add_argument("--ocr-pages", type=int, default=None,
                      help="image-only pages OCR-read per file (default: the fast cap); "
                           "0: no OCR, text layers only (text-first)")
    read.add_argument("--ocr-dpi", type=int, default=None)
    read.add_argument("--no-prefilter", action="store_true",
                      help="OCR every image the size rule passes")
    read.add_argument("--recap", action="store_true",
                      help="read the page-capped readings again in full")
    stored = sub.add_parser("plan-stored",
                            help="list the mail store's unread images and scans on disk")
    stored.add_argument("--state-dir", required=True)
    stored.add_argument("--fetch-dir", required=True)
    status = sub.add_parser("status", help="counts of the fetch store")
    status.add_argument("--fetch-dir", required=True)
    args = parser.parse_args(argv)

    if args.command == "status":
        fetch = FetchStore(args.fetch_dir, read_only=True)
        out = fetch.counts()
        try:
            readings = Readings(args.fetch_dir, read_only=True)
            out["readings"] = {s: n for s, n in readings.db.execute(
                "SELECT text_status, COUNT(*) FROM readings GROUP BY text_status")}
            out["notes"] = {n: c for n, c in readings.db.execute(
                "SELECT note, COUNT(*) FROM notes GROUP BY note")} if readings.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='notes'").fetchone() else {}
        except FileNotFoundError:
            out["readings"] = {}
        try:
            plan = StoredPlan(args.fetch_dir, read_only=True)
            out["stored"] = {k: n for k, n in plan.db.execute(
                "SELECT kind, COUNT(*) FROM stored GROUP BY kind")}
        except FileNotFoundError:
            out["stored"] = {}
        _print(out)
        return 0
    if args.command == "read":
        from .document_understanding import FAST_OCR_DPI, FAST_OCR_PAGES, MAX_OCR_PAGES
        # ``--ocr-pages 0``: text-first, no OCR (``read_pass(text_first=True)``); unset:
        # the fast cap; ``--full-ocr``: no cap.
        text_first = args.ocr_pages == 0
        limits = None if args.full_ocr or text_first else (
            FAST_OCR_PAGES if args.ocr_pages is None else args.ocr_pages,
            FAST_OCR_DPI if args.ocr_dpi is None else args.ocr_dpi)
        if not 1 <= args.workers <= 32 or not args.budget_seconds > 0 or (
                args.full_ocr and (args.ocr_pages is not None or args.ocr_dpi is not None)) or (
                text_first and (args.ocr_dpi is not None or args.recap
                                or args.state_dir is not None)) or (
                limits is not None and not (1 <= limits[0] <= MAX_OCR_PAGES
                                            and 100 <= limits[1] <= 300)):
            _print({"ok": False, "error": "arguments_invalid"})
            return 2
        _scrubbed_environment()
        mail = None
        if args.state_dir is not None:
            from .capture_mail import MailCaptureStore
            mail = MailCaptureStore.open_read_only(args.state_dir, timeout=60)
        if args.recap:
            if not args.full_ocr:
                _print({"ok": False, "error": "recap_needs_full_ocr"})
                return 2
            out = recap_pass(args.fetch_dir, workers=args.workers, nice=args.nice,
                             budget_seconds=args.budget_seconds, mail=mail)
            _print({"ok": True, "final": True, **out})
            return 0
        out = read_pass(args.fetch_dir, workers=args.workers, nice=args.nice,
                        budget_seconds=args.budget_seconds, follow=args.follow,
                        idle_seconds=args.idle_seconds, min_image_bytes=args.min_image_bytes,
                        progress=_print, progress_seconds=args.progress_seconds,
                        ocr_limits=limits, prefilter=not args.no_prefilter, mail=mail,
                        text_first=text_first)
        _print({"ok": True, "final": True, **out})
        return 0

    from .capture_mail import MailCaptureStore
    mail = MailCaptureStore.open_read_only(args.state_dir, timeout=60)
    fetch = FetchStore(args.fetch_dir)
    if Path(args.fetch_dir).expanduser().resolve() == mail.root or \
            mail.root in Path(args.fetch_dir).expanduser().resolve().parents:
        _print({"ok": False, "error": "fetch_dir_inside_mail_store"})
        return 2
    if args.command == "plan":
        _print({"ok": True, **fetch.plan(mail), **fetch.counts()})
        return 0
    if args.command == "plan-stored":
        # The people exclusion from the environment, fail-closed: a withheld mailbox's
        # files are never listed (and ingest checks again before applying a reading).
        from .people_exclusion import PeopleExclusion
        guard = PeopleExclusion.from_env(env_in)
        withheld = None if guard.unavailable else mail.withheld_mailboxes(guard)
        if withheld is None:
            _print({"ok": False, "error": "people_exclusion_unavailable"})
            return 3
        _print({"ok": True, **StoredPlan(args.fetch_dir).plan(mail, withheld)})
        return 0

    from .capture_mail import CAPTURE_PRINCIPAL
    from .graph_read import GraphMailReader, build_graph
    from .live_source_guard import BoundedBackoff, LiveSourceAudience
    from .people_exclusion import ExclusionUnavailable
    if not args.budget_seconds > 0:
        _print({"ok": False, "error": "budget_seconds_invalid"})
        return 2
    audience = LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    api, _transport = build_graph(env, sender=sender, retry=BoundedBackoff(budget_seconds=60))
    reader = GraphMailReader(api, audience)
    actor = Actor(audience.tenant, frozenset({args.principal or CAPTURE_PRINCIPAL}),
                  "restricted")
    try:
        reader.prepare_people()
    except ExclusionUnavailable:
        _print({"ok": False, "error": "people_exclusion_unavailable"})
        return 3
    withheld = mail.withheld_mailboxes(reader.people)
    if withheld is None:
        _print({"ok": False, "error": "people_exclusion_unavailable"})
        return 3
    downloader = Downloader(fetch, reader, actor, threads=args.threads,
                            per_mailbox=args.per_mailbox, batch=not args.no_batch,
                            batch_size=args.batch_size,
                            min_free_bytes=int(args.min_free_gb * (1 << 30)))
    with reader.directory_cache():
        out = downloader.run(fetch.todo(args.limit), withheld=withheld,
                             budget_seconds=args.budget_seconds, progress=_print,
                             progress_seconds=args.progress_seconds)
    _print({"ok": True, "final": True, **out, "store": fetch.counts()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
