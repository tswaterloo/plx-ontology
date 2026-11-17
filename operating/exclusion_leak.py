"""The people-exclusion leak scan: counts what is still held of an excluded person's mail
and chats where readers reach it (Postgres's served ontology and the search index), so a
leak raises an alarm (``ops_alarms`` check ``exclusion_leak``, critical) instead of
waiting for the daily purge (audit 11-15 row 17).

    python3 -m operating.exclusion_leak run --state-dir STATE [--dsn DSN]

Run hourly (``plx-exclusion-leak.timer``). Read-only. It counts:

* **withheld mail still served**: the handles the mail capture knows it must not serve
  (every stored copy of a message it remembered as withheld, the handles a capture purge
  journalled, the Graph re-read's matches and the carried-mail scan's attachments: what
  ``operating.mail_exclusion_purge purge`` removes) and the Postgres rows, search chunks
  and entities citing them, through that purge's own dry run (a transaction rolled
  back);
* **an excluded person's mailbox**: served versions whose handle names an excluded
  object id as its mailbox (``graph-mail://<id>/``) and the search chunks of those
  records;
* **an excluded person's 1:1 chats**: served Teams chat versions whose chat id holds an
  excluded object id, and their search chunks;
* beside them, **the capture store** (not served): its messages of an excluded mailbox and
  the withheld copies the next purge removes; reported, not the alarm's count.

The excluded ids come from the environment, as the capture's (``PeopleExclusion
.from_env``); an unavailable list fails closed (the status says so and the alarm fires).
Writes ``STATE/exclusion-leak/status.json`` (0600): counts per table and when; never an
id, address or handle. Exit 0 after a scan whatever it found, 1 when it could not scan
(the status records why), 2 on an unreadable state dir.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Optional, Sequence

SCHEMA = "plx-exclusion-leak/v1"
HOME, STATUS = "exclusion-leak", "status.json"
# each statement's bound, on both connections (the counts and the purge's dry run)
STATEMENT_TIMEOUT_MS = 600_000


def _prefix_range(prefix: str) -> tuple[str, str]:
    """[low, high) of every text starting with ``prefix`` that ends in ``/`` under the
    database's byte order (``C.UTF-8``; ``/`` + 1 is ``0``)."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def served_by_id(conn: Any, ids: Sequence[str]) -> dict[str, int]:
    """Served versions (and their search chunks) of an excluded mailbox or 1:1 chat."""
    out = {"mailbox_versions": 0, "mailbox_chunks": 0, "chat_versions": 0, "chat_chunks": 0}
    if not ids:
        return out
    with conn.cursor() as cur:
        cur.execute("SELECT datcollate FROM pg_database WHERE datname = current_database()")
        byte_order = str(cur.fetchone()[0]).split(".")[0] in ("C", "POSIX")
        for oid in ids:
            low, high = _prefix_range(f"graph-mail://{oid}/")
            # a range read on the handle index where text sorts by bytes; else every
            # handle is compared (slow, never a missed row)
            where = ("l1_handle >= %s AND l1_handle < %s" if byte_order
                     else "starts_with(l1_handle, %s) AND %s IS NOT NULL")
            cur.execute("SELECT count(*), count(DISTINCT object_id) FROM ontology.object_versions "
                        "WHERE " + where, (low, high))
            versions, objects = cur.fetchone()
            out["mailbox_versions"] += versions
            if objects:
                cur.execute("SELECT count(*) FROM search.chunks WHERE object_id IN (SELECT "
                            "object_id FROM ontology.object_versions WHERE " + where + ")",
                            (low, high))
                out["mailbox_chunks"] += cur.fetchone()[0]
        pattern = "(" + "|".join(sorted(ids)) + ")"
        chat = ("FROM ontology.object_versions WHERE "
                + ("l1_handle >= 'teams-chat://' AND l1_handle < 'teams-chat:/0' AND "
                   if byte_order else "starts_with(l1_handle, 'teams-chat://') AND ")
                + "l1_handle ~* %s")
        cur.execute("SELECT count(*), count(DISTINCT object_id) " + chat, (pattern,))
        versions, objects = cur.fetchone()
        out["chat_versions"] = versions
        if objects:
            cur.execute("SELECT count(*) FROM search.chunks WHERE object_id IN (SELECT "
                        "object_id " + chat + ")", (pattern,))
            out["chat_chunks"] = cur.fetchone()[0]
    return out


