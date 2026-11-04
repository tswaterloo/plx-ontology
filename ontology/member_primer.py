"""Prime the kept container member readings before a build reads them.

A projection build reads the members of every captured container (a mail's attached
messages, a zip's or ``.msg``'s files: ``integrations.captured_text.container_members``)
for its document fields and member records. Measured on the live gen 8 cold build
(2026-11-03): its document workers spent hours in ``read_container``, since member
readings were not kept between builds. A build now keeps them
(``<projection>/member-readings.sqlite``, ``integrations.kept_members``); this primer
fills that store ahead of a build, from the same rows the build reads through
``container_members`` (the mail capture's ``attachments`` and the SharePoint capture's
``driveItems``, as ``ontology.project.MailCaptures``/``SharePointCaptures`` stream them),
under the same key and code digest (``kept_members.members_code``), so the build's
lookups hit what it kept.

    python3 -m ontology.member_primer --state-dir <state> [--workers 4]

``<state>``: the host state directory (its capture stores; the store is primed at
``<state>/projection/member-readings.sqlite``, or ``--projection-dir``). Reads the capture
stores and blobs read-only and writes only that store, as its one writer: it holds the
store's lock (``kept_members.kept_members``) and refuses to start while a build holds it;
a build that starts while it runs reads without kept member readings. Resumable: a
container already kept under the current code is not read again. Ctrl-C stops it and keeps
what it read. Workers are replaced after ``TASKS_PER_WORKER`` tasks (their memory freed);
a reading made while a worker ran out of memory is not kept (``kept_members``): the build
reads it. It is not a build: it never prunes the store nor counts among the builds entries
are pruned by. Prints counts only, never a file's name or text.

Stdlib only.
"""

from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path
import signal
import sys
import time
from typing import Any, Iterator, Sequence

from integrations import captured_text as ct
from integrations import kept_members as km

ROW_KEYS = ("content_sha256", "name", "content_type", "mime_type")
CHUNK = 4                  # containers per worker task
TASKS_PER_WORKER = 200     # tasks before a worker process is replaced (its memory freed)
SOURCES = ("graph_mail", "sharepoint")

_VIEW: Any = None


def key_of(row: dict[str, Any]) -> tuple[str, str, str]:
    """The key ``container_members`` reads ``row``'s members under."""
    return (row["content_sha256"], ct._name(row), ct._mime(row))


def _rows(state_dir: Path, sources: Sequence[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """(blob directory, file row trimmed to what the member read uses) of every captured
    file the build reads members of, each source read in one snapshot."""
    from operating.host import capture_held, open_state_capture

    for source in sources:
        if not capture_held(state_dir, source):
            continue
        relation = "attachments" if source == "graph_mail" else "driveItems"
        with open_state_capture(state_dir, source) as opened:
            blobs = str(opened.blobs)
            for rec in opened.records([relation])[relation]:
                row = rec.row if isinstance(rec.row, dict) else {}
                if isinstance(row.get("content_sha256"), str):
                    yield blobs, {k: row[k] for k in ROW_KEYS if k in row}


def pending(rows: Sequence[tuple[str, dict[str, Any]]], kept: km.KeptMembers
            ) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, int]]:
    """The containers to read: each distinct key once, not kept under this code, whose
    bytes may hold members (``captured_text._may_contain``); and the counts."""
    seen: set[tuple[str, str, str]] = set()
    out, counts = [], {"files": 0, "distinct": 0, "kept": 0, "containers": 0}
    for blobs, row in rows:
        counts["files"] += 1
        key = key_of(row)
        if key in seen:
            continue
        seen.add(key)
        counts["distinct"] += 1
        if kept.holds(key):
            counts["kept"] += 1
            continue
        if ct._may_contain(blobs, key[0], row):
            counts["containers"] += 1
            out.append((blobs, row))
    return out, counts


def _init(path: str, code: str) -> None:
    global _VIEW

    signal.signal(signal.SIGINT, signal.SIG_IGN)   # the primer stops its workers
    # Hold one container at a time: the primer hands each reading over, keeps none.
    ct._MEMBER_READINGS = ct.ReadingMemo(1, ct._members_size)
    _VIEW = km.KeptMembers.child_view(path, code)
    ct._MEMBER_READINGS.backing = _VIEW


def _read(chunk: Sequence[tuple[str, dict[str, Any]]]) -> tuple[int, list[tuple[str, Any]]]:
    """Read each container of ``chunk`` through the memo (what it remembers is handed
    back, encoded: a transient failure or bytes gone are not)."""
    for blobs, row in chunk:
        ct.container_members(blobs, row)
    _, kept = _VIEW.take()
    return len(chunk), kept


def prime(state_dir: str | Path, projection_dir: str | Path | None = None, *,
          workers: int = 4, sources: Sequence[str] = SOURCES, log: Any = None
          ) -> dict[str, int]:
    """Prime ``<projection>/member-readings.sqlite`` (module docstring); returns counts.
    Raises ``kept_members.MembersBusy`` while a build holds the store."""
    state = Path(state_dir).expanduser()
    projection = state / "projection" if projection_dir is None else Path(projection_dir)
    started = time.monotonic()
    rows = list(_rows(state, sources))   # each source's snapshot closed
    blobs = sorted({blobs for blobs, _ in rows})
    with km.kept_members(projection / km.MEMBERS_FILE, blobs, required=True,
                         build=False) as kept:
        assert kept is not None
        todo, counts = pending(rows, kept)
        counts.update(read=0, newly_kept=0)
        if log:
            log(f"{counts['containers']} containers to read "
                f"({counts['kept']} kept already, {counts['distinct']} distinct files)")
        kept.flush()
        chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
        pool = multiprocessing.get_context("spawn").Pool(
            max(1, workers), initializer=_init,
            initargs=(str(kept.path), kept.code), maxtasksperchild=TASKS_PER_WORKER)
        try:
            for done, (read, new) in enumerate(pool.imap_unordered(_read, chunks), 1):
                kept.absorb([], new)
                counts["read"] += read
                counts["newly_kept"] += len(new)
                if done % 250 == 0:
                    kept.flush()
                    if log:
                        log(f"{counts['read']}/{counts['containers']} read, "
                            f"{counts['newly_kept']} kept, "
                            f"{round(time.monotonic() - started)} s")
            pool.close()
        except BaseException:
            pool.terminate()   # Ctrl-C: what was read stays kept (committed on close)
            raise
        finally:
            pool.join()
    counts["seconds"] = round(time.monotonic() - started)
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.member_primer",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--projection-dir")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    def log(text: str) -> None:
        print(text, file=sys.stderr, flush=True)
    try:
        counts = prime(args.state_dir, args.projection_dir, workers=args.workers, log=log)
    except km.MembersBusy:
        log("member readings are held by a build or another primer: not started")
        return 2
    except KeyboardInterrupt:
        log("stopped: what was read is kept; run again to resume")
        return 130
    log(f"done: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
