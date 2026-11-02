"""What the document steps of a build derived from each captured document, kept between
builds.

The ``mentions`` and ``text_index`` post_project steps (``ontology.mentions``,
``ontology.text_index``) read every document of every build: each read the document's
text (a mail body blob, an attachment's kept reading) and the mentions step extracted its
mentions (about 3 ms a mail version) and resolved each one through the identifier index.
At 0.5M documents the mentions step alone took 3,280 s of a cold build (cycle 7) and of
every refresh too.

A :class:`DocumentMemo` (``<projection state>/mentions.sqlite``, beside the kept readings,
0600: it holds mentions, link rows and text digests) keeps, by the digest of a document's
whole value (:func:`document_key`: citation, kind, every text field, sender class, digest,
times, date, copy key, withheld reason):

* ``mentions``: its extracted mentions (a function of the document and the extractor code
  alone, read back by any later build under the same code, cold ones included) and what
  resolving them wrote (its link rows before corroboration, its unanswered reference
  numbers, its counts) with what that read (the digest of the document's own version row
  and of every version a mention cites, and every identifier key its mentions were looked
  up under), valid under the build token of the generation it was written or checked in
  (``valid_at``). A build of the next generation takes them only when the document's
  version rows read alike, none of its keys is one whose index rows changed between the two
  generations (``ontology.project_incremental.BuildContext.changed_key``) and no reviewed
  alias answers one of its keys; else it resolves the document again;
* ``texts``: the text index's fields of it (citation, field path, digest and length of
  the indexed text), so the index rewrites only what changed without reading the text;
* ``groups``: for a source that names the documents of one captured record by a key of
  what they are read from (``document_groups``: a mail message with its attachments, a
  SharePoint file), the keys of those documents, so an unchanged record is not read at
  all. A group whose reading of a captured file failed for a reason not remembered
  (``integrations.captured_text.unremembered``) is not kept: the next build reads it again.

Every build marks the entries it used (its generation number); those no build of the last
``KEEP_BUILDS`` generations used go when the mentions step ends. Any failure of the file is
a miss: the steps then derive the document again.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
import zlib

FORMAT = "plx-document-memo/v1"
FILE = "mentions.sqlite"


def document_key(document: Any) -> str:
    """The digest of everything a ``Document`` holds (``ontology.mentions.Document``)."""
    value = [document.citation, document.kind,
             [[f.citation, f.field_path, f.text] for f in document.fields],
             document.sender_class, document.digest, document.recorded_at,
             document.superseded_at, document.date,
             list(document.copy_key) if document.copy_key is not None else None,
             document.text_withheld]
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()


def row_digest(row: Any) -> str | None:
    """The digest of a version row the step read (None: no row)."""
    if row is None:
        return None
    return sha256(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"),
                             default=str).encode("utf-8")).hexdigest()


def _pack(value: Any) -> bytes:
    return zlib.compress(json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                         .encode("utf-8"), 6)


def _unpack(blob: bytes) -> Any:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class DocumentMemo:
    """The memo file at ``path`` for one build: ``base`` is the build token of the
    generation it starts from (None: cold; a ``mentions`` entry's resolution is taken only
    when it was valid there), ``token`` its own and ``number`` its generation number."""

    KEEP_BUILDS = 2
    _BATCH = 500

    def __init__(self, path: str | Path, base: str | None, token: str, number: int) -> None:
        self.base, self.token, self.number = base, token, number
        self._pending = 0
        os.close(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(path, 0o600)
        self._db = sqlite3.connect(str(path), isolation_level=None)
        try:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = NORMAL")
            for table, extra in (("mentions", "code TEXT NOT NULL, valid_at TEXT, "),
                                 ("texts", "code TEXT NOT NULL, "), ("groups", "")):
                self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, "
                                 f"{extra}value BLOB NOT NULL, used INTEGER NOT NULL)")
            self._db.execute("BEGIN")
        except BaseException:
            self._db.close()
            raise

    def _wrote(self) -> None:
        self._pending += 1
        if self._pending >= self._BATCH:
            self._db.execute("COMMIT")
            self._db.execute("BEGIN")
            self._pending = 0

    def _get(self, table: str, key: str, code: str | None) -> tuple[Any, Any] | None:
        try:
            columns = "value, code, valid_at" if table == "mentions" else (
                "value, code, NULL" if table == "texts" else "value, NULL, NULL")
            row = self._db.execute(f"SELECT {columns} FROM {table} WHERE key = ?",
                                   (key,)).fetchone()
            if row is None or row[1] != code:
                return None
            return _unpack(row[0]), row[2]
        except Exception:  # noqa: BLE001 - an unreadable entry is a miss
            return None

    def _put(self, table: str, key: str, value: Any, *extra: Any) -> None:
        try:
            names = {"mentions": "key, code, valid_at, value, used",
                     "texts": "key, code, value, used", "groups": "key, value, used"}[table]
            values = (key, *extra, _pack(value), self.number)
            self._db.execute(f"INSERT OR REPLACE INTO {table} ({names}) VALUES "
                             f"({', '.join('?' for _ in values)})", values)
            self._wrote()
        except Exception:  # noqa: BLE001 - an entry not kept is derived again next time
            return

    def _used(self, table: str, key: str, valid: bool = False) -> None:
        try:
            if valid:
                self._db.execute("UPDATE mentions SET valid_at = ?, used = ? WHERE key = ?",
                                 (self.token, self.number, key))
            else:
                self._db.execute(f"UPDATE {table} SET used = ? WHERE key = ?",
                                 (self.number, key))
            self._wrote()
        except Exception:  # noqa: BLE001 - not marked: derived again later
            return

    # -- mentions ------------------------------------------------------------------

    def mentions(self, key: str, code: str) -> tuple[dict[str, Any], bool] | None:
        """(entry, whether its ``resolved`` part was valid in the base generation) kept
        for document ``key`` under ``code``, else None."""
        found = self._get("mentions", key, code)
        if found is None:
            return None
        entry, valid_at = found
        return entry, (self.base is not None and valid_at == self.base
                       and isinstance(entry.get("resolved"), dict))

    def keep_mentions(self, key: str, code: str, entry: Mapping[str, Any]) -> None:
        """Keep ``entry`` for document ``key``, its ``resolved`` part valid in this
        generation."""
        self._put("mentions", key, entry, code, self.token)

    def still_valid(self, key: str) -> None:
        """The ``resolved`` part of document ``key``'s entry, checked, holds in this
        generation."""
        self._used("mentions", key, valid=True)

    # -- texts ---------------------------------------------------------------------

    def text(self, key: str, code: str) -> Any:
        found = self._get("texts", key, code)
        if found is None:
            return None
        self._used("texts", key)
        return found[0]

    def keep_text(self, key: str, code: str, value: Any) -> None:
        self._put("texts", key, value, code)

    # -- groups --------------------------------------------------------------------

    def group(self, key: str) -> list[str] | None:
        """The document keys of a captured record's documents (``key``: the source's
        digest of what they are read from), else None."""
        found = self._get("groups", key, None)
        if found is None or not isinstance(found[0], list):
            return None
        self._used("groups", key)
        return found[0]

    def peek_group(self, key: str) -> list[str] | None:
        """:meth:`group`, marking nothing used (a read ahead of a step's own reads)."""
        found = self._get("groups", key, None)
        return found[0] if found is not None and isinstance(found[0], list) else None

    def peek_text(self, key: str, code: str) -> bool:
        """Whether :meth:`text` holds document ``key`` under ``code`` (marking nothing)."""
        return self._get("texts", key, code) is not None

    def holds_group(self, key: str, code: str) -> bool:
        """Whether :meth:`group` and :meth:`mentions` hold every document of group ``key``
        under ``code`` (marking nothing used: a read ahead of the step's own reads)."""
        found = self.peek_group(key)
        return found is not None and all(
            self._get("mentions", document, code) is not None for document in found)

    def keep_group(self, key: str, documents: Sequence[str]) -> None:
        self._put("groups", key, list(documents))

    def close(self, *, prune: bool) -> None:
        """Commit and close; ``prune``: drop the entries no build of the last
        ``KEEP_BUILDS`` generations used."""
        try:
            if prune:
                for table in ("mentions", "texts", "groups"):
                    self._db.execute(f"DELETE FROM {table} WHERE used <= ?",
                                     (self.number - self.KEEP_BUILDS,))
            self._db.execute("COMMIT")
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._db.close()


__all__ = ["DocumentMemo", "FILE", "FORMAT", "document_key", "row_digest"]
