"""The mailbox owners' addresses the mail flags (``owner_role``, ``owner_replied_at``) are
decided by: every address of each mailbox's owner (UPN and proxy addresses), read-only from
the mail capture's ``mailboxes``.

A run that cannot read the capture (a whole build on a Spark copy of the database, 11-23:
``owner_role`` null on 3.27M of 3.27M mails, and every "who is waiting on whom", reply-time
and Team waiting read empty) takes the addresses the last run that could read it kept in
the database (``biz.mailbox_owners``). None kept: a build with
``PLX_BIZ_OWNERS_REQUIRED=1`` stops rather than write nulls as if they were answers; any
other run leaves the places unknown (null, never guessed).

Part of the ``emails`` view's code fingerprint (``business_views_code.EXTERNAL``)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

TABLE = "mailbox_owners"


def capture_db() -> Path:
    """The mail capture the flags read (``PLX_STATE_DIR``, the host's state directory)."""
    return Path(os.environ.get("PLX_STATE_DIR") or "/data/plx-state") / "mail-capture" \
        / "capture.db"


def mailbox_addresses(db: Any) -> dict[str, list[str]] | None:
    """Per mailbox id (lower-case) every address of its owner (UPN and proxy addresses,
    lower-case), read-only from the mail capture ``db``; None when it cannot be read (the
    owner's place and replies are then unknown: null, never guessed)."""
    if not Path(db).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return None
    out: dict[str, set[str]] = {}
    try:
        for box, upn, identities in conn.execute(
                "SELECT mailbox_id, upn, identities FROM mailboxes"):
            try:
                listed = json.loads(identities or "[]")
            except (TypeError, ValueError):
                listed = []
            for a in [upn, *(listed if isinstance(listed, list) else [])]:
                if not isinstance(a, str):
                    continue
                a = a.strip().lower()
                a = a[5:] if a.startswith("smtp:") else a
                if "@" in a and ":" not in a and box:
                    out.setdefault(str(box).strip().lower(), set()).add(a)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {box: sorted(addrs) for box, addrs in out.items()}


def _kept(write: Any) -> dict[str, list[str]] | None:
    """The addresses an earlier run kept: this run's schema, then the served one; None when
    there are none (never run, or the table is absent)."""
    from operating.pg import business_views as bv
    for schema in dict.fromkeys((bv.SCHEMA, bv.SERVED_SCHEMA)):
        try:
            with write.transaction():
                rows = write.execute(f"SELECT mailbox, addrs FROM {schema}.{TABLE}").fetchall()
        except Exception:  # noqa: BLE001 - no table there: nothing kept there
            continue
        if rows:
            return {str(m): sorted(a) for m, a in rows}
    return None


def _keep(write: Any, boxes: dict[str, list[str]]) -> None:
    """Keep the addresses just read from the capture (replacing the kept ones) in this
    run's schema, for a later run that cannot read the capture."""
    from operating.pg import business_views as bv
    with write.transaction():
        write.execute(f"CREATE TABLE IF NOT EXISTS {bv.SCHEMA}.{TABLE} "
                      "(mailbox text PRIMARY KEY, addrs text[] NOT NULL)")
        write.execute(f"TRUNCATE {bv.SCHEMA}.{TABLE}")
        with write.cursor().copy(f"COPY {bv.SCHEMA}.{TABLE} (mailbox, addrs) FROM STDIN") as cp:
            for box, addrs in boxes.items():
                cp.write_row((box, addrs))


def owners(write: Any, db: Any, *, build: bool) -> tuple[dict[str, list[str]] | None, str]:
    """``(addresses, read from)``: from the capture ``db`` (kept for later runs), else the
    kept ones (``stored``), else None (``none``). A ``build`` with none and
    ``PLX_BIZ_OWNERS_REQUIRED=1`` raises (its transaction rolls back: the served table
    stands)."""
    boxes = mailbox_addresses(db)
    if boxes is not None:
        if boxes:
            _keep(write, boxes)
        return boxes, "capture"
    boxes = _kept(write)
    if boxes:
        return boxes, "stored"
    if build and os.environ.get("PLX_BIZ_OWNERS_REQUIRED", "") == "1":
        raise RuntimeError("the mailbox owners' addresses cannot be read (the mail capture "
                           "is unreadable and no earlier run kept them): owner_role would "
                           "be null for every mail, build stopped")
    return None, "none"
