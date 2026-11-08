"""The markings a document carries in search, applied on every read path that can
return a document's identity or text.

A document's chunks carry the markings its content and labels earn
(``operating.chunking.sensitive``: a statement, a pay stub, an offer or termination letter,
a payroll or HR confidentiality label), written at chunking and re-marked in place by
``python3 -m operating.chunking.remark``. The re-mark updates the search store only; the
served store and its text index were built before a rule existed. So every read decides
here too, and a document whose chunks carry a marking the reader does not hold is
withheld exactly as any other object the reader may not read:

* object visibility (``plx.find``, ``plx.object``, ``plx.around``, ``plx.timeline``, record
  pages and their document lists, aggregates): the store's visible-object predicates add
  ``sql(column)`` with ``params(held)`` (one read of the marked documents per process and
  refresh, never per row);
* ``plx.read`` and the attachments listed inside a message: ``handle_withheld``;
* ``plx.documents`` and opening a record page: ``withheld`` (a batch by object id).

Only with hybrid search on (``PLX_SEARCH=hybrid``, ``PLX_PG_DSN``): without it there is no
search store and the served store's own markings decide. Fail closed where pay stubs and
HR letters live: when the store cannot be read, every file (a SharePoint file, an
attachment, a container member) is withheld from a reader who does not hold every
marking; a message, which the word index answers when search is down, and a business
record, which has no document chunks, are not. A reader holding every marking (the
owner's full-read grant) is never withheld anything and costs no read.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Iterable

# Object types whose chunks are a document's (a record's own text, ``record_text`` or
# ``bc_text``, carries its record's markings and is gated by the record's own fields).
DOCUMENT_TYPES = frozenset({"Email", "SharePointFile", "Attachment", "ContainerMember",
                            "TeamsChatMessage", "TeamsChannelMessage", "Meeting", "MCTask",
                            "MCApproval"})
FILE_TYPES = frozenset({"SharePointFile", "Attachment", "ContainerMember"})
RECORD_KINDS = ("record_text", "bc_text")
# The marked documents are re-read in the background once this old (a re-mark shows within
# about this long); an unreadable store is retried after ``RETRY_S``.
REFRESH_S = 120.0
RETRY_S = 30.0
_PAGE = re.compile(r"[#&]page=[0-9]+$")


def _every_marking() -> frozenset[str]:
    from ontology.store_ddl import MARKINGS
    return frozenset(MARKINGS)


def holds_every(held: Iterable[str]) -> bool:
    """Whether ``held`` covers every marking a document can carry (nothing is withheld)."""
    return _every_marking() <= frozenset(held)


def _document(object_id: str) -> bool:
    return isinstance(object_id, str) and object_id.split(":", 1)[0] in DOCUMENT_TYPES


def enabled() -> bool:
    from operating.search import plug
    return plug.enabled()


def withheld(object_ids: Iterable[str], held: Iterable[str], *, conn: Any = None
             ) -> set[str]:
    """The document objects among ``object_ids`` whose search chunks carry a marking
    outside ``held`` (every file among them when the search store cannot be read)."""
    held = frozenset(held)
    ids = sorted({o for o in object_ids if _document(o)})
    if not ids or (conn is None and not enabled()) or holds_every(held):
        return set()
    own = conn is None
    try:
        if own:
            import psycopg
            from operating.embed import config
            conn = psycopg.connect(os.environ.get("PLX_PG_DSN") or config.DEFAULT_DSN,
                                   connect_timeout=5)
        rows = conn.execute(
            "SELECT DISTINCT object_id FROM search.chunks WHERE object_id = ANY(%s) "
            "AND kind <> ALL(%s) AND NOT (markings <@ %s::text[])",
            (ids, list(RECORD_KINDS), sorted(set(held)))).fetchall()
        return {row[0] for row in rows}
    except Exception:  # noqa: BLE001 - undecidable: files withheld (fail closed)
        return {o for o in ids if o.split(":", 1)[0] in FILE_TYPES}
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- marked set

class _Marked:
    """Every document whose (non-record) search chunks carry a marking: object id ->
    markings and chunk handle (less a page suffix) -> markings, read in one statement,
    kept per DSN and refreshed in the background. ``None`` while unreadable."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.kept: dict[str, tuple[float, Any]] = {}
        self.refreshing: set[str] = set()

    @staticmethod
    def _read(dsn: str) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            rows = conn.execute(
                "SELECT object_id, source_handle, markings FROM search.chunks "
                "WHERE kind <> ALL(%s) AND markings <> '{}'", (list(RECORD_KINDS),)).fetchall()
        ids: dict[str, set[str]] = {}
        handles: dict[str, set[str]] = {}
        for object_id, handle, marks in rows:
            if object_id:
                ids.setdefault(object_id, set()).update(marks)
            if handle:
                handles.setdefault(_PAGE.sub("", handle), set()).update(marks)
        return ({k: frozenset(v) for k, v in ids.items()},
                {k: frozenset(v) for k, v in handles.items()})

    def _load(self, dsn: str) -> Any:
        try:
            got = self._read(dsn)
        except Exception:  # noqa: BLE001 - unreadable: undecidable (fail closed)
            got = None
        with self.lock:
            self.kept[dsn] = (time.monotonic(), got)
            self.refreshing.discard(dsn)
        return got

    def get(self) -> Any:
        from operating.embed import config
        dsn = os.environ.get("PLX_PG_DSN") or config.DEFAULT_DSN
        now = time.monotonic()
        with self.lock:
            hit = self.kept.get(dsn)
            if hit is not None:
                age = now - hit[0]
                if hit[1] is None and age < RETRY_S:
                    return None
                if hit[1] is not None and age >= REFRESH_S and dsn not in self.refreshing:
                    self.refreshing.add(dsn)
                    threading.Thread(target=self._load, args=(dsn,), daemon=True,
                                     name="plx-document-marks").start()
                if hit[1] is not None:
                    return hit[1]
        return self._load(dsn)

    def forget(self) -> None:
        with self.lock:
            self.kept.clear()


