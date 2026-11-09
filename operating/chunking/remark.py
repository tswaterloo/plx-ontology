"""``python3 -m operating.chunking.remark [--dsn DSN] [--capture DB] [--apply] [--workers N]``

Re-marks the chunks already in ``search.chunks`` under today's sensitive rule
(``operating.chunking.sensitive``: card and bank statements, pay stubs, offer and
termination letters, SIN numbers, payroll or HR confidentiality labels), without
re-chunking or re-embedding. Each document (its chunks of one object and audience, in
chunk order) is judged on its whole text, its title as support and its labels: the folders
its chunk header names (``Where:``) and, with ``--capture`` (the SharePoint capture's
``capture.db``, read-only), the site, library and folders of its source URL. A document
judged sensitive gets ``finance`` and ``pii`` on every chunk and on the entities read from
it; markings are only ever added, never removed.

Default is a dry run: counts only (documents and chunks it would mark, per reason, kind and
audience), nothing written. ``--apply`` writes in batches, each committed; a rerun finds
nothing more to mark (idempotent). Prints one JSON report: counts only, never text, names
or ids. Exit codes: 0 done, 2 schema missing, 1 failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from multiprocessing import Pool
from typing import Any, Iterator, Sequence

from .sensitive import MARKS, folders, labelled, personal_financial

RECORD_KINDS = ("record_text", "bc_text", "record_words")   # a record's own text: its record's markings
BATCH = 2000
DOC_BATCH = 4000   # documents judged at a time (bounded memory)
DOC_TEXT_CAP = 400_000


def capture_labelled(path: str) -> set[str]:
    """The SharePoint handles whose source URL files them under a payroll or HR
    confidentiality label (the site, library or folders; never the file name)."""
    from urllib.parse import unquote, urlparse
    out: set[str] = set()
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for handle, record in db.execute(
                "SELECT l1_handle, json_extract(record, '$.original_url') FROM items "
                "WHERE record LIKE '%original_url%'"):
            if isinstance(record, str) and labelled(folders(unquote(urlparse(record).path))):
                out.add(handle)
    finally:
        db.close()
    return out


def _labels(header: str | None) -> list[str]:
    for line in (header or "").splitlines():
        if line.startswith("Where: "):
            where = line[len("Where: "):]
            if where.startswith(("attached to ", "inside ")):
                return []
            return folders(where)
    return []


def judge(doc: tuple[str, str | None, list[str], str, bool]) -> str | None:
    """(key, title, labels, text, capture-labelled) -> why the document is sensitive."""
    _key, title, labels, text, captured = doc
    if captured:
        return "label"
    return personal_financial(text, title=title, labels=labels)


def _documents(conn: Any, captured: set[str]) -> Iterator[tuple[tuple, list, tuple]]:
    """(document key, its chunks [(chunk_id, markings)], the judge's input) per document."""
    with conn.cursor(name="remark_chunks") as cur:
        yield from _grouped(cur, captured)


def _grouped(cur: Any, captured: set[str]) -> Iterator[tuple[tuple, list, tuple]]:
    cur.itersize = 5000
    cur.execute("SELECT chunk_id, object_id, audience_key, kind, source_handle, title, header, "
                "body, markings FROM search.chunks WHERE kind <> ALL(%s) "
                "ORDER BY COALESCE(object_id, source_handle), audience_key, chunk_id",
                (list(RECORD_KINDS),))
    key, rows, meta = None, [], None
    texts: list[str] = []
    size = 0
    for chunk_id, object_id, audience, kind, handle, title, header, body, marks in cur:
        this = (object_id or handle, audience)
        if this != key:
            if key is not None:
                yield key, rows, (*meta, "\n".join(texts), meta[3])
            key, rows, texts, size = this, [], [], 0
            meta = [kind, title, _labels(header), False]
        rows.append((chunk_id, list(marks or ())))
        base = str(handle or "").split("#", 1)[0]
        if captured and base in captured:
            meta[3] = True
        if title and not meta[1]:
            meta[1] = title
        if size < DOC_TEXT_CAP and body:
            texts.append(body)
            size += len(body)
    if key is not None:
        yield key, rows, (*meta, "\n".join(texts), meta[3])


def _judge_one(item: tuple) -> str | None:
    kind, title, labels, _captured, text, captured = item
    return judge(("", title, labels, text, captured))


def run(dsn: str, *, capture: str | None, apply: bool, workers: int) -> dict[str, Any]:
    import psycopg
    captured = capture_labelled(capture) if capture else set()
    reader = psycopg.connect(dsn, autocommit=False)
    writer = psycopg.connect(dsn, autocommit=False) if apply else None
    counts: dict[str, Any] = {"documents": 0, "chunks": 0, "capture_labelled_handles":
                              len(captured)}
    reasons, kinds, audiences = Counter(), Counter(), Counter()
    docs_marked = chunks_marked = already = 0
    pending: list[str] = []
    objects: set[tuple[str, str]] = set()

    def flush() -> None:
        nonlocal pending, objects
        if writer is not None and pending:
            writer.execute(
                "UPDATE search.chunks SET markings = ARRAY(SELECT DISTINCT m FROM "
                "unnest(markings || %s::text[]) AS m ORDER BY m), updated_at = now() "
                "WHERE chunk_id = ANY(%s) AND NOT markings @> %s::text[]",
                (list(MARKS), pending, list(MARKS)))
            if objects:
                writer.execute(
                    "UPDATE search.entities e SET markings = ARRAY(SELECT DISTINCT m FROM "
                    "unnest(e.markings || %s::text[]) AS m ORDER BY m) FROM "
                    "unnest(%s::text[], %s::text[]) AS d(object_id, audience_key) "
                    "WHERE e.object_id = d.object_id AND e.audience_key = d.audience_key "
                    "AND NOT e.markings @> %s::text[]",
                    (list(MARKS), [o for o, _ in objects], [a for _, a in objects],
                     list(MARKS)))
            writer.commit()
        pending, objects = [], set()

    try:
        def decide(batch: list[tuple]) -> None:
            nonlocal docs_marked, chunks_marked, already
            found = pool.map(_judge_one, [item for _k, _r, item in batch], chunksize=32)
            for (key, rows, item), reason in zip(batch, found):
                counts["documents"] += 1
                counts["chunks"] += len(rows)
                if reason is None:
                    continue
                todo = [cid for cid, marks in rows if not set(MARKS) <= set(marks)]
                if not todo:
                    already += 1
                    continue
                docs_marked += 1
                chunks_marked += len(todo)
                reasons[reason] += 1
                kinds[item[0]] += len(todo)
                audiences[key[1]] += len(todo)
                pending.extend(todo)
                if key[0] and ":" in key[0] and not key[0].startswith(("sharepoint:", "graph-")):
                    objects.add((key[0], key[1]))
                if len(pending) >= BATCH:
                    flush()

        with Pool(max(1, workers)) as pool:
            batch: list[tuple] = []
            for document in _documents(reader, captured):
                batch.append(document)
                if len(batch) >= DOC_BATCH:
                    decide(batch)
                    batch = []
            decide(batch)
        flush()
    finally:
        reader.close()
        if writer is not None:
            writer.close()
    counts.update({"applied": apply, "documents_to_mark": docs_marked,
                   "chunks_to_mark": chunks_marked, "documents_already_marked": already,
                   "by_reason": dict(reasons), "chunks_by_kind": dict(kinds),
                   "chunks_by_audience_top": dict(audiences.most_common(12))})
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.chunking.remark")
    parser.add_argument("--dsn", help="Postgres DSN (default PLX_PG_DSN, else plx)")
    parser.add_argument("--capture", help="the SharePoint capture's capture.db (read-only)")
    parser.add_argument("--apply", action="store_true", help="write (default: counts only)")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    from operating.embed import config
    dsn = args.dsn or os.environ.get("PLX_PG_DSN") or config.DEFAULT_DSN
    try:
        import psycopg
        with psycopg.connect(dsn) as conn:
            if conn.execute("SELECT to_regclass('search.chunks')").fetchone()[0] is None:
                print(json.dumps({"refused": "search.chunks absent"}))
                return 2
        report = run(dsn, capture=args.capture, apply=args.apply, workers=args.workers)
    except Exception as exc:  # noqa: BLE001 - reported as its type only (no text)
        print(json.dumps({"failed": type(exc).__name__}))
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
