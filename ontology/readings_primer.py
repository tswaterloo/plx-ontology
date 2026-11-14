"""Prime whole-file readings into a kept-readings store before a run reads them.

A run that reads every captured document again (the text re-mark judge,
``ontology.pg_step_inplace marks``; a mention cache prefill) extracts each captured file's
text (``integrations.captured_text.captured_text``: PDF pages, Office files, ...) unless the
build's kept readings hold it. Measured 2026-11-10 on served plx: with the kept readings
pruned to ~29k entries, a judge shard spent ~40-50% of its time in extraction, and the
same bytes were read again in every shard holding a message that carries them (mail: 2.03M
attachment rows, 484k distinct byte digests; PDFs 583k rows, 307k distinct).

Here each distinct reading key (bytes digest, MIME type, name, pinned OCR text digest:
the key ``captured_text`` reads under) of the mail attachments and the latest SharePoint
file versions, as ``ontology.project.MailCaptures``/``SharePointCaptures`` build their rows,
is read once, on ``--workers`` processes, and kept in ``<out>/readings.sqlite``
(``captured_text.KeptReadings``, under ``readings_code()``): a reader serving
``[<out>, <projection>]`` (``kept_members.serve_kept``) then reads each file's text from
there. A kept reading is a function of its key and the reading code alone, so a row built
otherwise than the reader's only costs a miss, never another text. Not a build: the store
is never pruned; the projection's own kept readings are never opened for writing.
Resumable (a key already kept under this code is not read again). Prints counts only,
never a file's name or text.

    python3 -m ontology.readings_primer --state-dir <state> --out <dir> [--workers 12]

Stdlib only (the readers' own libraries).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Iterator, Sequence

from integrations import captured_text as ct
from ontology import parent_watch

CHUNK = 16                 # files per worker task
TASKS_PER_WORKER = 400     # tasks before a worker process is replaced (its memory freed)
FILE = "readings.sqlite"

_VIEW: Any = None


def key_of(row: dict[str, Any]) -> tuple[str, str, str, str | None]:
    """The key ``captured_text`` reads ``row`` under."""
    return (row["content_sha256"], ct._mime(row), ct._name(row), ct.ocr_digest(row))


def _source_rows(source: Any) -> Iterator[dict[str, Any]]:
    """The file rows a mail or SharePoint capture source hands ``captured_text`` (its
    ``_message_documents``/``_file_documents`` rows), from its open connection."""
    conn = getattr(source, "_conn", None)
    if conn is None:
        return
    if callable(getattr(source, "_message_documents", None)):
        text = getattr(source, "_text", "text_status, text_sha256")
        for item in conn.execute(f"SELECT metadata, content_sha256, {text} FROM attachments "
                                 "WHERE l1_handle IS NOT NULL AND content_sha256 IS NOT NULL"):
            yield {**json.loads(item["metadata"]), "content_sha256": item["content_sha256"],
                   "text_status": item["text_status"], "text_sha256": item["text_sha256"]}
    elif callable(getattr(source, "_file_documents", None)):
        for row in conn.execute("SELECT record, content_sha256, text_status, text_sha256 FROM "
                                "items WHERE content_sha256 IS NOT NULL AND version_id IN "
                                "(SELECT MAX(version_id) FROM items GROUP BY l1_handle)"):
            yield {**json.loads(row["record"]), "content_sha256": row["content_sha256"],
                   "text_status": row["text_status"], "text_sha256": row["text_sha256"]}


def rows(state_dir: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """(blob directory, file row) of every captured file the mail and SharePoint sources
    read whole-file text of, each source in its read snapshot."""
    from ontology import step_migrate as sm
    from ontology.delta import _internal_domains
    from operating.host import state_captures
    with contextlib.ExitStack() as stack:
        sources = state_captures(state_dir, stack, internal_domains=_internal_domains(os.environ))
        sm.open_snapshots(sources)
        for name in sorted(sources):
            source = sources[name]
            source = getattr(source, "_inner", getattr(source, "_source", source))
            blobs = getattr(source, "blobs", None)
            if not isinstance(blobs, (str, Path)):
                continue
            for row in _source_rows(source):
                yield str(blobs), row


def pending(found: Iterator[tuple[str, dict[str, Any]]], kept: ct.KeptReadings
            ) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, int]]:
    """Each distinct key once, not kept under this code yet; and the counts. Spread by
    the key's digest, so the workers' tasks mix cheap and dear files."""
    seen: set[tuple[Any, ...]] = set()
    out, counts = [], {"files": 0, "distinct": 0, "kept": 0, "to_read": 0}
    for blobs, row in found:
        counts["files"] += 1
        key = key_of(row)
        if key in seen:
            continue
        seen.add(key)
        counts["distinct"] += 1
        if kept.get(key) is not None:
            counts["kept"] += 1
            continue
        out.append((blobs, row))
    out.sort(key=lambda item: hashlib.sha256(repr(key_of(item[1])).encode()).digest())
    counts["to_read"] = len(out)
    return out, counts


def _init(path: str, code: str) -> None:
    global _VIEW
    from ontology.project_parallel import _ChildKept
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # the primer stops its workers
    # Hold one reading at a time: each is handed over to the primer, none kept here.
    ct._CACHE = ct.ReadingMemo(1, ct._size)
    _VIEW = _ChildKept(ct.KeptReadings, path, code)
    ct._CACHE.backing = _VIEW


def _read(chunk: Sequence[tuple[str, dict[str, Any]]]) -> tuple[int, list[tuple[str, Any]]]:
    for blobs, row in chunk:
        ct.captured_text(blobs, row)
    _, kept = _VIEW.take()
    return len(chunk), kept


def prime(state_dir: str | Path, out: str | Path, *, workers: int = 12,
          log: Any = None) -> dict[str, int]:
    """Prime ``<out>/readings.sqlite`` (module docstring); returns counts."""
    started = time.monotonic()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    kept = ct.KeptReadings(out / FILE, ct.readings_code())
    try:
        todo, counts = pending(rows(Path(state_dir).expanduser()), kept)
        counts.update(read=0, newly_kept=0, listed_seconds=round(time.monotonic() - started))
        if log:
            log(f"{counts['to_read']} files to read ({counts['kept']} kept already, "
                f"{counts['distinct']} distinct of {counts['files']})")
        kept.flush()
        chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
        pool = multiprocessing.get_context("spawn").Pool(
            max(1, workers), **parent_watch.pool_init(_init, (str(kept.path), kept.code)),
            maxtasksperchild=TASKS_PER_WORKER)
        try:
            for done, (read, new) in enumerate(pool.imap_unordered(_read, chunks), 1):
                kept.absorb([], new)
                counts["read"] += read
                counts["newly_kept"] += len(new)
                if done % 500 == 0:
                    kept.flush()
                    if log:
                        log(f"{counts['read']}/{counts['to_read']} read, "
                            f"{counts['newly_kept']} kept, {round(time.monotonic() - started)} s")
            pool.close()
        except BaseException:
            pool.terminate()   # what was read stays kept (committed on close)
            raise
        finally:
            pool.join()
    finally:
        kept.close(prune=False)
    counts["seconds"] = round(time.monotonic() - started)
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.readings_primer",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--out", required=True, help="directory of the primed readings.sqlite")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args(argv)

    def log(text: str) -> None:
        print(text, file=sys.stderr, flush=True)
    try:
        counts = prime(args.state_dir, args.out, workers=args.workers, log=log)
    except KeyboardInterrupt:
        log("stopped: what was read is kept; run again to resume")
        return 130
    print(json.dumps(counts, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
