"""extract.py OUT_DIR: on the box, the parts of the captures a comms train's build reads, into
OUT_DIR (a state directory for PLX_STATE_DIR on the Spark build copy), read-only:

* mail-capture/capture.db: the mailboxes (operating.pg.mail_owners.mailbox_addresses) and each
  current message's authentication_results (operating.mail_envelope.reader); nothing else of a
  message (no body, subject, addresses or record fields);
* meetings-capture/capture.db: the whole meetings capture (a transcript's participants are read
  from its item ACLs, business_comms.captured_participants), a consistent copy (SQLite backup).

Refuses when OUT_DIR exists. Prints what it wrote."""
import os
import sqlite3
import sys
import time

STATE = os.environ.get("PLX_STATE_DIR") or "/data/plx-state"


def mail(out: str) -> str:
    src = os.path.join(STATE, "mail-capture", "capture.db")
    ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    w = sqlite3.connect(out)
    w.executescript(
        "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
        "CREATE TABLE mailboxes (mailbox_id TEXT PRIMARY KEY, upn TEXT NOT NULL, identities TEXT NOT NULL);"
        "CREATE TABLE messages (version_id INTEGER PRIMARY KEY, mailbox_id TEXT NOT NULL, "
        "message_id TEXT NOT NULL, superseded_at TEXT, record TEXT NOT NULL);")
    w.executemany("INSERT INTO mailboxes VALUES (?,?,?)",
                  ro.execute("SELECT mailbox_id, upn, identities FROM mailboxes"))
    cur = ro.execute(
        "SELECT version_id, mailbox_id, message_id, json_object('authentication_results', "
        "json_extract(record, '$.authentication_results')) FROM messages WHERE superseded_at IS "
        "NULL AND json_extract(record, '$.authentication_results') IS NOT NULL")
    n = 0
    while True:
        rows = cur.fetchmany(20000)
        if not rows:
            break
        w.executemany("INSERT INTO messages (version_id, mailbox_id, message_id, record) "
                      "VALUES (?,?,?,?)", rows)
        n += len(rows)
    w.execute("CREATE INDEX messages_by_id ON messages(mailbox_id, message_id)")
    w.commit()
    w.close()
    return f"mail: mailboxes + {n} messages"


def meetings(out: str) -> str:
    src = os.path.join(STATE, "meetings-capture", "capture.db")
    ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    w = sqlite3.connect(out)
    ro.backup(w)
    w.close()
    return f"meetings: {os.path.getsize(out) / 1e6:.0f} MB"


def main() -> int:
    out = sys.argv[1]
    if os.path.exists(out):
        sys.exit(f"refused: {out} exists")
    t0 = time.time()
    said = []
    for sub, fn in (("mail-capture", mail), ("meetings-capture", meetings)):
        os.makedirs(os.path.join(out, sub), mode=0o700)
        said.append(fn(os.path.join(out, sub, "capture.db")))
    print("; ".join(said) + f" in {time.time() - t0:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
