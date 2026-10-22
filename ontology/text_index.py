"""The word index of captured document text ``plx.find`` searches (store layout v8).

A ``build_store`` ``post_project`` step (``operating/build_steps.py``): every text field of
every captured document version the projection holds (a mail's subject and body, a file's
or an attachment's name and extracted text, by page; ``ontology.mentions.Document``) is
stored once per projected object of that version in ``text_fields``, its text in
overlapping chunks (``text_chunks``, zlib) and their words (``chunk_text``, contentless
FTS5), so a phrase no identifier and no value carries is found by the words it holds and
cited to its page and field. Values carry their own words (``value_text``, kept by
triggers: ``ontology/store_ddl.py``); a field the projection already holds as a text value
of the same version and path (an Email's subject) is not stored again.

Permissions are stored, never decided here: each field carries the markings a reader must
hold (its version's and those of the property its path states, ``Projector._marks_for_path``)
and names the version it reads; ``plx.find`` joins that version and applies the reader's
audience, markings, time and item ACL (``operating/ontology_tools.py``). A field whose
markings cannot be established (an unknown binding, a marking the store does not know) or
whose path is not a citable field path is skipped and counted, never stored unmarked.

Incremental: the step runs on a seeded copy of the previous generation and rewrites only
the fields whose text, markings or document facts changed; a field no document states any
more is deleted, with its words (a contentless table forgets a row only when told them).
Its report holds totals only, so a cold and an incremental build of the same captures
report alike.

Vector search: no embedding model is available offline in this deployment (stdlib only),
so the index is words only and the report says so (``vector``); a semantic index would be
a new step and a new store layout.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from contextlib import closing
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology.mentions import Document, _source_documents, cited_page
from ontology.store import StoreError, markings_text
from ontology.store_ddl import TEXT_TOKENIZER

FORMAT = "plx-text-index/v1"
CHUNK_CHARS = 2000      # characters per chunk (a search ANDs its words within one chunk)
CHUNK_OVERLAP = 200     # characters two neighbouring chunks share, so a phrase is whole once
FIELD_CAP = 200_000     # characters of one field indexed; the rest is counted as truncated
VECTOR = {"state": "unavailable", "reason": "no_offline_embedding_model"}
# $defs/field_path of the tool contract: a field a reader is cited to.
_FIELD_PATH = re.compile(r"^row(?:\.[A-Za-z_][A-Za-z0-9_]*){1,3}$")
_SPACE = re.compile(r"\s+")
_STORED_TEXT = ("SELECT 1 FROM value_sources s JOIN value_cells c ON c.src = s.src "
                "JOIN value_fields f ON f.field = c.field JOIN value_kinds k ON k.kind = c.kind "
                "WHERE s.object_id = ? AND s.l1_handle = ? AND f.field_path = ? "
                "AND k.value_type = 'text' AND c.value IS NOT NULL LIMIT 1")


def chunks(text: str) -> list[tuple[int, str]]:
    """(start, text) chunks of ``text``: at most ``CHUNK_CHARS`` each, cut at whitespace
    where there is some in the second half, each starting at a word within the last
    ``CHUNK_OVERLAP`` characters of the one before; blank chunks are dropped."""
    out: list[tuple[int, str]] = []
    start, size = 0, len(text)
    while start < size:
        end = min(size, start + CHUNK_CHARS)
        if end < size:
            cut = max(text.rfind(c, start + CHUNK_CHARS // 2, end) for c in " \t\r\n\f")
            end = cut if cut > start else end
        if text[start:end].strip():
            out.append((start, text[start:end]))
        if end >= size:
            break
        space = _SPACE.search(text, max(start + 1, end - CHUNK_OVERLAP), end)
        start = space.end() if space is not None and space.end() < end else end
    return out


def chunk_text(body: bytes) -> str:
    return zlib.decompress(body).decode("utf-8")


def query_terms(words: Sequence[str]) -> list[str] | None:
    """The index's own word for each of ``words`` (folded by the index's tokenizer), as a
    search names it; None when one of them is not exactly one indexed word (the tokenizer
    splits it or drops it: a search for it would fail or find nothing, and either is no
    proof the text is absent)."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE VIRTUAL TABLE w USING fts5(x, tokenize='{TEXT_TOKENIZER}')")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5vocab(w, 'instance')")
        conn.executemany("INSERT INTO w (rowid, x) VALUES (?, ?)", enumerate(words, 1))
        terms: dict[int, list[str]] = {}
        for doc, term in conn.execute("SELECT doc, term FROM t"):
            terms.setdefault(doc, []).append(term)
    out = [terms.get(index, []) for index in range(1, len(words) + 1)]
    return [found[0] for found in out] if all(len(found) == 1 for found in out) else None


