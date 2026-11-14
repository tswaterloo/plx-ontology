"""Find and purge captured mail the people exclusion refuses but the store cannot tell.

Until Graph reads selected ``bccRecipients`` (INT-018), a message whose only excluded
party sat in Bcc passed the exclusion and was captured: its stored record has no Bcc, so
neither the capture's own purge (``MailCaptureStore.purge_withheld``) nor any reader can
find it. Bcc is only on the sender's copy, so the candidates are the stored current
messages sent from inside the tenant (a ``from`` in a domain of a captured mailbox) or
with no sender (a draft).

``python3 -m operating.mail_exclusion_purge scan --state-dir D --live-audience F
[--out FILE] [--workers N]``: re-reads every candidate through the GET-only Graph client
(``GraphMailReader.refused_messages``: ids and the fields the check needs, never a body;
a folder whose candidates outnumber its pages is listed, else each candidate is read by
id) and writes the ids the exclusion refuses now to ``FILE`` (default
``D/mail-capture/exclusion-scan.json``, mode 0600: mailbox and message ids only, never
an address). Resumable: a mailbox already done in ``FILE`` is not read again. Credentials
and the exclusion list come from the environment, as the capture's.

``python3 -m operating.mail_exclusion_purge purge --state-dir D --matches FILE
[--dsn DSN] [--store PATH ...] [--apply]``: removes those messages, every other mailbox's
copy of them (the same internet message id) and everything derived from them, while
holding the host's cycle lock (no capture beside it):

1. Postgres (``--dsn``, default ``PLX_PG_DSN``): every ontology object with a version
   citing a removed message or attachment (and the attachments and container members it
   carries) with all its rows (``operating.pg.ontology_delta._DELETE``), the events cited
   on those records, and the search chunks and entities of those objects or citing those
   records; a ``load_state`` row records the counts (so served caches refresh).
2. Each SQLite ontology store (``--store``; default the served generations' ``store.sqlite``
   and ``objects.sqlite`` and the delta layers under ``D/projection``): the same objects.
3. The capture store (``MailCaptureStore.purge_messages``): the messages, their
   attachments, text readings, events and unreferenced blobs.

Besides the scan's matches (none when the default scan file does not exist yet) it removes
every stored copy of a message the capture remembered as withheld (``people_withheld``: an
excluded person sent or received it, seen on another mailbox's copy) and the derived copies
of every L1 handle a capture purge deleted (``purged_handles``, journalled by
``MailCaptureStore._purge``); on ``--apply`` it remembers the removed messages' keys, so no
mailbox stores a copy again, and forgets the journalled handles once their copies are gone.

``python3 -m operating.mail_exclusion_purge carried --state-dir D [--out FILE]
[--workers N] [--live-audience F] [--apply]`` (DOC-008): decides every stored attachment by
the mail it carries (``integrations.carried_mail``: an attached ``.eml`` or ``.msg``, one in
a zip, each carried mail's senders and recipients) with the people exclusion of the
environment: bound through the GET-only Graph client with ``--live-audience`` (directory
reads only, as the capture binds it: every address decided), else offline with the
capture's recorded directory (a tenant address it does not hold stays undecided and
withholds its container), and writes the
attachment handles not cleared, each with its content status, to ``FILE`` (default
``D/mail-capture/carried-scan.json``, 0600: handles and counts, never an address). A
container's walk is kept in the capture store (``carried_people``) with ``--apply``
(holding the cycle lock), and read back instead of its bytes by the next scan. ``purge``
also withholds those attachments (``--carried FILE``; default that file when present):
their derived copies go as above, and each capture row keeps its message and id but loses
its bytes, text and handle, as an attachment the capture never stored.

Without ``--apply`` nothing is written (the capture store opened read-only without the
cycle lock; Postgres: counts over session temp tables in a transaction rolled back; SQLite
opened read-only) and it prints what would go. Either
way it prints one JSON report of counts only. Not touched: the content-addressed build
caches (``mention-extract``, ``fields``, ``readings``, ``member-readings``: keyed by the
bytes, which another record may share).
Exit codes: 0 done, 2 refused, 1 failed or (``scan``) incomplete: run it again.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import parent_watch

FORMAT = "plx-mail-exclusion-matches/v1"
SCAN_FILE = "exclusion-scan.json"
PAGE = 1000          # messages per folder listing page
# A folder is listed when it takes fewer pages than it has candidates (else read by id).
_CARRIED = ("Attachment.attached_to", "ContainerMember.in_attachment",
            "ContainerMember.in_file")


# ------------------------------------------------------------------------- candidates

def candidates(db: sqlite3.Connection) -> dict[str, dict[str, list[str]]]:
    """``{mailbox id: {folder id: [message id, ...]}}``: the stored current messages that
    can carry Bcc (sent from a tenant domain, or with no sender)."""
    domains: set[str] = set()
    for upn, identities in db.execute("SELECT upn, identities FROM mailboxes"):
        for value in [upn, *json.loads(identities or "[]")]:
            if isinstance(value, str) and "@" in value:
                domains.add(value.rsplit("@", 1)[1].lower())
    found: dict[str, dict[str, list[str]]] = {}
    for box, folder, mid, sender in db.execute(
            "SELECT mailbox_id, folder_id, message_id, lower(trim(json_extract(record, "
            "'$.from'))) FROM messages WHERE superseded_at IS NULL"):
        if sender and sender.rsplit("@", 1)[-1] not in domains:
            continue
        found.setdefault(box, {}).setdefault(folder, []).append(mid)
    return found


def _totals(db: sqlite3.Connection) -> dict[tuple[str, str], int]:
    return {(b, f): t for b, f, t in db.execute(
        "SELECT mailbox_id, folder_id, total FROM mailbox_folders WHERE total IS NOT NULL")}


def _listed(total: int | None, wanted: int) -> bool:
    return total is not None and wanted > 1 and math.ceil(max(total, 1) / PAGE) < wanted


# ------------------------------------------------------------------------------ scan

def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"format": FORMAT, "done": [], "matches": [], "counts": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, Mapping) or data.get("format") != FORMAT:
        raise ValueError("matches_file_invalid")
    return dict(data)


def _save(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, sort_keys=True)
    os.replace(tmp, path)


def scan(db: sqlite3.Connection, reader: Any, actor: Any, out: Path, *, workers: int = 8,
         now: Callable[[], str] | None = None) -> dict[str, Any]:
    """Re-read the candidates (:func:`candidates`) of every mailbox ``out`` has not done
    and record the refused ids in ``out``; counts only."""
    from integrations.live_source_guard import NotVisible
    from integrations.people_exclusion import ExclusionUnavailable, PeopleExcluded

    reader.prepare_people()   # ExclusionUnavailable: nothing is read
    state = _load(out)
    done = set(state["done"])
    wanted = candidates(db)
    totals = _totals(db)
    counts = {"mailboxes": len(wanted), "candidates": 0, "read": 0, "listed_folders": 0,
              "matched": 0, "undecided": 0, "absent": 0, "mailboxes_refused": 0,
              "mailboxes_failed": 0, "failed_kinds": {}}
    lock = threading.Lock()
    matches = {tuple(m) for m in state["matches"]}

    def one(box: str) -> None:
        folders = wanted[box]
        hits: set[tuple[str, str]] = set()
        local = {"read": 0, "listed": 0, "undecided": 0, "absent": 0}
        try:
            for folder, ids in folders.items():
                if _listed(totals.get((box, folder)), len(ids)):
                    got = reader.refused_messages(actor, box, folder_id=folder, page_size=PAGE)
                    local["listed"] += 1
                else:
                    got = reader.refused_messages(actor, box, message_ids=ids)
                stored = set(ids)
                hits |= {(box, m) for m in got["refused"] if m in stored}
                local["read"] += got["read"]
                local["undecided"] += len(got["undecided"])
                local["absent"] += len(got["absent"])
        except (NotVisible, PeopleExcluded):
            with lock:   # a mailbox the gate refuses: withheld whole by the capture
                counts["mailboxes_refused"] += 1
            return
        except ExclusionUnavailable:
            raise
        except Exception as exc:   # one mailbox; resumed by the next scan
            with lock:
                counts["mailboxes_failed"] += 1
                kinds = counts["failed_kinds"]
                kinds[type(exc).__name__] = kinds.get(type(exc).__name__, 0) + 1
            return
        with lock:
            counts["read"] += local["read"]
            counts["listed_folders"] += local["listed"]
            counts["undecided"] += local["undecided"]
            counts["absent"] += local["absent"]
            matches.update(hits)
            if not local["undecided"]:
                done.add(box)
            state.update(done=sorted(done), matches=sorted(matches))
            _save(out, state)

    todo = sorted(b for b in wanted if b not in done)
    counts["candidates"] = sum(len(ids) for b in todo for ids in wanted[b].values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in [pool.submit(one, box) for box in todo]:
            future.result()
    counts["matched"] = len(matches)
    counts["complete"] = len(done) + counts["mailboxes_refused"] >= len(wanted) \
        and not counts["mailboxes_failed"] and not counts["undecided"]
    state.update(done=sorted(done), matches=sorted(matches), counts=counts,
                 scanned_at=(now or _now)())
    _save(out, state)
    return counts


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ carried mail (DOC-008)

CARRIED_FORMAT = "plx-carried-mail-scan/v1"
CARRIED_FILE = "carried-scan.json"


def _mailish(name: str, hint: str | None) -> bool:
    from integrations import carried_mail
    return carried_mail.may_carry(b"", name, hint)


def _walk_one(blobs: str, digest: str, name: str, hint: str | None) -> tuple[str, str | None]:
    """(digest, the encoded walk of one stored attachment's bytes; None when they are
    gone): a worker's whole job. A raw zip is told an Office package by its central
    directory alone (``use_case_parts.is_container``'s own test), never read whole."""
    from zipfile import BadZipFile, ZipFile
    from integrations import carried_mail
    from integrations.captured_text import DROPPED_SUFFIX, blob_path, read_blob
    raw = blob_path(blobs, digest)
    head = None
    try:
        with open(raw, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        pass
    if head is not None:
        if not carried_mail.may_carry(head, name, hint):
            return digest, carried_mail.encode(None)
        if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            try:
                with ZipFile(raw) as archive:
                    if any(i.filename == "[Content_Types].xml" for i in archive.infolist()):
                        return digest, carried_mail.encode(None)
            except (BadZipFile, OSError, ValueError, EOFError):
                pass   # walked below: an unreadable zip clears nobody
    content = read_blob(blobs, digest)
    if content is None:
        if blob_path(blobs, digest, DROPPED_SUFFIX).exists():
            # an original the store dropped was no container (``settle_original`` keeps
            # every container's bytes): it carries nothing
            return digest, carried_mail.encode(None)
        return digest, None
    return digest, carried_mail.encode(carried_mail.walk(content, name, hint))


def carried_candidates(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{container digest: {"name", "hint", "handles": {l1 handle: ...}}}`` of every
    stored captured attachment (the name walked with: a mail by its name or type when
    any of its names says so, else the first)."""
    found: dict[str, dict[str, Any]] = {}
    for handle, digest, meta in db.execute(
            "SELECT l1_handle, content_sha256, metadata FROM attachments WHERE "
            "content_status='captured' AND l1_handle IS NOT NULL AND content_sha256 IS NOT "
            "NULL"):
        try:
            data = json.loads(meta)
        except ValueError:
            data = {}
        name = str(data.get("name") or "")
        hint = data.get("content_type") if isinstance(data.get("content_type"), str) else None
        one = found.get(digest)
        if one is None:
            one = found[digest] = {"name": name, "hint": hint, "handles": []}
        elif not _mailish(one["name"], one["hint"]) and _mailish(name, hint):
            one["name"], one["hint"] = name, hint
        one["handles"].append(handle)
    return found


def carried_scan(store: Any, guard: Any, *, workers: int = 8, keep: bool = False,
                 now: Callable[[], str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Decide every stored container attachment by the mail it carries
    (``integrations.carried_mail``: the kept walk, else its bytes walked, in ``workers``
    processes) with ``guard`` now: (counts, ``{l1 handle: content status}`` of each
    attachment not cleared). ``keep``: the walks made are kept (``carried_people``).
    Never an address in either."""
    from integrations import carried_mail
    from integrations.capture_mail import (ATTACHMENT_CARRIED_UNDECIDED, ATTACHMENT_EXCLUDED)
    code = carried_mail.code()
    wanted = carried_candidates(store.db)
    counts = {"attachments": sum(len(v["handles"]) for v in wanted.values()),
              "contents": len(wanted), "kept_walks": 0, "walked": 0, "containers": 0,
              "carrying_mail": 0, "bytes_gone": 0, carried_mail.CLEAR: 0,
              carried_mail.EXCLUDED: 0, carried_mail.UNDECIDED: 0,
              "withheld_attachments": 0}
    walks: dict[str, Any] = {}
    todo = []
    for digest, one in wanted.items():
        kept = store.carried_walk(digest, code)
        if kept is not None and not (kept[0] is None and _mailish(one["name"], one["hint"])):
            walks[digest] = kept[0]
            counts["kept_walks"] += 1
        else:
            todo.append((digest, one["name"], one["hint"]))
    new: list[tuple[str, Any]] = []
    if todo:
        blobs = str(store.blobs)
        import multiprocessing
        # spawned, not forked: the caller may hold threads (a fork copies their locks)
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=max(1, workers),
                mp_context=multiprocessing.get_context("spawn"),
                **parent_watch.pool_init()) as pool:
            futures = [pool.submit(_walk_one, blobs, *job) for job in todo]
            for future in concurrent.futures.as_completed(futures):
                digest, encoded = future.result()
                counts["walked"] += 1
                if encoded is None:
                    counts["bytes_gone"] += 1
                    walks[digest] = ([], False)       # bytes gone: nothing cleared
                    continue
                walks[digest] = carried_mail.decode(encoded)
                new.append((digest, walks[digest]))
    withheld: dict[str, str] = {}
    for digest, found in walks.items():
        if found is not None:
            counts["containers"] += 1
            counts["carrying_mail"] += bool(found[0])
        verdict = carried_mail.decided(found, guard)
        counts[verdict.state] += 1
        if verdict.state == carried_mail.CLEAR:
            continue
        status = (ATTACHMENT_EXCLUDED if verdict.state == carried_mail.EXCLUDED
                  else ATTACHMENT_CARRIED_UNDECIDED)
        for handle in wanted[digest]["handles"]:
            withheld[handle] = status
    counts["withheld_attachments"] = len(withheld)
    if keep and new:
        counts["kept_new"] = store.keep_carried(new, code)
    counts["scanned_at"] = (now or _now)()
    return counts, withheld


def _carried_guard(args: argparse.Namespace, state_dir: Path, env: Mapping[str, str],
                   sender: Any) -> Any:
    """The people guard the carried scan decides with: bound through the GET-only Graph
    client (the capture's, deciding every address) with ``--live-audience``, else the
    environment's list with the capture's recorded directory (offline: a tenant address
    it does not hold stays undecided). None when it cannot decide at all."""
    from integrations import people_exclusion
    if args.live_audience:
        from integrations.graph_read import GraphMailReader, build_graph
        from integrations.live_source_guard import (BoundedBackoff, LiveSourceAudience,
                                                    SourceUnavailable)
        audience = LiveSourceAudience.from_file(args.live_audience)
        try:
            api, _transport = build_graph(env, sender=sender,
                                          retry=BoundedBackoff(budget_seconds=60))
            reader = GraphMailReader(api, audience)
            reader.prepare_people()
        except (SourceUnavailable, people_exclusion.ExclusionUnavailable):
            return None
        guard = getattr(reader, "people", None)
        return guard if getattr(guard, "bound", False) else None
    guard = people_exclusion.with_directory(
        people_exclusion.PeopleExclusion.from_env(env), state_dir)
    return None if guard.unavailable else guard


def _carried_cli(args: argparse.Namespace, state_dir: Path, env: Mapping[str, str],
                 sender: Any = None) -> int:
    from integrations.capture_mail import MailCaptureStore, _locked

    guard = _carried_guard(args, state_dir, env, sender)
    if guard is None:   # nothing decided, nothing written
        print(json.dumps({"ok": False, "error": "people_exclusion_unavailable"}))
        return 2
    out = Path(args.out) if args.out else state_dir / "mail-capture" / CARRIED_FILE

    def work(store: Any) -> dict[str, Any]:
        counts, withheld = carried_scan(store, guard, workers=args.workers, keep=args.apply)
        _save(out, {"format": CARRIED_FORMAT, "counts": counts, "withheld": withheld})
        return counts
    if not args.apply:
        store = MailCaptureStore.open_read_only(state_dir, timeout=60)
        try:
            counts = work(store)
        finally:
            store.close()
        print(json.dumps({"ok": True, **counts}, sort_keys=True))
        return 0
    return _locked(str(state_dir), work)


def _load_carried(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, Mapping) or data.get("format") != CARRIED_FORMAT \
            or not isinstance(data.get("withheld"), Mapping):
        raise ValueError("carried_file_invalid")
    return {str(h): str(s) for h, s in data["withheld"].items()}


# ----------------------------------------------------------------------------- purge

def captured(store: Any, pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """What a purge removes: the messages ``pairs`` names, every mailbox's copy of them
    (the same internet message id) and every stored copy of a message the capture
    remembered as withheld (``people_withheld``); the L1 handles of their versions and
    attachments, plus the handles a capture purge deleted whose derived copies are still to
    go (``purged_handles``): what every derived copy cites; and the ``people_withheld``
    keys of those messages (remembered on ``--apply``, so no later read stores a copy)."""
    remembered = store.remembered_copies()
    pairs = list(pairs)
    every = sorted({*(store.copies_of(pairs) if pairs else ()), *remembered})
    handles: set[str] = set()
    for box, mid in every:
        for (h,) in store.db.execute("SELECT l1_handle FROM messages WHERE mailbox_id=? AND "
                                     "message_id=?", (box, mid)):
            handles.add(h)
    for h in sorted(handles):
        handles.update(a for (a,) in store.db.execute(
            "SELECT l1_handle FROM attachments WHERE message_handle=? AND l1_handle IS NOT "
            "NULL", (h,)))
    journal = store.purged_handles()
    handles.update(journal)
    return {"pairs": every, "handles": sorted(h for h in handles if h),
            "keys": store.message_keys(every), "remembered": len(remembered),
            "journal": len(journal)}


def _load_handles(cur: Any, handles: Sequence[str]) -> None:
    cur.execute("CREATE TEMP TABLE purge_handles (h text PRIMARY KEY) ON COMMIT DROP")
    with cur.copy("COPY purge_handles (h) FROM STDIN") as copy:
        for h in handles:
            copy.write_row((h,))
    cur.execute("CREATE TEMP TABLE owned (object_id text PRIMARY KEY) ON COMMIT DROP")
    cur.execute("INSERT INTO owned SELECT DISTINCT object_id FROM ontology.object_versions "
                "WHERE l1_handle IN (SELECT h FROM purge_handles)")
    while True:   # what a removed message carries goes with it
        cur.execute("INSERT INTO owned SELECT DISTINCT from_object FROM ontology.link_rows "
                    "WHERE to_object IN (SELECT object_id FROM owned) AND link_type = "
                    "ANY(%s) ON CONFLICT DO NOTHING", (list(_CARRIED),))
        if not cur.rowcount:
            break
    cur.execute("ANALYZE owned")
    cur.execute("ANALYZE purge_handles")


def purge_pg(dsn: str, handles: Sequence[str], *, apply: bool) -> dict[str, Any]:
    """Postgres: the objects citing ``handles`` (and what they carry), their rows, the
    events cited on those records and their search rows. Dry run: counts only."""
    import psycopg

    from operating.pg.ontology_delta import _DELETE

    report: dict[str, Any] = {}
    with psycopg.connect(dsn, application_name="plx-mail-exclusion-purge") as pg, \
            pg.cursor() as cur:
        if apply:   # not beside a delta layer being applied
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
        _load_handles(cur, handles)
        cur.execute("SELECT count(*) FROM owned")
        report["objects"] = cur.fetchone()[0]
        chunk_where = ("object_id IN (SELECT object_id FROM owned) OR split_part("
                       "source_handle, '#', 1) IN (SELECT h FROM purge_handles) OR "
                       "source_handle IN (SELECT h FROM purge_handles)")
        events = ("event_id IN (SELECT event_id FROM ontology.events WHERE l1_handle IN "
                  "(SELECT h FROM purge_handles))")
        steps = [("ontology", "event_objects", events), ("ontology", "events", events),
                 *(("ontology", t, w) for t, w in _DELETE),
                 ("search", "chunks", chunk_where),
                 ("search", "entities", "object_id IN (SELECT object_id FROM owned)")]
        for schema, table, where in steps:
            key = f"{schema}.{table}"
            cur.execute("SELECT to_regclass(%s)", (key,))
            if cur.fetchone()[0] is None:   # a database without the search schema
                continue
            if apply:
                cur.execute(f"DELETE FROM {key} WHERE {where}")
            else:
                cur.execute(f"SELECT count(*) FROM {key} WHERE {where}")
            n = cur.rowcount if apply else cur.fetchone()[0]
            report[key] = report.get(key, 0) + n
        if apply:
            cur.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                        (f"purge:mail-exclusion:{_now()}",
                         json.dumps({"objects": report["objects"],
                                     "handles": len(handles)})))
        else:
            pg.rollback()
    return report


def purge_sqlite(path: Path, handles: Sequence[str], *, apply: bool) -> dict[str, Any]:
    """One SQLite ontology store (a generation's ``store.sqlite``, its ``objects.sqlite``
    print index, or a delta layer): the objects citing ``handles`` and what they carry,
    with every row of theirs. Dry run: opened read-only, counts only."""
    from ontology.text_index import _delete_field

    uri = f"file:{path}" + ("" if apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=120, isolation_level=None)
    report: dict[str, Any] = {"store": path.name}
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        if "object_versions" not in tables:
            if "objects" in tables and "bindings" in tables:   # a generation's print index
                return {**report, **_purge_prints(conn, handles, apply=apply)}
            return {**report, "skipped": "not_an_ontology_store"}
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TEMP TABLE purge_handles (h TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO temp.purge_handles VALUES (?)",
                         [(h,) for h in handles])
        conn.execute("CREATE TEMP TABLE owned (object_id TEXT PRIMARY KEY)")
        for h in handles:
            conn.execute("INSERT OR IGNORE INTO temp.owned SELECT object_id FROM "
                         "object_versions WHERE l1_handle = ?", (h,))
        frontier = [r[0] for r in conn.execute("SELECT object_id FROM temp.owned")]
        while frontier:
            found = []
            for oid in frontier:
                for kind in _CARRIED:
                    found += [r[0] for r in conn.execute(
                        "SELECT from_object FROM link_rows WHERE to_object = ? AND "
                        "link_type = ?", (oid, kind))]
            frontier = []
            for oid in found:
                if conn.execute("INSERT OR IGNORE INTO temp.owned VALUES (?)",
                                (oid,)).rowcount:
                    frontier.append(oid)
        owned = [r[0] for r in conn.execute("SELECT object_id FROM temp.owned")]
        report["objects"] = len(owned)
        counts: dict[str, int] = {}

        def run(table: str, sql: str, params: Sequence[Any]) -> None:
            if apply:
                n = conn.execute(f"DELETE FROM {table} WHERE {sql}", params).rowcount
            else:
                n = conn.execute(f"SELECT count(*) FROM {table} WHERE {sql}",
                                 params).fetchone()[0]
            counts[table] = counts.get(table, 0) + n

        for oid in owned:
            events = [r[0] for r in conn.execute(
                "SELECT event_id FROM event_objects WHERE object_id = ?", (oid,))]
            for event in events:
                run("event_objects", "event_id = ?", (event,))
                run("events", "event_id = ?", (event,))
            run("link_rows", "from_object = ?", (oid,))
            run("link_rows", "to_object = ?", (oid,))
            versions = [r[0] for r in conn.execute(
                "SELECT rowid FROM object_versions WHERE object_id = ?", (oid,))]
            for ver in versions:
                for (src,) in conn.execute("SELECT src FROM value_sources WHERE ver = ?",
                                           (ver,)).fetchall():
                    run("value_cells", "src = ?", (src,))
                run("value_sources", "ver = ?", (ver,))
            run("identifiers", "object_id = ?", (oid,))
            for (handle,) in conn.execute("SELECT l1_handle FROM object_versions WHERE "
                                          "object_id = ?", (oid,)).fetchall():
                fields = conn.execute(
                    "SELECT field_id FROM text_fields WHERE object_id = ? AND (citation = ? "
                    "OR (citation > ? AND citation < ?))",
                    (oid, handle, handle + "#", handle + "$")).fetchall()
                counts["text_fields"] = counts.get("text_fields", 0) + len(fields)
                if apply:
                    for (field_id,) in fields:
                        _delete_field(conn, field_id)
            run("object_versions", "object_id = ?", (oid,))
            run("object_removals", "object_id = ?", (oid,))
            if "delta_owned" in tables:
                run("delta_owned", "object_id = ?", (oid,))
            run("objects", "object_id = ?", (oid,))
        if apply:
            conn.execute("COMMIT")
        report.update(counts)
        return report
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _purge_prints(conn: sqlite3.Connection, handles: Sequence[str], *, apply: bool
                  ) -> dict[str, Any]:
    """A generation's ``objects.sqlite`` (object ids and their prints, no content): the
    Email objects of the removed message versions (the ids their handles name)."""
    from ontology.project import object_id_of

    ids = set()
    for h in handles:
        rest = h.split("://", 1)[-1].split("@", 1)[0]
        if "#" in h or rest.count("/") != 1:
            continue
        box, mid = rest.split("/")
        from urllib.parse import unquote
        ids.add(object_id_of("Email", "graph_mail.messages",
                             {"id": unquote(mid), "mailbox": unquote(box)}))
    sql = "FROM objects WHERE binding = 'graph_mail.messages' AND object_id = ?"
    if not apply:
        return {"objects": sum(conn.execute(f"SELECT count(*) {sql}", (i,)).fetchone()[0]
                               for i in sorted(ids))}
    conn.execute("BEGIN IMMEDIATE")
    n = sum(conn.execute(f"DELETE {sql}", (i,)).rowcount for i in sorted(ids))
    conn.execute("COMMIT")
    return {"objects": n}


def default_stores(state_dir: Path) -> list[Path]:
    """The served generations' stores and print indexes, and the delta layers."""
    projection = state_dir / "projection"
    found = sorted(projection.glob("generations/*/store.sqlite"))
    found += sorted(projection.glob("generations/*/objects.sqlite"))
    found += sorted(projection.glob("delta/delta-*.sqlite"))
    return found


def purge(store: Any, pairs: Sequence[tuple[str, str]], *, dsn: str | None,
          stores: Sequence[Path], apply: bool,
          carried: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Postgres, then the SQLite stores, then the capture store (last: a purge cut short
    is run again from the same matches and journal, which the capture store still
    resolves). The capture store remembers the messages' keys before deleting them and
    forgets the journalled handles only after every derived copy is gone. ``carried``:
    the attachments the carried-mail scan withholds (``{l1 handle: content status}``),
    whose derived copies go too and whose rows the capture store withholds in place."""
    plan = captured(store, pairs)
    carried = dict(carried or {})
    plan["handles"] = sorted({*plan["handles"], *carried})
    report: dict[str, Any] = {"apply": apply, "matched": len(pairs),
                              "messages_with_copies": len(plan["pairs"]),
                              "remembered_copies": plan["remembered"],
                              "journalled_handles": plan["journal"],
                              "carried_attachments": len(carried),
                              "handles": len(plan["handles"])}
    if not plan["handles"]:
        return report
    if dsn:
        report["postgres"] = purge_pg(dsn, plan["handles"], apply=apply)
    report["sqlite"] = [purge_sqlite(p, plan["handles"], apply=apply) for p in stores]
    if apply:
        report["withheld_keys_new"] = store.remember_withheld(plan["keys"])
        report["capture"] = store.purge_messages(plan["pairs"])
        report["carried_capture"] = store.withhold_attachments(carried)
        report["journal_forgotten"] = store.forget_purged(plan["handles"])
    else:
        report["withheld_keys"] = len(plan["keys"])
        report["capture"] = {"messages": len(plan["pairs"]), "versions": sum(
            store.db.execute("SELECT count(*) FROM messages WHERE mailbox_id=? AND "
                             "message_id=?", p).fetchone()[0] for p in plan["pairs"])}
    return report


# ------------------------------------------------------------------------------- CLI

def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.mail_exclusion_purge",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--state-dir", required=True)
    s.add_argument("--live-audience", required=True)
    s.add_argument("--principal", default=None)
    s.add_argument("--out", default=None)
    s.add_argument("--workers", type=int, default=8)
    p = sub.add_parser("purge")
    p.add_argument("--state-dir", required=True)
    p.add_argument("--matches", default=None)
    p.add_argument("--dsn", default=None)
    p.add_argument("--no-postgres", action="store_true")
    p.add_argument("--store", action="append", default=None)
    p.add_argument("--carried", default=None)
    p.add_argument("--apply", action="store_true")
    c = sub.add_parser("carried")
    c.add_argument("--state-dir", required=True)
    c.add_argument("--out", default=None)
    c.add_argument("--workers", type=int, default=os.cpu_count() or 8)
    c.add_argument("--live-audience", default=None)
    c.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir).expanduser()
    env = os.environ if environ is None else environ
    capture_db = state_dir / "mail-capture" / "capture.db"
    if not capture_db.exists():
        print(json.dumps({"ok": False, "error": "mail_capture_missing"}))
        return 2
    try:
        if args.command == "scan":
            return _scan_cli(args, state_dir, capture_db, env, sender)
        if args.command == "carried":
            return _carried_cli(args, state_dir, env, sender)
        return _purge_cli(args, state_dir, env)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc).split(":")[0][:80]}))
        return 2


def _scan_cli(args: argparse.Namespace, state_dir: Path, capture_db: Path,
              env: Mapping[str, str], sender: Any) -> int:
    from integrations.capture_mail import CAPTURE_PRINCIPAL
    from integrations.graph_read import GraphMailReader, build_graph
    from integrations.live_source_guard import (BoundedBackoff, LiveSourceAudience,
                                                SourceUnavailable)
    from integrations.people_exclusion import ExclusionUnavailable
    from ontology.core import Actor

    audience = LiveSourceAudience.from_file(args.live_audience)
    try:
        api, _transport = build_graph(env, sender=sender,
                                      retry=BoundedBackoff(budget_seconds=60))
    except SourceUnavailable:   # no credentials: nothing is read
        print(json.dumps({"ok": False, "error": "graph_unavailable"}))
        return 2
    reader = GraphMailReader(api, audience)
    actor = Actor(audience.tenant, frozenset({args.principal or CAPTURE_PRINCIPAL}),
                  "restricted")
    out = Path(args.out) if args.out else capture_db.parent / SCAN_FILE
    db = sqlite3.connect(f"file:{capture_db}?mode=ro", uri=True, timeout=60,
                         check_same_thread=False)
    try:
        counts = scan(db, reader, actor, out, workers=args.workers)
    except ExclusionUnavailable:
        print(json.dumps({"ok": False, "error": "people_exclusion_unavailable"}))
        return 2
    finally:
        db.close()
    print(json.dumps({"ok": True, **counts}, sort_keys=True))
    return 0 if counts["complete"] else 1   # run it again: it resumes


def _purge_cli(args: argparse.Namespace, state_dir: Path, env: Mapping[str, str]) -> int:
    from integrations.capture_mail import _locked

    path = Path(args.matches) if args.matches else \
        state_dir / "mail-capture" / SCAN_FILE
    if args.matches and not path.exists():
        print(json.dumps({"ok": False, "error": "matches_missing"}))
        return 2
    # no scan yet: the remembered copies and the journal are still purged
    data = _load(path)
    pairs = [(str(b), str(m)) for b, m in data["matches"]]
    carried_path = Path(args.carried) if args.carried else \
        state_dir / "mail-capture" / CARRIED_FILE
    if args.carried and not carried_path.exists():
        print(json.dumps({"ok": False, "error": "carried_missing"}))
        return 2
    carried = _load_carried(carried_path)
    dsn = None if args.no_postgres else (args.dsn or env.get("PLX_PG_DSN")
                                         or "postgresql:///plx")
    stores = [Path(p) for p in args.store] if args.store is not None \
        else default_stores(state_dir)

    def work(store: Any) -> dict[str, Any]:
        report = purge(store, pairs, dsn=dsn, stores=stores, apply=args.apply,
                       carried=carried)
        counts = data.get("counts") or {}
        report["scan"] = "absent" if not path.exists() else (
            "complete" if counts.get("complete") else "incomplete")
        # stored messages Graph no longer holds: never re-decided, so never purged here
        report["scan_absent_messages"] = counts.get("absent", 0)
        return report
    if not args.apply:
        # a dry run reads only: the capture store opened read-only, no cycle lock taken
        from integrations.capture_mail import MailCaptureStore
        store = MailCaptureStore.open_read_only(state_dir, timeout=60)
        try:
            report = work(store)
        finally:
            store.close()
        print(json.dumps({"ok": True, **report}, sort_keys=True))
        return 0
    return _locked(str(state_dir), work)


if __name__ == "__main__":
    sys.exit(main())
