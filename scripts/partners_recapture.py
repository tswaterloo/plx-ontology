"""The re-capture of what the mail capture dropped for the two excluded people (owner
decision 2026-11-21: their data is kept, the owner's alone). Counts only, never an id or
address on output.

  python3 -m scripts.partners_recapture plan  --state-dir STATE
  python3 -m scripts.partners_recapture apply --state-dir STATE --backup DIR [--dsn DSN]
  python3 -m scripts.partners_recapture undo  --state-dir STATE --backup DIR

``plan``: what the capture still holds back: the remembered withheld messages
(``people_withheld``: no mailbox stores a copy of one while it is remembered), the folders
whose last re-list was explained by the exclusion (``folder_coverage`` ``people_excluded``
copies), the attachments marked excluded, and the guards below.

``apply`` (only with the captures already including the people, ``people_capture.ENV`` =
``include`` in the ingest environment, and the purge timer removed): in one transaction of
the capture store, writes every row it changes to ``DIR`` first (0600), then forgets the
withheld messages, sets those folders ``short`` with no re-list time (the capture re-lists
them whole on its next runs, ``MailCapture._relist_short``, within its own per-folder
bounds and Graph's per-mailbox limits) and returns the excluded attachments to deferred.
Refuses unless ``/data`` keeps ``MIN_FREE_GB`` free after ``NEED_GB`` (an estimate of what
the copies re-read take, ``BLOB_KB`` per message) and the standby's replay lag is under
``MAX_LAG_GB`` (the WAL budget): the capture's own low-disk stop is the backstop.

``undo``: restores the rows ``apply`` wrote to ``DIR`` (the withheld keys, the folders'
coverage, the attachments' status). What the capture stored meanwhile stays: it is the
owner's alone through the people set either way."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Sequence

MIN_FREE_GB = 75
BLOB_KB = 83            # stored bytes per captured message, measured 11-21 (275.8 GB / 3.30M)
MAX_LAG_GB = 8
from integrations.capture_mail import (ATTACHMENT_DEFERRED as DEFERRED,  # noqa: E402
                                      ATTACHMENT_EXCLUDED as EXCLUDED)
BACKUP = "partners-recapture-backup.json"


def _db(state: Path, *, write: bool) -> sqlite3.Connection:
    path = state / "mail-capture" / "capture.db"
    if not path.is_file():
        raise SystemExit(json.dumps({"ok": False, "error": "mail_capture_absent"}))
    if write:
        return sqlite3.connect(path, timeout=60)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def _has(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone() is not None


def counts(db: sqlite3.Connection) -> dict[str, Any]:
    withheld = db.execute("SELECT count(*) FROM people_withheld").fetchone()[0] \
        if _has(db, "people_withheld") else 0
    folders, copies = db.execute(
        "SELECT count(*), coalesce(sum(json_extract(detail, '$.people_excluded')), 0) FROM "
        "folder_coverage WHERE json_extract(detail, '$.people_excluded') > 0").fetchone() \
        if _has(db, "folder_coverage") else (0, 0)
    attachments = db.execute("SELECT count(*) FROM attachments WHERE content_status = ?",
                             (EXCLUDED,)).fetchone()[0]
    return {"withheld_messages": withheld, "folders": folders, "copies": int(copies or 0),
            "attachments_excluded": attachments}


def guards(state: Path, copies: int, dsn: str | None) -> dict[str, Any]:
    free = shutil.disk_usage(state).free / 2**30
    need = copies * BLOB_KB / 2**20
    out: dict[str, Any] = {"free_gb": round(free, 1), "need_gb": round(need, 1),
                           "min_free_gb": MIN_FREE_GB, "disk_ok": free - need >= MIN_FREE_GB,
                           "lag_gb": None, "lag_ok": False,
                           "include_on": os.environ.get("PLX_CAPTURE_PEOPLE") == "include"}
    try:
        import psycopg
        with psycopg.connect(dsn or "postgresql:///plx", connect_timeout=10) as pg:
            rows = pg.execute("SELECT coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), "
                              "replay_lsn)), 0) FROM pg_stat_replication WHERE replay_lsn "
                              "IS NOT NULL").fetchone()
        out["lag_gb"] = round(float(rows[0]) / 2**30, 2)
        out["lag_ok"] = out["lag_gb"] < MAX_LAG_GB
    except Exception as exc:  # noqa: BLE001 - an unreadable lag refuses
        out["lag_error"] = type(exc).__name__
    return out


def apply(state: Path, backup: Path, dsn: str | None) -> dict[str, Any]:
    db = _db(state, write=True)
    try:
        found = counts(db)
        checks = guards(state, found["copies"], dsn)
        refused = [k for k in ("include_on", "disk_ok", "lag_ok") if not checks[k]]
        if refused:
            return {"ok": False, "refused": refused, **found, **checks}
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = backup / BACKUP
        if target.exists():
            return {"ok": False, "refused": ["backup_exists"], **found}
        db.execute("BEGIN IMMEDIATE")
        saved = {
            "people_withheld": db.execute("SELECT message_key, withheld_at FROM "
                                          "people_withheld").fetchall()
            if _has(db, "people_withheld") else [],
            "folder_coverage": db.execute(
                "SELECT mailbox_id, folder_id, status, reason, last_relist_at FROM "
                "folder_coverage WHERE json_extract(detail, '$.people_excluded') > 0"
            ).fetchall() if _has(db, "folder_coverage") else [],
            "attachments": db.execute("SELECT message_handle, attachment_id FROM attachments "
                                      "WHERE content_status = ?", (EXCLUDED,)).fetchall()}
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(saved, fh)
            fh.flush()
            os.fsync(fh.fileno())
        if saved["people_withheld"]:
            db.execute("DELETE FROM people_withheld")
        if saved["folder_coverage"]:
            db.executemany("UPDATE folder_coverage SET status = 'short', reason = "
                           "'awaiting_relist', last_relist_at = NULL WHERE mailbox_id = ? AND "
                           "folder_id = ?", [(r[0], r[1]) for r in saved["folder_coverage"]])
        db.executemany("UPDATE attachments SET content_status = ? WHERE message_handle = ? "
                       "AND attachment_id = ? AND content_status = ?",
                       [(DEFERRED, h, a, EXCLUDED) for h, a in saved["attachments"]])
        db.execute("COMMIT")
        return {"ok": True, **found, **checks, "backup": str(target)}
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def undo(state: Path, backup: Path) -> dict[str, Any]:
    saved = json.loads((backup / BACKUP).read_text())
    db = _db(state, write=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        if saved["people_withheld"]:
            db.execute("CREATE TABLE IF NOT EXISTS people_withheld (message_key TEXT PRIMARY "
                       "KEY, withheld_at TEXT NOT NULL)")
            db.executemany("INSERT OR IGNORE INTO people_withheld VALUES (?, ?)",
                           saved["people_withheld"])
        if saved["folder_coverage"]:
            db.executemany("UPDATE folder_coverage SET status = ?, reason = ?, "
                           "last_relist_at = ? WHERE mailbox_id = ? AND folder_id = ?",
                           [(r[2], r[3], r[4], r[0], r[1]) for r in saved["folder_coverage"]])
        db.executemany("UPDATE attachments SET content_status = ? WHERE message_handle = ? "
                       "AND attachment_id = ? AND content_status = ?",
                       [(EXCLUDED, h, a, DEFERRED) for h, a in saved["attachments"]])
        db.execute("COMMIT")
    finally:
        db.close()
    return {"ok": True, "restored": {k: len(v) for k, v in saved.items()}}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m scripts.partners_recapture")
    ap.add_argument("command", choices=("plan", "apply", "undo"))
    ap.add_argument("--state-dir", required=True, type=Path)
    ap.add_argument("--backup", type=Path)
    ap.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN"))
    args = ap.parse_args(argv)
    if args.command != "plan" and args.backup is None:
        ap.error("--backup is required")
    if args.command == "plan":
        db = _db(args.state_dir, write=False)
        try:
            found = counts(db)
        finally:
            db.close()
        out = {"ok": True, **found, **guards(args.state_dir, found["copies"], args.dsn)}
    elif args.command == "apply":
        out = apply(args.state_dir, args.backup, args.dsn)
    else:
        out = undo(args.state_dir, args.backup)
    print(json.dumps(out, sort_keys=True))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
