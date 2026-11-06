"""The chunk id runs of each audience family over a store's document-text index
(``chunk_families``), written after the ``text_index`` step (``ontology.text_index``) by
the same build, generation or delta layer alike (``operating.build_steps``).

``plx.documents`` searches the captured text of the sources it is asked about
(``operating.ontology_tools_evidence``): the word index matches every source's chunks,
and the audience of each match's object decides whether it is kept. A search of a source
holding few chunks (Teams messages, SharePoint files beside a mailbox's mail) read every
other source's matches first only to drop them. The runs let it list the matches of its
own sources alone, in the same id order: the same rows, the same gate calls, the same
answer. A store without the table (built before it) reads every match, as before.
"""

from __future__ import annotations

import sqlite3
from typing import Any


CHUNK_FAMILIES_DDL = (
    "CREATE TABLE IF NOT EXISTS chunk_families (lo INTEGER PRIMARY KEY, "
    "hi INTEGER NOT NULL CHECK (hi >= lo), family TEXT NOT NULL)")


# Any later write that could move a chunk out of the run of its object's family empties
# the runs, whatever code makes it, so a search reads every match again (fails closed):
# an object's id or audience changed or deleted (it may come back under another family),
# a chunk added or renumbered, a field moved to another object. A store's runs are used
# only while all of these triggers are in it (``CHUNK_FAMILIES_GUARDS``).
_EMPTY = "BEGIN DELETE FROM chunk_families; END"
CHUNK_FAMILIES_GUARDS = {
    "chunk_families_object_moved":
        "AFTER UPDATE OF object_id, audience_key ON objects WHEN OLD.object_id IS NOT "
        "NEW.object_id OR OLD.audience_key IS NOT NEW.audience_key",
    "chunk_families_object_deleted": "AFTER DELETE ON objects",
    "chunk_families_chunk_added": "AFTER INSERT ON text_chunks",
    "chunk_families_chunk_moved":
        "AFTER UPDATE OF chunk_id, field_id ON text_chunks WHEN OLD.chunk_id IS NOT "
        "NEW.chunk_id OR OLD.field_id IS NOT NEW.field_id",
    "chunk_families_field_moved":
        "AFTER UPDATE OF field_id, object_id ON text_fields WHEN OLD.field_id IS NOT "
        "NEW.field_id OR OLD.object_id IS NOT NEW.object_id",
}
# A chunk whose field or object the store does not hold when the runs are written: its
# run is read by every search (an object inserted later may be of any family).
ANY_FAMILY = "*"


def audience_family(audience_key: str) -> str:
    """The audience family of a key: the part before its first colon (the whole key
    without one), as ``plx.documents`` reads it."""
    return audience_key.split(":", 1)[0]


def store_chunk_families(conn: sqlite3.Connection) -> int | None:
    """Write ``chunk_families`` anew: the chunk id runs (``lo`` to ``hi``) whose chunks'
    fields belong to objects of one audience family, so a word search over the chunks
    of a few families (``plx.documents`` of Teams messages or SharePoint files) reads only
    the matches in their runs instead of every source's. A run holds no chunk of another
    family; a chunk with no field or object is in a run every search reads
    (``ANY_FAMILY``). Written only when every field's chunks are consecutive ids, as the
    text index inserts them; else the table is left empty and searches read every match,
    as before. The guards (``CHUNK_FAMILIES_GUARDS``) are added with them: a later step of
    this build, or any later write, that could change a chunk's family empties them. The
    number of runs written, or None when left empty."""
    conn.execute(CHUNK_FAMILIES_DDL)
    conn.execute("DELETE FROM chunk_families")
    for name, when in CHUNK_FAMILIES_GUARDS.items():
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {when} {_EMPTY}")
    runs: list[list[Any]] = []
    previous_hi = 0
    for lo, hi, count, audience_key in conn.execute(
            "SELECT g.lo, g.hi, g.n, o.audience_key FROM (SELECT field_id, MIN(chunk_id) AS lo, "
            "MAX(chunk_id) AS hi, COUNT(*) AS n FROM text_chunks GROUP BY field_id) g "
            "LEFT JOIN text_fields fd ON fd.field_id = g.field_id "
            "LEFT JOIN objects o ON o.object_id = fd.object_id ORDER BY g.lo"):
        if count != hi - lo + 1 or lo <= previous_hi:
            conn.execute("DELETE FROM chunk_families")
            return None   # a field's chunks are not one run of ids: nothing is narrowed
        previous_hi = hi
        family = ANY_FAMILY if audience_key is None else audience_family(audience_key)
        if runs and runs[-1][2] == family:
            runs[-1][1] = hi
        else:
            runs.append([lo, hi, family])
    conn.executemany("INSERT INTO chunk_families (lo, hi, family) VALUES (?, ?, ?)", runs)
    return len(runs)


def runs_guarded(conn: sqlite3.Connection) -> bool:
    """Whether every guard of the runs is in the store (``CHUNK_FAMILIES_GUARDS``): only
    then may a search trust them; a store copied without its triggers reads every match."""
    names = sorted(CHUNK_FAMILIES_GUARDS)
    found = conn.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name IN "
        f"({','.join('?' * len(names))})", names).fetchone()[0]
    return found == len(names)


__all__ = ["ANY_FAMILY", "CHUNK_FAMILIES_DDL", "CHUNK_FAMILIES_GUARDS", "audience_family",
           "runs_guarded", "store_chunk_families"]
