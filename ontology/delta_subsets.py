"""Small capture files holding just the records a delta cycle follows (split out of
``ontology.delta``; same names, re-exported there)."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology.delta_io import _chunks, _ro


def _copy_schema(src: sqlite3.Connection, dst: sqlite3.Connection, schema: str = "main") -> None:
    for row in src.execute(f"SELECT type, name, sql FROM {schema}.sqlite_master WHERE sql IS NOT "
                           "NULL AND type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
                           "ORDER BY type DESC"):
        dst.execute(row["sql"])


def _attach_copy(dst: sqlite3.Connection, live: Path) -> None:
    dst.execute("ATTACH DATABASE ? AS live", (f"file:{live}?mode=ro",))


def mirror_subset(live: Path, out: Path, keys: Iterable[tuple[str, str]]) -> None:
    """A mirror file holding only these records (every retained version and removal) and
    every capture, relation and label row: ``MirrorCaptures`` reads it as the mirror."""
    out.unlink(missing_ok=True)
    src = _ro(live)
    dst = sqlite3.connect(out, isolation_level=None)
    dst.row_factory = sqlite3.Row
    try:
        _copy_schema(src, dst)
        src.close()
        _attach_copy(dst, live)
        dst.execute("BEGIN")
        keyed = ("row_current", "row_version", "row_removal")
        tables = [r[0] for r in dst.execute("SELECT name FROM live.sqlite_master WHERE "
                                            "type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table in keyed or table in ("identifier_index", "stage_row", "stage_seen",
                                           "absence_seen", "change_capture",
                                           "relation_change", "sync_progress"):
                continue
            dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
        dst.execute("CREATE TEMP TABLE want (relation TEXT, key_json TEXT)")
        dst.executemany("INSERT INTO want VALUES (?, ?)", list(keys))
        for table in keyed:
            dst.execute(f"INSERT INTO main.{table} SELECT t.* FROM want w JOIN live.{table} t "
                        "ON t.relation = w.relation AND t.key_json = w.key_json")
        dst.execute("COMMIT")
    finally:
        dst.close()


def mail_subset(live_state: Path, out_state: Path, keys: Iterable[tuple[str, str]]) -> None:
    """A mail capture directory holding only these messages (every version, their
    attachments and attachment texts); the blobs are the live capture's (read-only)."""
    live = live_state / "mail-capture" / "capture.db"
    root = out_state / "mail-capture"
    root.mkdir(parents=True, exist_ok=True)
    blobs = root / "blobs"
    if not blobs.exists():
        blobs.symlink_to((live_state / "mail-capture" / "blobs").resolve())
    db = root / "capture.db"
    db.unlink(missing_ok=True)
    src = _ro(live)
    dst = sqlite3.connect(db, isolation_level=None)
    try:
        _copy_schema(src, dst)
        src.close()
        _attach_copy(dst, live)
        dst.execute("BEGIN")
        for table in ("meta", "folders", "mailboxes", "mailbox_folders"):
            dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
        dst.execute("CREATE TEMP TABLE want (mailbox_id TEXT, message_id TEXT)")
        dst.executemany("INSERT INTO want VALUES (?, ?)", list(keys))
        dst.execute("INSERT INTO main.messages SELECT m.* FROM want w JOIN live.messages m "
                    "ON m.mailbox_id = w.mailbox_id AND m.message_id = w.message_id")
        dst.execute("INSERT INTO main.attachments SELECT a.* FROM live.attachments a WHERE "
                    "a.message_handle IN (SELECT l1_handle FROM main.messages)")
        dst.execute("INSERT INTO main.attachment_text_versions SELECT t.* FROM "
                    "live.attachment_text_versions t WHERE t.l1_handle IN "
                    "(SELECT l1_handle FROM main.attachments)")
        dst.execute("INSERT OR IGNORE INTO main.blobs SELECT b.* FROM live.blobs b WHERE "
                    "b.sha256 IN (SELECT body_sha256 FROM main.messages UNION SELECT "
                    "content_sha256 FROM main.attachments UNION SELECT text_sha256 FROM "
                    "main.attachments UNION SELECT text_sha256 FROM main.attachment_text_versions)")
        dst.execute("COMMIT")
    finally:
        dst.close()


