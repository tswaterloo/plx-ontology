"""What a Teams message itself says (``biz.teams_messages.said``), read from the message's own
document text (``ontology.text_fields`` / ``text_chunks``: the whole text, never a preview).

The view had no text and its author column held the sender's raw JSON (ASK-GENIE spec 20 #2,
KNOWN-GAPS D1). ``author`` and ``author_kind`` are read from that JSON in SQL
(``business_comms.TEAMS``); the text needs the chunks' zlib bodies, so it is set here, by the
build (``business_comms.fill``) and by every refresh of the rows it reads again
(``refresh_changed``): one pass over the table's rows, chunks read in batches, each field
reassembled in order (a chunk lays its text at its offset, as ``comms_search`` opens it).

Markings: a text field carrying a marking is NOT read into ``said`` (the row's own markings and
audience are untouched, and the guarded view decides who reads a row), so a marked text stays
reachable only where search shows it. A message with no text field (a system event: a member
joined, a call started) keeps ``said`` NULL. Nothing is skipped silently: the counts of each
outcome are returned and the build records them in its state notes."""
from __future__ import annotations

import logging
import zlib
from typing import Any

LOG = logging.getLogger("operating.pg.teams_text")
KIND = "teams_message"     # ``ontology.text_fields.document_kind`` of a Teams message's body
BATCH = 20_000             # text fields per read of their chunks


def _assemble(chunks: list[tuple[int, bytes]]) -> str:
    text = ""
    for start, body in chunks:
        text = text[:start] + zlib.decompress(bytes(body)).decode("utf-8", "replace")
    return text


def _conn(write: Any) -> Any:
    """The connection of ``write`` (a connection, or a cursor of one: the migrations' writers)."""
    return write if hasattr(write, "cursor") else write.connection


def _teams_table(write: Any) -> str | None:
    from operating.pg import business_views as bv
    table = f"{bv.SCHEMA}.teams_messages"
    return None if write.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None \
        else table


def _via_temp(write: Any, name: str, ddl: str, rows: Any, where: str) -> dict[str, int] | None:
    table = _teams_table(write)
    if table is None or not rows:
        return None
    write.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")
    write.execute(f"CREATE TEMP TABLE {name} ({ddl} PRIMARY KEY)")
    with _conn(write).cursor().copy(f"COPY {name} FROM STDIN") as cp:
        for r in rows:
            cp.write_row((r,))
    try:
        return apply(write, table, where)
    finally:
        write.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")


def apply_fields(write: Any, field_ids: Any) -> dict[str, int] | None:
    """``apply`` for the messages whose text fields ``field_ids`` name: called, in the same
    transaction, by every writer that changes ``ontology.text_fields.markings`` after the build
    (``text_remark``, ``ontology_migrate``), so a message whose text is marked later loses its
    ``said`` at once (and one whose marks are undone gets it back). None: no ids, or no
    teams_messages table yet (nothing stored, nothing to leak)."""
    ids = sorted({int(f) for f in field_ids})
    return _via_temp(write, "tm_fields", "field_id bigint", ids,
                     "t.handle IN (SELECT f.version_handle FROM ontology.text_fields f JOIN "
                     "pg_temp.tm_fields x ON x.field_id = f.field_id)")


def apply_handles(write: Any, handles: Any) -> dict[str, int] | None:
    """``apply`` for the messages with these handles (a document's version handle), called
    after a writer deleted or copied their text fields (``ontology_migrate`` drops the fields
    of a removed document; ``ontology_delta`` copies a layer's): a text that is gone, or
    that came back marked, leaves no ``said``; a text that came back open is read again."""
    rows = sorted({str(h) for h in handles})
    return _via_temp(write, "tm_handles", "handle text", rows,
                     "t.handle IN (SELECT handle FROM pg_temp.tm_handles)")


def apply(write: Any, table: str, where: str = "true") -> dict[str, int]:
    """Set ``said`` on the rows of ``table`` matching ``where`` from their text fields.
    Returns {rows, with_text, marked_text_withheld, no_text_field, undecodable}."""
    got = {"rows": 0, "with_text": 0, "marked_text_withheld": 0, "no_text_field": 0,
           "undecodable": 0}
    fields = write.execute(
        f"SELECT t.handle, fd.field_id, fd.markings = '[]' FROM {table} t LEFT JOIN "
        "ontology.text_fields fd ON fd.version_handle = t.handle AND fd.object_id = "
        f"t.object_ids[1] AND fd.document_kind = %s WHERE {where}", (KIND,)).fetchall()
    got["rows"] = len(fields)
    open_fields = [(h, f) for h, f, ok in fields if f is not None and ok]
    got["marked_text_withheld"] = sum(1 for _, f, ok in fields if f is not None and not ok)
    got["no_text_field"] = sum(1 for _, f, _ok in fields if f is None)
    write.execute("DROP TABLE IF EXISTS pg_temp.tm_said")
    write.execute("CREATE TEMP TABLE tm_said (handle text PRIMARY KEY, said text)")
    # every row read is set, so a text that is now marked or gone leaves no stale ``said``
    have = {h for h, _ in open_fields}
    with _conn(write).cursor().copy("COPY tm_said (handle, said) FROM STDIN") as cp:
        for h, _f, _ok in fields:
            if h not in have:
                cp.write_row((h, None))
    for lo in range(0, len(open_fields), BATCH):
        part = open_fields[lo:lo + BATCH]
        by_field: dict[int, list[tuple[int, bytes]]] = {}
        for fid, start, body in write.execute(
                "SELECT field_id, start, body FROM ontology.text_chunks WHERE field_id = "
                "ANY(%s) ORDER BY field_id, start", ([f for _, f in part],)):
            by_field.setdefault(fid, []).append((start, body))
        rows = []
        for handle, fid in part:
            try:
                text = _assemble(by_field.get(fid, []))
            except zlib.error as exc:
                got["undecodable"] += 1
                LOG.warning("teams text of field %s cannot be decoded: %s", fid, exc)
                rows.append((handle, None))
                continue
            rows.append((handle, text if text.strip() else None))
        with _conn(write).cursor().copy("COPY tm_said (handle, said) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        got["with_text"] += sum(1 for r in rows if r[1] is not None)
    write.execute(f"UPDATE {table} t SET said = s.said FROM tm_said s WHERE s.handle = t.handle")
    write.execute("DROP TABLE pg_temp.tm_said")
    return got