def capture_by_id(state: Path, ids: Sequence[str]) -> Optional[int]:
    """The capture store's messages of an excluded mailbox (not served); None when the
    store is absent or unreadable."""
    import sqlite3
    path = state / "mail-capture" / "capture.db"
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            return sum(conn.execute("SELECT count(*) FROM messages WHERE mailbox_id = ?",
                                    (oid,)).fetchone()[0] for oid in ids)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def withheld_served(state: Path, dsn: str) -> dict[str, Any]:
    """The purge's own dry run over what the capture knows it withholds: Postgres counts
    of rows still citing those handles, and how many stored copies the purge removes."""
    from integrations.capture_mail import MailCaptureStore
    from operating import mail_exclusion_purge as mp

    scan = mp._load(state / "mail-capture" / mp.SCAN_FILE)
    pairs = [(str(b), str(m)) for b, m in scan.get("matches") or ()]
    carried = mp._load_carried(state / "mail-capture" / mp.CARRIED_FILE)
    store = MailCaptureStore.open_read_only(state, timeout=60)
    try:
        # as ``mail_exclusion_purge.captured``, with the remembered copies read through
        # the message-key index each capture run completes (the purge's own read of every
        # record takes many minutes; the daily purge still does it)
        every = sorted({*(store.copies_of(pairs) if pairs else ()),
                        *store.remembered_copies(indexed=True)})
        found: set[str] = set()
        for box, mid in every:
            found.update(h for (h,) in store.db.execute(
                "SELECT l1_handle FROM messages WHERE mailbox_id=? AND message_id=?",
                (box, mid)))
        for h in sorted(found):
            found.update(a for (a,) in store.db.execute(
                "SELECT l1_handle FROM attachments WHERE message_handle=? AND l1_handle "
                "IS NOT NULL", (h,)))
        found.update(store.purged_handles())
    finally:
        store.close()
    handles = sorted({*(h for h in found if h), *carried})
    report: dict[str, Any] = {"handles": len(handles), "capture_copies": len(every)}
    if handles:
        report["postgres"] = mp.purge_pg(dsn, handles, apply=False,
                                         statement_timeout_ms=STATEMENT_TIMEOUT_MS)
    return report


def scan(state: Path, dsn: str, environ: Mapping[str, str], *,
         connect: Optional[Callable[[str], Any]] = None) -> dict[str, Any]:
    """Counts only. ``served`` sums what readers can reach; ``capture`` what the store
    still holds that the next purge removes."""
    from integrations.people_exclusion import PeopleExclusion
    guard = PeopleExclusion.from_env(environ)
    if guard.unavailable:
        return {"ok": False, "error": f"exclusion unavailable ({guard.unavailable})"}
    # The excluded ids, read here rather than through a new PeopleExclusion method:
    # integrations/people_exclusion.py is step code (its bytes feed the steps digest).
    guard.require()
    ids = sorted(frozenset(getattr(guard, "_ids")))
    if connect is None:
        import psycopg

        def connect(target: str) -> Any:
            conn = psycopg.connect(target, connect_timeout=10, autocommit=True,
                                   application_name="plx-exclusion-leak",
                                   options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
                                           "-c default_transaction_read_only=on")
            return conn
    conn = connect(dsn)
    try:
        by_id = served_by_id(conn, ids)
    finally:
        conn.close()
    withheld = withheld_served(state, dsn)
    pg = withheld.get("postgres") or {}
    served = dict(by_id)
    served["withheld_objects"] = int(pg.get("objects") or 0)
    served["withheld_search_chunks"] = int(pg.get("search.chunks") or 0)
    served["withheld_search_entities"] = int(pg.get("search.entities") or 0)
    served["withheld_rows"] = sum(int(v) for k, v in pg.items()
                                  if k != "objects" and not k.startswith("search."))
    return {"ok": True, "excluded_people": len(ids), "served": served,
            "served_total": sum(served.values()),
            "capture": {"excluded_mailbox_messages": capture_by_id(state, ids),
                        "withheld_copies": withheld["capture_copies"],
                        "withheld_handles": withheld["handles"]}}


def run(state: Path, dsn: str, environ: Mapping[str, str] = os.environ, *,
        now: Optional[datetime] = None, scanner: Callable[..., dict[str, Any]] = scan
        ) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    home = state / HOME
    home.mkdir(mode=0o700, exist_ok=True)
    try:
        found = scanner(state, dsn, environ)
    except Exception as exc:  # noqa: BLE001 - a scan that cannot run is the finding
        found = {"ok": False, "error": f"scan failed ({type(exc).__name__})"}
    status = {"schema": SCHEMA, "checked_at": now.isoformat().replace("+00:00", "Z"),
              **found}
    tmp = home / (STATUS + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(status, f, indent=1)
    os.replace(tmp, home / STATUS)
    return status


def window_skip(state: Path) -> Optional[dict[str, Any]]:
    """The window lock's skip report while a window blocks ``exclusion-leak`` (the scan
    then waits for the next hour; a status older than 3 h raises its own alarm), else None.
    The guard keeps the job's running lock for the process, so a window taken later waits
    for this scan. A broken lock never stops the scan (logged)."""
    try:
        from ontology import window_lock
        return window_lock.skip_report("exclusion-leak", state_dir=state)
    except Exception as exc:  # noqa: BLE001 - logged, the scan runs
        print(f"window check failed, running anyway: {exc}", file=sys.stderr)
        return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--state-dir", required=True, type=Path)
    r.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN") or "postgresql:///plx")
    args = parser.parse_args(argv)
    if not args.state_dir.is_dir():
        print(f"state dir unreadable: {args.state_dir}", file=sys.stderr)
        return 2
    held = window_skip(args.state_dir)
    if held:   # a migration or maintenance window: its DDL never waits behind this scan
        print(json.dumps(held, sort_keys=True))
        return 0
    status = run(args.state_dir, args.dsn)
    print(json.dumps({k: status.get(k) for k in ("checked_at", "ok", "served_total",
                                                  "error")}))
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