def sharepoint_subset(live_state: Path, out_state: Path, keys: Iterable[tuple[str, ...]]
                      ) -> None:
    """The SharePoint drive capture holding only these items (every version, their blob
    rows, every root and permission row; the blobs are the live capture's, read-only) and
    the list-item capture holding only these list items."""
    from ontology.project import SharePointCaptures
    drive_live, lists_live = SharePointCaptures.stores(live_state)
    drive_out, lists_out = SharePointCaptures.stores(out_state)
    keys = list(keys)
    items = [(k[1], k[2]) for k in keys if k[0] == "item"]
    listed = [(k[0], k[1]) for k in keys if k[0] != "item"]
    if drive_live.is_file():
        drive_out.parent.mkdir(parents=True, exist_ok=True)
        blobs = drive_out.parent / "blobs"
        if not blobs.exists():
            blobs.symlink_to((drive_live.parent / "blobs").resolve())
        drive_out.unlink(missing_ok=True)
        with contextlib.closing(_ro(drive_live)) as src, \
                contextlib.closing(sqlite3.connect(drive_out, isolation_level=None)) as dst:
            _copy_schema(src, dst)
            dst.execute("ATTACH DATABASE ? AS live", (f"file:{drive_live}?mode=ro",))
            dst.execute("BEGIN")
            for table in [r[0] for r in dst.execute(
                    "SELECT name FROM live.sqlite_master WHERE type = 'table' AND name NOT "
                    "LIKE 'sqlite_%'")]:
                if table in ("items", "blobs", "item_events", "walked_folders"):
                    continue
                dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
            dst.execute("CREATE TEMP TABLE want (drive_id TEXT, item_id TEXT)")
            dst.executemany("INSERT INTO want VALUES (?, ?)", items)
            dst.execute("INSERT INTO main.items SELECT i.* FROM want w JOIN live.items i ON "
                        "i.drive_id = w.drive_id AND i.item_id = w.item_id")
            dst.execute("INSERT OR IGNORE INTO main.blobs SELECT b.* FROM live.blobs b WHERE "
                        "b.sha256 IN (SELECT content_sha256 FROM main.items UNION SELECT "
                        "text_sha256 FROM main.items)")
            dst.execute("COMMIT")
    if lists_live.is_file() and listed:
        lists_out.parent.mkdir(parents=True, exist_ok=True)
        store_subset(lists_live, lists_out, listed)


def store_subset(live: Path, out: Path, keys: Iterable[tuple[str, str]]) -> None:
    """A capture-store file holding only these records' versions and removals, with
    every run, mark, ACL and meta row."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    src = _ro(live)
    dst = sqlite3.connect(out, isolation_level=None)
    try:
        _copy_schema(src, dst)
        src.close()
        _attach_copy(dst, live)
        dst.execute("BEGIN")
        tables = [r[0] for r in dst.execute("SELECT name FROM live.sqlite_master WHERE "
                                            "type = 'table' AND name NOT LIKE 'sqlite_%'")]
        dst.execute("CREATE TEMP TABLE want (kind TEXT, path TEXT, PRIMARY KEY (kind, path))")
        dst.executemany("INSERT OR IGNORE INTO want VALUES (?, ?)", list(keys))
        for table in tables:
            if table in ("versions", "removals"):
                dst.execute(f"INSERT INTO main.{table} SELECT t.* FROM want w JOIN live.{table} t "
                            "ON t.kind = w.kind AND t.path = w.path")
            else:
                dst.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")
        dst.execute("COMMIT")
    finally:
        dst.close()