_MARKED = _Marked()


def forget() -> None:
    """Drop the kept marked set (the next read re-reads it): after a re-mark, in tests."""
    _MARKED.forget()


def withheld_set(held: Iterable[str]) -> tuple[frozenset[str], frozenset[str]] | None:
    """(the object ids, the chunk handles) of documents marked beyond ``held``; None when
    the search store cannot be read (the caller withholds every file)."""
    held = frozenset(held)
    if not enabled() or holds_every(held):
        return frozenset(), frozenset()
    got = _MARKED.get()
    if got is None:
        return None
    ids, handles = got
    return (frozenset(o for o, m in ids.items() if not m <= held),
            frozenset(h for h, m in handles.items() if not m <= held))


def params(held: Iterable[str]) -> dict[str, str]:
    """The SQL parameters ``sql`` reads for a reader holding ``held``: the withheld
    document ids (``:dm_ids``) and the object types withheld whole (``:dm_types``, every
    file type when the search store cannot be read)."""
    got = withheld_set(held)
    if got is None:
        return {"dm_ids": "[]", "dm_types": json.dumps(sorted(FILE_TYPES))}
    return {"dm_ids": json.dumps(sorted(got[0])), "dm_types": "[]"}


def sql(column: str) -> str:
    """SQL: the object id ``column`` is not a document marked beyond the reader
    (with ``params``)."""
    return (f"({column} NOT IN (SELECT value FROM json_each(:dm_ids)) AND "
            f"substr({column}, 1, instr({column}, ':') - 1) NOT IN "
            "(SELECT value FROM json_each(:dm_types)))")


def handle_withheld(handle: str, held: Iterable[str], object_ids: Iterable[str] = ()
                    ) -> bool:
    """Whether a cited document (``handle``, a page suffix ignored, or any of the objects
    the store holds at it, ``object_ids``) is marked beyond ``held``. When the search
    store cannot be read, a file's handle (one the served store holds as a file, or one
    naming an attachment) is withheld."""
    got = withheld_set(held)
    ids = [o for o in object_ids if isinstance(o, str)]
    if got is None:
        return (any(o.split(":", 1)[0] in FILE_TYPES for o in ids)
                or "#attachment=" in (handle or "") or (handle or "").startswith(
                    ("sharepoint:", "original:")))
    key = _PAGE.sub("", handle or "")
    return key in got[1] or any(o in got[0] for o in ids)


# --------------------------------------------------------------------------- text rows

SENSITIVE = frozenset({"finance", "pii"})   # ``operating.chunking.sensitive.MARKS``


LABEL_PATHS = ("row.sensitivity_label", "row.retention_label", "row.compliance_tag")
TITLE_PATHS = ("row.name", "row.subject")
CUE_PATHS = TITLE_PATHS + ("row.path",) + LABEL_PATHS


def sensitive_fields(fields: Iterable[tuple[str, str]]) -> bool:
    """Whether a document version whose fields are ``fields`` ((field path, text), its
    text fields' text and its name, subject, path and label values) is personal financial
    or HR-confidential, judged as ``ontology.text_index._sensitive`` judges a document: its
    whole text, its name or subject as support, the folders of its path and its recorded
    labels as labels."""
    from operating.chunking.sensitive import folders, personal_financial
    texts, title, labels = [], None, []
    for path, text in fields:
        if not isinstance(text, str):
            continue
        if path in TITLE_PATHS and title is None:
            title = text
        if path == "row.path":
            labels.extend(folders(text))
        elif path in LABEL_PATHS:
            labels.append(text)
        texts.append(text)
    return personal_financial("\n".join(texts), title=title, labels=labels) is not None


def readable_text(rows: Iterable[Any], held: Iterable[str],
                  sensitive: Any) -> list[Any]:
    """The captured-text rows (``text_fields`` / ``text_chunks``: each with ``object_id``
    and ``version_handle``) a reader holding ``held`` may read the text of: every row for
    a reader holding ``finance`` and ``pii``; else none of a document version
    ``sensitive`` (``[(object id, version handle)] -> the handles judged personal
    financial or HR-confidential``: ``StoreReader.sensitive_versions``, the whole version
    judged once by :func:`sensitive_fields`) names. Undecidable (``sensitive`` raised):
    no row is shown.

    The text index marks such a document's fields ``finance`` and ``pii`` since 7aba9f1245
    (``ontology.text_index._sensitive``), but a store whose fields were indexed before
    (a generation, Postgres before the text index's step migration re-marked it) holds
    them without, and a document with no search chunks is not known to ``withheld``. The
    judge's measured false positives (trial balances, bank reconciliations, customer
    statements) are withheld only from a reader without ``pii``: accepted."""
    rows = list(rows)
    if not rows or SENSITIVE <= frozenset(held):
        return rows
    pairs = sorted({(row["object_id"], row["version_handle"]) for row in rows})
    try:
        marked = set(sensitive(pairs))
    except Exception:  # noqa: BLE001 - undecidable: withheld (fail closed)
        return []
    return [row for row in rows if row["version_handle"] not in marked]


__all__ = ["DOCUMENT_TYPES", "FILE_TYPES", "SENSITIVE", "enabled", "forget",
           "handle_withheld", "holds_every", "params", "readable_text", "sensitive_fields",
           "sql", "withheld", "withheld_set"]