def _field_marks(projector: Any, version: Mapping[str, Any], field_path: str) -> str | None:
    """The markings a reader of the field must hold: its version's and the properties'
    its path states on the version's binding; None when they cannot be established."""
    marks_for = getattr(projector, "_marks_for_path", None)
    if marks_for is None:
        return None
    try:
        return markings_text(set(json.loads(version["markings"]))
                             | set(marks_for(version["binding_id"], field_path[len("row."):])))
    except (KeyError, StoreError, TypeError, ValueError):
        return None


def _fields(document: Document) -> Iterator[tuple[str, str, str]]:
    """(citation, field_path, text) of the document's own fields (its handle or one of its
    pages; a field cited on another handle, as an attachment's name in its mail, is that
    document's)."""
    for field in document.fields:
        base = field.citation
        if base != document.citation:
            base, page = cited_page(field.citation)
            if page is None:
                continue
        if base == document.citation:
            yield field.citation, field.field_path, field.text


def _delete_field(conn: sqlite3.Connection, field_id: int) -> None:
    for chunk_id, body in conn.execute(
            "SELECT chunk_id, body FROM text_chunks WHERE field_id = ?", (field_id,)).fetchall():
        conn.execute("INSERT INTO chunk_text (chunk_text, rowid, body) VALUES ('delete', ?, ?)",
                     (chunk_id, chunk_text(body)))
    conn.execute("DELETE FROM text_chunks WHERE field_id = ?", (field_id,))
    conn.execute("DELETE FROM text_fields WHERE field_id = ?", (field_id,))


def store_text_index(builder: Any, projector: Any, *,
                     documents: Iterable[Document] | None = None) -> dict[str, Any]:
    """Bring the generation's document-text index to ``documents`` (else every capture
    source's ``documents()``, as ``ontology.mentions.store_mention_links`` reads them)."""
    conn = builder.connection
    if documents is None:
        documents = _source_documents(projector)
    counts = {"documents": 0, "documents_not_projected": 0, "fields_stored_as_values": 0,
              "fields_unmarked": 0, "fields_unciteable": 0, "fields_truncated": 0}
    previous = {(row[1], row[2], row[3]): (row[0], tuple(row[4:]))
                for row in conn.execute(
                    "SELECT field_id, citation, field_path, object_id, version_handle, "
                    "document_kind, sender_class, markings, text_digest FROM text_fields")}
    seen: set[tuple[str, str, str]] = set()
    for document in documents:
        versions = conn.execute(
            "SELECT v.object_id, v.markings, o.binding_id FROM object_versions v "
            "JOIN objects o ON o.object_id = v.object_id WHERE v.l1_handle = ? "
            "ORDER BY v.object_id", (document.citation,)).fetchall()
        if not versions:
            counts["documents_not_projected"] += 1
            continue
        counts["documents"] += 1
        for citation, field_path, text in _fields(document):
            if not isinstance(text, str) or not text.strip():
                continue
            if not _FIELD_PATH.match(field_path):
                counts["fields_unciteable"] += 1
                continue
            body = text[:FIELD_CAP]
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            for object_id, markings, binding_id in versions:
                key = (citation, field_path, object_id)
                if key in seen:
                    continue
                if citation == document.citation and conn.execute(
                        _STORED_TEXT, (object_id, citation, field_path)).fetchone():
                    counts["fields_stored_as_values"] += 1
                    continue
                marks = _field_marks(projector, {"markings": markings,
                                                 "binding_id": binding_id}, field_path)
                if marks is None:
                    counts["fields_unmarked"] += 1
                    continue
                seen.add(key)
                counts["fields_truncated"] += len(text) > FIELD_CAP
                facts = (document.citation, document.kind, document.sender_class, marks, digest)
                held = previous.get(key)
                if held is not None and held[1] == facts:
                    continue
                if held is not None:
                    _delete_field(conn, held[0])
                field_id = conn.execute(
                    "INSERT INTO text_fields (object_id, citation, field_path, version_handle, "
                    "document_kind, sender_class, markings, text_digest) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?)", (object_id, citation, field_path,
                                                 *facts)).lastrowid
                for start, piece in chunks(body):
                    chunk_id = conn.execute(
                        "INSERT INTO text_chunks (field_id, start, body) VALUES (?, ?, ?)",
                        (field_id, start, zlib.compress(piece.encode("utf-8"), 6))).lastrowid
                    conn.execute("INSERT INTO chunk_text (rowid, body) VALUES (?, ?)",
                                 (chunk_id, piece))
    for key, (field_id, _facts) in previous.items():
        if key not in seen:
            _delete_field(conn, field_id)
    fields, pieces, stored = conn.execute(
        "SELECT (SELECT COUNT(*) FROM text_fields), COUNT(*), COALESCE(SUM(length(body)), 0) "
        "FROM text_chunks").fetchone()
    return {"format": FORMAT, **counts, "fields": fields, "chunks": pieces,
            "chunk_bytes": stored, "vector": dict(VECTOR)}


__all__ = ["CHUNK_CHARS", "CHUNK_OVERLAP", "FIELD_CAP", "FORMAT", "VECTOR", "chunk_text",
           "chunks", "query_terms", "store_text_index"]
