"""How many cited pages each captured reading has, kept by the reading's key.

Kept apart from ``captured_text`` on purpose: that module's source is an input of the
SharePoint identifier index's code (``case_tools_host._index_inputs``), and a change to it
makes every process read every captured file's text again at its start.
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

from .captured_pages import text_pages
from .captured_text import _blob_kept, _mime, _name, captured_text, ocr_digest, readings_code


class PageCounts:
    """How many cited pages each captured reading has (:meth:`CapturedText.page`'s
    range), kept by the reading's key (bytes digest, MIME type, name, pinned OCR text
    digest): whether a page of an attachment exists is a question of its bytes alone, so
    an item gate asking it never decodes the text again once one read counted it. With a
    ``path`` (a SQLite file in the app's own data directory, never the state's) the counts
    are kept between processes and restarts, under the digest of the reading code
    (:func:`readings_code`), so a kept count is the one a read now would make. A count is
    served only while its bytes are on disk (as a kept reading is); a reading that failed
    is never counted. Any failure of the file is a miss."""

    MAX_KEPT = 200_000

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = None if path is None else Path(path)
        self._held: OrderedDict[tuple, int] = OrderedDict()
        self._lock = threading.Lock()
        self._local = threading.local()
        self._code: str | None = None

    def _conn(self) -> Any:
        if self.path is None:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is None:
            import sqlite3
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            conn = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS page_counts (key TEXT PRIMARY KEY, "
                         "pages INTEGER NOT NULL)")
            self._local.conn = conn
        if self._code is None:
            self._code = readings_code()
        return conn

    def _disk_key(self, key: tuple) -> str:
        return sha256(json.dumps([self._code, *key]).encode()).hexdigest()

    def count(self, blobs: str | Path, row: Mapping[str, Any]) -> int | None:
        """The number of cited pages of ``row``'s reading (0: not paged or no text);
        None when it cannot be read now (:func:`captured_text` None)."""
        digest = row.get("content_sha256")
        if not isinstance(digest, str):
            return None
        key = (digest, _mime(row), _name(row), ocr_digest(row))
        with self._lock:
            held = self._held.get(key)
        if held is not None:
            return held if _blob_kept(blobs, digest) else None
        try:
            conn = self._conn()
            found = None if conn is None else conn.execute(
                "SELECT pages FROM page_counts WHERE key=?", (self._disk_key(key),)).fetchone()
        except Exception:  # noqa: BLE001 - a kept count that cannot be read is a miss
            found = None
        if found is not None and _blob_kept(blobs, digest):
            self._hold(key, int(found[0]))
            return int(found[0])
        read = captured_text(blobs, row)
        if read is None:
            return None
        text = read.text
        pages = len(text_pages(text)) if read.paged and text is not None else 0
        self._hold(key, pages)
        try:
            conn = self._conn()
            if conn is not None:
                with conn:
                    conn.execute("INSERT OR REPLACE INTO page_counts VALUES (?, ?)",
                                 (self._disk_key(key), pages))
        except Exception:  # noqa: BLE001 - not kept: counted again by the next process
            pass
        return pages

    def has_page(self, blobs: str | Path, row: Mapping[str, Any], page: int) -> bool | None:
        """Whether :meth:`CapturedText.page` of ``row``'s reading holds ``page``; None when
        the reading cannot be read now."""
        count = self.count(blobs, row)
        return None if count is None else 1 <= page <= count

    def _hold(self, key: tuple, pages: int) -> None:
        with self._lock:
            self._held[key] = pages
            self._held.move_to_end(key)
            while len(self._held) > self.MAX_KEPT:
                self._held.popitem(last=False)


PAGE_COUNTS = PageCounts()


def keep_page_counts(path: str | Path | None) -> None:
    """Keep this process's page counts (:class:`PageCounts`) in ``path`` (None: in this
    process only)."""
    global PAGE_COUNTS
    PAGE_COUNTS = PageCounts(path)
