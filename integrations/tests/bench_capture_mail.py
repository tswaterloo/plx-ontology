"""Mail capture throughput on a large store, over the injected fake Graph (no network).

The live store (2.1M messages, 1.4M captured attachments, 118k attachments deferred while
the OCR backlog is at its bound) ran at 25-37k messages/hour with one core busy: each
store call holds the one store lock every capture thread waits on. This seeds a store of that
shape (``--seed-messages`` stored messages, each with ``--seed-attachments`` captured
attachments, ``--deferred`` of them carrying an attachment deferred by a full OCR
backlog, the backlog at its bound), then captures ``--messages`` new messages (one
small attachment each) per mailbox over a Graph that answers each GET after
``--latency`` seconds, and reads ``--rounds`` incremental rounds (no change: the
round-end repair and checks only). Prints messages/second and seconds per round.

Evidence label: injected-transport. Usage::

    python3 -m integrations.tests.bench_capture_mail [--seed-messages 100000] [--messages 300]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

for _name in [k for k in os.environ if k.startswith(("MICROSOFT_GRAPH_", "PLX_WORKBENCH_API"))]:
    os.environ.pop(_name)

from integrations import graph_read  # noqa: E402
from integrations.capture_mail import (ATTACHMENT_DEFERRED, CaptureScope,  # noqa: E402
                                       MailCapture, MailCaptureStore)
from integrations.graph_read import GraphMailReader  # noqa: E402
from integrations.live_source_guard import BoundedBackoff  # noqa: E402
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV,  # noqa: E402
                                                  FOLDER_A, FOLDER_B, SYNTHETIC, UPN_A,
                                                  UPN_B, FakeGraph, capture_audience,
                                                  file_attachment)


class SlowGraph(FakeGraph):
    def __init__(self, latency: float) -> None:
        super().__init__()
        self.latency = latency

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if self.latency and "graph.microsoft.com" in url:
            time.sleep(self.latency)
        return super().__call__(method, url, headers, body, timeout, max_bytes)


def seed_store(store: MailCaptureStore, boxes, messages: int, per: int, deferred: int) -> None:
    db, now = store.db, "2026-09-01T00:00:00Z"
    with db:
        for box, folder in boxes:
            db.execute("INSERT OR IGNORE INTO mailboxes VALUES (?,?,?,?,?,?,?)",
                       (box, f"{box}@x", "[]", "[]", "{}", now, now))
            msg_rows, att_rows = [], []
            for n in range(messages):
                handle = f"seed:{box}:{n}"
                bad = n < deferred
                msg_rows.append((handle, box, f"SEED-{n}", "CK", folder,
                                 json.dumps({"id": f"SEED-{n}", "subject": "s"}), "d",
                                 "captured", now, now))
                for a in range(per):
                    status = ATTACHMENT_DEFERRED if bad and a == 0 else "captured"
                    meta = {"id": f"ATT-{a}", "kind": "#microsoft.graph.fileAttachment",
                            "name": f"f{a}.pdf", "content_type": "application/pdf",
                            "size": 1000 + a, "last_modified": now}
                    att_rows.append((handle, f"ATT-{a}", None if status != "captured"
                                     else f"{handle}:a{a}", box, json.dumps(meta),
                                     "0" * 64 if status == "captured" else None, status, now,
                                     "text" if status == "captured" else None))
            db.executemany(
                "INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, "
                "folder_id, record, digest, body_status, recorded_at, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", msg_rows)
            db.executemany(
                "INSERT INTO attachments (message_handle, attachment_id, l1_handle, "
                "mailbox_id, metadata, content_sha256, content_status, recorded_at, "
                "text_status) VALUES (?,?,?,?,?,?,?,?,?)", att_rows)
        # One scan awaiting OCR, its bytes kept: the backlog over its bound (1 byte).
        db.execute("INSERT INTO blobs (sha256, kind, bytes, stored_at) VALUES (?,?,?,?)",
                   ("1" * 64, "attachment", 5000, now))
        db.execute("UPDATE attachments SET content_sha256=?, text_status='ocr_pending' "
                   "WHERE message_handle=? AND attachment_id='ATT-1'",
                   ("1" * 64, f"seed:{boxes[0][0]}:0"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-messages", type=int, default=100_000)
    parser.add_argument("--seed-attachments", type=int, default=2)
    parser.add_argument("--deferred", type=int, default=2000)
    parser.add_argument("--messages", type=int, default=300)
    parser.add_argument("--latency", type=float, default=0.03)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    boxes = [(BOX_A, FOLDER_A), (BOX_B, FOLDER_B)]
    with tempfile.TemporaryDirectory() as tmp:
        store = MailCaptureStore(Path(tmp) / "state")
        started = time.monotonic()
        seed_store(store, boxes, args.seed_messages, args.seed_attachments, args.deferred)
        seeded = time.monotonic() - started
        graph = SlowGraph(args.latency)
        for box, _f in boxes:
            for n in range(args.messages):
                graph.put(box, f"NEW-{n}", "CK-1", body=f"body {n} " * 40,
                          attachments=[file_attachment(f"A{n}", f"line {n}\n".encode() * 50,
                                                       name="lines.txt")])
        for item in graph.attachments.values():
            for att in item:
                att["contentType"] = "text/plain"
        api, _t = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        scope = CaptureScope([{"address": UPN_A, "folders": ["inbox"]},
                              {"address": UPN_B, "folders": ["inbox"]}], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=SYNTHETIC)
        capture = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                              page_size=100, workers=args.workers, pages_per_turn=2,
                              defer_ocr=True, lanes=2, max_pending_ocr_bytes=1,
                              sleep=lambda s: None)
        started = time.monotonic()
        run = capture.run(scope)
        took = time.monotonic() - started
        new = store.db.execute("SELECT COUNT(*) FROM messages WHERE message_id LIKE "
                               "'NEW-%'").fetchone()[0]
        if new != 2 * args.messages:
            raise SystemExit(f"captured {new} of {2 * args.messages}: "
                             f"{run['incomplete_reasons']}")
        rounds = []
        for _ in range(args.rounds):
            started = time.monotonic()
            capture.run(scope)
            rounds.append(time.monotonic() - started)
        # The check each folder round's end makes:
        # a folder holding none, and the one holding the deferred pile.
        checks = {}
        for name, call in (("has_deferred", store.has_deferred_attachments),
                           ("count", store.deferred_attachments)):
            for folder in ("EMPTY-FOLDER", FOLDER_A):
                started = time.monotonic()
                for _ in range(10):
                    call(BOX_A, folder)
                checks[f"{name}_{'empty' if folder != FOLDER_A else 'held'}_ms"] = round(
                    (time.monotonic() - started) * 100, 2)
        print(json.dumps({"seed_seconds": round(seeded, 1), "versions_new": new,
                          "folder_check": checks,
                          "capture_seconds": round(took, 2),
                          "messages_per_second": round(new / took, 1),
                          "messages_per_hour": round(new / took * 3600),
                          "incremental_round_seconds": [round(r, 2) for r in rounds],
                          "graph_gets": sum(1 for c in graph.calls if c[0] == "GET")}))
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
