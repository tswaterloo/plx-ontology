"""``python3 -m integrations.spark_doc_reader``: read the documents the mail capture left
unread on the Sparks (11-23: ~11k PDFs, web pages, office files and csv the box's extractor
deferred, failed on or never reached; their attachments stay ``ai_ok`` NULL, withheld from the
AI, until text exists).

* ``plan``: the distinct (bytes, type, name) of every captured, unread document of a current
  message version (text status ``TARGET_STATUSES``), as ``attach_fetch.read_key``, written to
  the pass's own ``docs.sqlite`` in ``--fetch-dir``. Images are not here (``image_ocr_gpu``).
* ``read``: each planned document not yet read is checked just before its bytes leave
  (``image_ocr_gpu.Gate``: people exclusion, undecided addresses, Freightcom in the name,
  record or text; any doubt refuses, counted), shipped over ssh to
  ``integrations.spark_doc_worker`` on each host, and its reading stored in ``readings.sqlite``
  (``attach_fetch.Readings``) and its plan row in ``stored.sqlite`` (kind ``doc``), where
  ingest's ``apply_stored_pass`` applies it as a new text version. Only a reading with text
  (``extracted``) is kept for apply; every other answer is noted and the document stays as it
  is. Bytes are never written on the Spark. Counts only on stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

DB = "docs.sqlite"
# the capture's text statuses of a document nothing has read (and no image: ``image_ocr_gpu``)
TARGET_STATUSES = ("ocr_deferred", "failed", "no_text", "unsupported", "ocr_pending",
                   "extraction_timed_out", "exceeds_bound")
TARGET_LIKE = "ocr_failed%"
DOC_EXT = (".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".htm", ".html", ".docx", ".doc",
           ".pptx", ".rtf", ".xml", ".json", ".md", ".tsv")
# A file goes over the pipe in frames of at most this many bytes (a frame's length is 4 bytes):
# no file size is refused, a larger one is just more frames.
PART = 1 << 30
KEPT = ("extracted", "extracted_ocr")


def is_document(name: str, content_type: str) -> bool:
    low = (name or "").lower()
    if low.endswith(DOC_EXT):
        return True
    return (content_type or "").lower() in {"application/pdf", "text/csv", "text/html",
                                            "text/plain", "application/vnd.ms-excel"}


def open_db(root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(Path(root) / DB, timeout=30, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS docs (read_key TEXT PRIMARY KEY, "
               "sha TEXT NOT NULL, meta TEXT NOT NULL, was TEXT NOT NULL, state TEXT NOT NULL "
               "DEFAULT 'planned', status TEXT, seconds REAL)")
    return db


def plan(mail: Any, held: set[str], db: sqlite3.Connection) -> dict[str, int]:
    from .attach_fetch import read_key
    counts = {"seen": 0, "withheld": 0, "not_document": 0, "listed": 0}
    marks = ",".join("?" * len(TARGET_STATUSES))
    rows = mail.db.execute(
        "SELECT a.content_sha256, a.metadata, a.text_status, a.mailbox_id FROM attachments a "
        "JOIN messages m ON m.l1_handle=a.message_handle AND m.superseded_at IS NULL WHERE "
        f"a.content_status='captured' AND a.content_sha256 IS NOT NULL AND (a.text_status IN "
        f"({marks}) OR a.text_status LIKE ?)", (*TARGET_STATUSES, TARGET_LIKE))
    batch = []
    for sha, meta_json, was, mailbox in rows:
        counts["seen"] += 1
        if str(mailbox).lower() in held:
            counts["withheld"] += 1
            continue
        meta = json.loads(meta_json)
        if not is_document(str(meta.get("name") or ""), str(meta.get("content_type") or "")):
            counts["not_document"] += 1
            continue
        batch.append((read_key(sha, meta), sha, json.dumps(meta, sort_keys=True), was))
    with db:
        before = db.execute("SELECT count(*) FROM docs").fetchone()[0]
        db.executemany("INSERT OR IGNORE INTO docs (read_key, sha, meta, was) VALUES (?,?,?,?)",
                       batch)
        counts["listed"] = db.execute("SELECT count(*) FROM docs").fetchone()[0] - before
    return counts


class Worker:
    """One ssh pipe to ``spark_doc_worker`` on a host (its own process)."""

    def __init__(self, argv: Sequence[str]) -> None:
        self.proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        hello = self._frame()
        if not hello or "hello" not in json.loads(hello):
            raise RuntimeError("worker_hello")

    def _frame(self) -> bytes | None:
        head = self.proc.stdout.read(4)
        if len(head) < 4:
            return None
        (n,) = struct.unpack(">I", head)
        return self.proc.stdout.read(n)

    def _send(self, body: bytes) -> None:
        self.proc.stdin.write(struct.pack(">I", len(body)) + body)
        self.proc.stdin.flush()

    def read(self, meta: Mapping[str, Any], content: bytes) -> dict[str, Any]:
        parts = [content[i:i + PART] for i in range(0, len(content), PART)] or [b""]
        self._send(json.dumps({"name": meta.get("name"),
                               "content_type": meta.get("content_type"),
                               "parts": len(parts)}).encode())
        for part in parts:
            self._send(part)
        out = self._frame()
        if out is None:
            raise RuntimeError("worker_pipe_ended")
        return json.loads(out)

    def close(self) -> None:
        try:
            self.proc.stdin.write(struct.pack(">I", 0))
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait(timeout=30)


def run(mail: Any, gate: Any, db: sqlite3.Connection, readings_db: sqlite3.Connection,
        stored_db: sqlite3.Connection, workers: Sequence[Worker], *, budget_seconds: float,
        limit: int | None = None, bytes_of: Callable[[str], bytes | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic) -> dict[str, int]:
    """Read the planned documents with ``workers`` (one thread each)."""
    deadline = monotonic() + budget_seconds
    get = bytes_of or (lambda sha: _blob(mail, sha))
    counts = {"sent": 0, "kept": 0, "no_text": 0, "refused": 0, "bytes_missing": 0,
              "worker_failed": 0}
    lock = threading.Lock()
    todo: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
    q = "SELECT read_key, sha, meta FROM docs WHERE state='planned' ORDER BY read_key"
    for i, row in enumerate(db.execute(q).fetchall()):
        if limit is not None and i >= limit:
            break
        todo.put(row)

    def mark(key: str, state: str, status: str | None = None, seconds: float | None = None):
        with lock, db:
            db.execute("UPDATE docs SET state=?, status=?, seconds=? WHERE read_key=?",
                       (state, status, seconds, key))

    def work(worker: Worker) -> None:
        while monotonic() < deadline:
            try:
                key, sha, meta_json = todo.get_nowait()
            except queue.Empty:
                return
            meta = json.loads(meta_json)
            with lock:
                why = gate.why_not(mail.db, sha)
            if why is not None:
                with lock:
                    counts["refused"] += 1
                mark(key, "refused", why[:60])
                continue
            content = get(sha)
            if content is None:
                with lock:
                    counts["bytes_missing"] += 1
                mark(key, "bytes_missing")
                continue
            try:
                got = worker.read(meta, content)
            except Exception:  # noqa: BLE001 - this host is done; the document stays planned
                with lock:
                    counts["worker_failed"] += 1
                return
            status = str(got.get("status"))
            if isinstance(got.get("text"), str):   # a lone surrogate from a bad font map
                got["text"] = got["text"].encode("utf-8", "ignore").decode("utf-8")
            with lock:
                counts["sent"] += 1
                counts["kept" if status in KEPT and got.get("text") else "no_text"] += 1
            if status in KEPT and got.get("text"):
                with lock, readings_db:
                    readings_db.execute(
                        "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?)",
                        (key, sha, status, got["text"], None, got.get("seconds"), time.time()))
                    stored_db.execute(
                        "INSERT OR IGNORE INTO stored VALUES (?,?,?,?,?,?)",
                        (key, sha, "doc", meta_json, 1, time.time()))
                    stored_db.commit()
            mark(key, "read", status, got.get("seconds"))

    threads = [threading.Thread(target=work, args=(w,), daemon=True) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return counts


def _blob(mail: Any, sha: str) -> bytes | None:
    try:
        return mail.blob(sha)
    except Exception:  # noqa: BLE001 - dropped or missing
        return None


def ssh_argv(host: str, ssh_config: str, code: str, python: str) -> list[str]:
    # ``python`` is the operator's own setting (it may hold $HOME); ``code`` is quoted
    remote = (f"cd {shlex.quote(code)} && PYTHONPATH=. exec nice -n 10 {python} "
              "-m integrations.spark_doc_worker")
    return ["ssh", "-o", "BatchMode=yes", "-F", ssh_config, host, remote]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="spark_doc_reader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "read", "status"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", required=True)
        p.add_argument("--fetch-dir", required=True)
        if name == "read":
            p.add_argument("--host", action="append", required=True)
            p.add_argument("--per-host", type=int, default=8)
            p.add_argument("--budget-seconds", type=float, required=True)
            p.add_argument("--limit", type=int)
            p.add_argument("--ssh-config", default="/data/plx-state/ts-petra/ssh_config")
            p.add_argument("--code", default="plx-docs/code")
            p.add_argument("--python", default="$HOME/plx-tests/venv/bin/python")
    args = ap.parse_args(argv)
    from .attach_fetch import Readings, StoredPlan
    from .capture_mail import MailCaptureStore
    from .image_ocr_gpu import Gate
    root = Path(args.fetch_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    db = open_db(root)
    if args.cmd == "status":
        print(json.dumps(dict(db.execute("SELECT state, count(*) FROM docs GROUP BY 1"))))
        return 0
    mail = MailCaptureStore.open_read_only(args.state_dir, timeout=60)
    gate = Gate.from_env(os.environ, mail, args.state_dir)
    if gate.withheld is None:
        print(json.dumps({"error": "people_exclusion_unavailable"}))
        return 2
    if args.cmd == "plan":
        print(json.dumps(plan(mail, {str(m).lower() for m in gate.withheld}, db)))
        return 0
    readings = Readings(root).db
    stored = StoredPlan(root).db
    workers = [Worker(ssh_argv(h, args.ssh_config, args.code, args.python))
               for h in args.host for _ in range(args.per_host)]
    try:
        print(json.dumps(run(mail, gate, db, readings, stored, workers,
                             budget_seconds=args.budget_seconds, limit=args.limit)))
    finally:
        for w in workers:
            w.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
