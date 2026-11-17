"""Read-side guard: PLX's own sales documents the served store holds as supplier documents.

The served store classifies some of PLX's OWN Sage order-entry documents (sales invoices and
order confirmations it sends to its customers, on its letterhead with its HST number) as a
supplier's: ``SupplierInvoice`` / ``SupplierConfirmation`` objects whose seller is blank or
wrong and whose "supplier" is in truth PLX's customer. The classifier fix (which
dismisses such a class) waits for its migration; until it has rewritten the rows, every
reader is kept from seeing these objects, exactly as the migration will remove them.

Which objects (:func:`own_issued`): the evidence is the reference rule's strongest one, the
line Sage OE prints on each sales document it issues, its marker as a word of its own then
bracketed values, the first naming one of its sales forms ('PB#INFO [OEINV],[],[IN1000...],
[2,107.68],[CROW05],...'; :data:`MARKER`, :data:`FORMS`). An object is PLX-issued only when
EVERY title cue of its class (``ontology.document_fields`` title and numbered-title patterns,
the ones the served classification read) stands on a page carrying such a line, so a
supplier's invoice sent together with one of PLX's own pages (a freight pack, a reply with
our confirmation attached behind) keeps its supplier pages. A supplier returning PLX's own
purchase order prints 'PB#INFO [PETPO]', which marks nothing. The letterhead-only half of the
reference rule (an own name heading the issuer block, with no record line) is NOT applied
here: its hand-read precision fell short of zero wrong (fields-mig handoff: 3 of 25 wrong
before ca199ff4b2/685699f988), so a page shows those as the store holds them.

The decision is keyed on the rule, never on a sample's ids: a read-only pass over EVERY
supplier-typed object of the served store and the capture stores' kept readings
(``python3 -m operating.pg.own_issued build``) writes ``ontology.own_issued_documents``
(migration 0038): one row per hidden object, with its document's digest and the evidence,
and ``ontology.own_issued_judged``: every digest judged, PLX's own or not. The
incremental pass (``refresh``, ``plx-own-issued.timer`` every 15 minutes) judges only the
documents of supplier-typed objects no pass judged yet (the ones the delta added) and adds
their rows, so a new PLX-issued document is withheld within about 20 minutes of the delta
holding it (the readers' ``REFRESH_S`` after the pass). Pages never read text.
Nothing else is written: no ``objects``, value or link row changes, no step reads the table,
so no digest moves. A reader process loads the ids once and refreshes them in the
background (:func:`hidden_ids`); ``operating.search.document_marks`` adds them to the ids
every visible-object predicate withholds (record pages, ``plx.find``/``object``/``around``/
``timeline``/``investigate``, process, aggregate, traverse, Ask facts, document lists) for
every reader, the owner included. Only when Postgres serves (``PLX_ONTOLOGY_PG``): the ids
are its object ids. An unreadable or missing table hides nothing (the store as it is); a
table once read stays applied while a refresh fails.

Undo: TRUNCATE ontology.own_issued_documents, ontology.own_issued_judged (readers drop the ids within ``REFRESH_S``).
Remove the guard once the fields-mig migration has run (the table then names no live
object of a supplier type: ``verify`` reports it).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

TABLE = "ontology.own_issued_documents"
JUDGED = "ontology.own_issued_judged"   # every digest judged (``refresh`` skips them)
# The supplier-document classes the guard decides (object type -> document_fields profile).
TYPES = {"SupplierInvoice": "supplier_invoice", "SupplierConfirmation": "supplier_confirmation"}
# The Sage OE record line (fields-mig reference: own_parties.record_lines): the marker as a
# word of its own and PLX's own sales forms (invoice, order confirmation, packing slip of
# its Canadian and US companies). PETPO (a purchase order PLX sent, returned by a
# supplier) is not one.
MARKER = "PB#INFO"
FORMS = frozenset({"OEINV", "OEA1INV", "OECONF", "OEA1CONF", "OEPACK", "OEA1PACK"})
RULE = "own_record_line"
_LINE = re.compile(r"(?<![\w#])" + re.escape(MARKER) +
                   r"[ \t]*(\[[^\]\n]*\](?:[ \t]*,?[ \t]*\[[^\]\n]*\])*)")
_SLOT = re.compile(r"\[([^\]\n]*)\]")
_SHA = re.compile(r"[?&#]sha256=([0-9a-f]{64})(?:&|$)")
ENV = "PLX_ONTOLOGY_PG"
REFRESH_S = 300.0
RETRY_S = 60.0
# the capture stores (the host's ingest state, ``PLX_INGEST_STATE``)
STATE = os.environ.get("PLX_INGEST_STATE") or "/data/plx-state"
MAIL_DB = os.path.join(STATE, "mail-capture", "capture.db")
SHAREPOINT_DB = os.path.join(STATE, "sharepoint-capture", "capture.db")
BLOBS = {"mail": os.path.join(STATE, "mail-capture", "blobs", "sha256"),
         "sharepoint": os.path.join(STATE, "sharepoint-capture", "blobs", "sha256")}


# ------------------------------------------------------------------ the decision

def record_pages(pages: Iterable[tuple[int | None, str]]) -> dict[int, str]:
    """The pages (``page or 0``) carrying a Sage OE record line of one of :data:`FORMS`,
    each with the first such form."""
    found: dict[int, str] = {}
    for page, text in pages:
        if not isinstance(text, str) or MARKER not in text:
            continue
        for m in _LINE.finditer(text):
            slot = _SLOT.search(text, m.start(1), m.end(1))
            form = slot[1].strip().upper() if slot else ""
            if form in FORMS:
                found.setdefault(page or 0, form)
                break
    return found


def cue_pages(profile_id: str, pages: Sequence[tuple[int | None, str]]) -> set[int]:
    """The pages holding a title cue of class ``profile_id`` (every cue the served
    classification reads, a numbered title too)."""
    from ontology import document_fields as df
    found: set[int] = set()
    for patterns in (df._title_patterns(), df._numbered_title_patterns()):
        for pid, pattern in patterns:
            if pid != profile_id:
                continue
            for page, text in pages:
                if isinstance(text, str) and pattern.search(text):
                    found.add(page or 0)
    return found


def own_issued(object_type: str, pages: Sequence[tuple[int | None, str]]
               ) -> dict[str, Any] | None:
    """The evidence that the ``object_type`` document read as ``pages`` is PLX's own
    (module docstring), or None: an unknown type, a cue of its class on a page with no
    record line, or no cue while a page with text carries no record line. With no title
    cue this code reads (a heading the store's older reading took for the class, 'Order
    -Confirmation'), the document is PLX's own only when EVERY page with text carries a
    record line: nothing on it is anyone else's."""
    profile_id = TYPES.get(object_type)
    if profile_id is None:
        return None
    records = record_pages(pages)
    if not records:
        return None
    cues = cue_pages(profile_id, pages)
    if not cues:
        texts = {page or 0 for page, text in pages if isinstance(text, str) and text.strip()}
        if not texts or not texts <= set(records):
            return None
        return {"rule": RULE, "forms": sorted(set(records.values())), "cue_pages": [],
                "every_page": True}
    if not cues <= set(records):
        return None
    return {"rule": RULE, "forms": sorted({records[p] for p in cues}),
            "cue_pages": sorted(cues)}


def handle_sha(handle: Any) -> str | None:
    """The content digest an evidence handle names (``...sha256=<hex>``)."""
    m = _SHA.search(handle) if isinstance(handle, str) else None
    return m.group(1) if m else None


# ------------------------------------------------------------------ the read side

def serving() -> bool:
    """Whether Postgres serves the ontology (the table's ids are its object ids)."""
    return os.environ.get(ENV) in ("1", "only")


class _Hidden:
    """The hidden object ids, read in one statement per process and refreshed in the
    background; the empty set until a read succeeds."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.kept: tuple[float, frozenset[str]] | None = None
        self.failed_at: float | None = None
        self.refreshing = False

    @staticmethod
    def _read() -> frozenset[str]:
        import psycopg
        from operating.pg import config
        with psycopg.connect(config.dsn(), connect_timeout=5) as conn:
            if conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0] is None:
                return frozenset()
            rows = conn.execute(f"SELECT object_id FROM {TABLE}").fetchall()
        return frozenset(r[0] for r in rows if isinstance(r[0], str))

    def _load(self) -> frozenset[str] | None:
        try:
            got = self._read()
        except Exception:  # noqa: BLE001 - unreadable: keep what was read (none: none)
            with self.lock:
                self.failed_at, self.refreshing = time.monotonic(), False
            return None
        with self.lock:
            self.kept, self.failed_at, self.refreshing = (time.monotonic(), got), None, False
        return got

    def get(self) -> frozenset[str]:
        now = time.monotonic()
        with self.lock:
            kept, failed = self.kept, self.failed_at
            if kept is not None:
                if now - kept[0] >= REFRESH_S and not self.refreshing and (
                        failed is None or now - failed >= RETRY_S):
                    self.refreshing = True
                    threading.Thread(target=self._load, daemon=True,
                                     name="plx-own-issued").start()
                return kept[1]
            if failed is not None and now - failed < RETRY_S:
                return frozenset()
        got = self._load()
        return got if got is not None else frozenset()

    def forget(self) -> None:
        with self.lock:
            self.kept, self.failed_at, self.refreshing = None, None, False


_HIDDEN = _Hidden()


def hidden_ids() -> frozenset[str]:
    """The object ids every reader is kept from (empty unless Postgres serves)."""
    return _HIDDEN.get() if serving() else frozenset()


def forget() -> None:
    """Drop the loaded ids (the next read re-reads them): after a build, in tests."""
    _HIDDEN.forget()


# ------------------------------------------------------------------ the build

_OBJECTS_SQL = (
    "SELECT o.object_id, o.object_type, v.l1_handle FROM ontology.objects o "
    "JOIN ontology.object_values v ON v.object_id = o.object_id AND v.property = 'evidence' "
    "AND v.superseded_at IS NULL WHERE o.object_type = ANY(%s)")


def _capture_row(sha: str, mail: Any, sharepoint: Any) -> tuple[str, dict[str, Any]] | None:
    """(store, the ``captured_text`` row) of the captured file ``sha``."""
    got = mail.execute("SELECT metadata, text_status, text_sha256 FROM attachments "
                       "WHERE content_sha256 = ? LIMIT 1", (sha,)).fetchone()
    if got is not None:
        meta = json.loads(got[0])
        return "mail", {"content_sha256": sha, "content_type": meta.get("content_type"),
                        "name": meta.get("name"), "text_status": got[1],
                        "text_sha256": got[2]}
    got = sharepoint.execute("SELECT record, text_status, text_sha256 FROM items "
                             "WHERE content_sha256 = ? LIMIT 1", (sha,)).fetchone()
    if got is not None:
        record = json.loads(got[0])
        return "sharepoint", {"content_sha256": sha, "name": record.get("name"),
                              "content_type": (record.get("file") or {}).get("mimeType"),
                              "text_status": got[1], "text_sha256": got[2]}
    return None


_DBS: dict[str, Any] = {}


def _db(path: str) -> Any:
    if path not in _DBS:
        _DBS[path] = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    return _DBS[path]


def judge(task: tuple[str, tuple[str, ...]]) -> tuple[str, dict[str, Any] | None, str | None]:
    """One document (its digest, the types it is held as): (digest, {type: evidence}, an
    error)."""
    from integrations.captured_text import captured_text
    sha, types = task
    try:
        found = _capture_row(sha, _db(os.environ.get("PLX_OWN_MAIL_DB", MAIL_DB)),
                             _db(os.environ.get("PLX_OWN_SP_DB", SHAREPOINT_DB)))
        if found is None:
            return sha, None, "not_captured"
        read = captured_text(BLOBS[found[0]], found[1])
        if read is None:
            return sha, None, "no_text"
        pages = list(read.pages)
        out = {t: e for t in types if (e := own_issued(t, pages)) is not None}
        return sha, out, None
    except Exception as exc:  # noqa: BLE001 - one unreadable file decides nothing
        return sha, None, type(exc).__name__


def _judge_all(tasks: Sequence[tuple[str, tuple[str, ...]]], workers: int,
               decisions: Any = None) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Judge ``tasks`` (digest, types) in a process pool: (digest -> {type: evidence} of
    each document read, empty when not PLX's own; error name -> count). A document that
    could not be read is not in the first map (judged again on the next pass)."""
    judged: dict[str, dict[str, Any]] = {}
    errors: dict[str, int] = {}
    if not tasks:
        return judged, errors
    from concurrent.futures import ProcessPoolExecutor
    from ontology import parent_watch
    with ProcessPoolExecutor(max_workers=max(1, min(workers, len(tasks))),
                             **parent_watch.pool_init()) as pool:
        for sha, out, error in pool.map(judge, tasks, chunksize=64):
            if decisions is not None:
                decisions.write(json.dumps([sha, out, error], sort_keys=True) + "\n")
            if error:
                errors[error] = errors.get(error, 0) + 1
                continue
            judged[sha] = out or {}
    return judged, errors


def _hidden_rows(by_sha: Mapping[str, Sequence[tuple[str, str]]],
                 judged: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, str, str]]:
    """(object id, type, digest, evidence) of each held object whose document was judged
    PLX's own for its type."""
    rows = []
    for sha, objs in by_sha.items():
        out = judged.get(sha) or {}
        for object_id, object_type in objs:
            if object_type in out:
                rows.append((object_id, object_type, sha,
                             json.dumps(out[object_type], sort_keys=True)))
    return sorted(rows)


def _summary(tasks: int, judged: Mapping[str, Mapping[str, Any]],
             rows: Sequence[tuple[str, str, str, str]], errors: Mapping[str, int],
             started: float, **extra: Any) -> dict[str, Any]:
    return {"documents": tasks, "hidden_documents": len({r[2] for r in rows}),
            "own_documents_judged": sum(1 for out in judged.values() if out),
            "hidden_objects": len(rows),
            "by_type": {t: sum(1 for r in rows if r[1] == t) for t in sorted(TYPES)},
            "errors": dict(errors), "seconds": round(time.monotonic() - started, 1), **extra}


def build(conn: Any, *, workers: int, limit: int | None = None, write: bool = True,
          shas: set[str] | None = None, decisions: Any = None) -> dict[str, Any]:
    """Judge every supplier-typed object's document (for every type the guard decides, so a
    later object of the other type needs no new read) and (``write``) replace both tables'
    rows in one transaction."""
    started = time.monotonic()
    by_sha = {sha: objs for sha, objs in _objects(conn).items()
              if shas is None or sha in shas}
    tasks = [(sha, tuple(sorted(TYPES))) for sha in sorted(by_sha)]
    if limit is not None:
        tasks = tasks[:limit]
        by_sha = {sha: by_sha[sha] for sha, _ in tasks}
    judged, errors = _judge_all(tasks, workers, decisions)
    rows = _hidden_rows(by_sha, judged)
    if write:
        _write(conn, rows, judged)
    return _summary(len(tasks), judged, rows, errors, started, written=write)


def refresh(conn: Any, *, workers: int) -> dict[str, Any]:
    """The incremental pass (``plx-own-issued.timer``): judge only the documents of
    supplier-typed objects no pass judged yet (the ones the delta added), record them, and
    add a hidden row for every held object of a document judged PLX's own (a new object of
    an old document too). Rows are only added, in one transaction: a full ``build``
    replaces them. With nothing judged yet, it is a full build."""
    started = time.monotonic()
    if conn.execute(f"SELECT NOT EXISTS (SELECT 1 FROM {JUDGED})").fetchone()[0]:
        return dict(build(conn, workers=workers), mode="build")
    by_sha = _objects(conn)
    known: dict[str, dict[str, Any]] = {}
    unread: set[str] = set()   # judged own, its evidence not of the rule's shape
    for sha, evidence in conn.execute(f"SELECT content_sha256, evidence FROM {JUDGED} "
                                      "WHERE own").fetchall():
        try:
            got = json.loads(evidence) if evidence else {}
        except ValueError:
            got = {}
        # a stored judgement of another shape decides nothing (it is judged again)
        if isinstance(got, dict) and all(t in TYPES and isinstance(e, dict)
                                         and e.get("rule") == RULE for t, e in got.items()) \
                and got:
            known[sha] = got
        else:
            unread.add(sha)
    seen = {r[0] for r in conn.execute(f"SELECT content_sha256 FROM {JUDGED}").fetchall()}
    tasks = [(sha, tuple(sorted(TYPES))) for sha in sorted(by_sha)
             if sha not in seen or sha in unread]
    judged, errors = _judge_all(tasks, workers)
    rows = _hidden_rows(by_sha, {**known, **judged})
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {JUDGED} (content_sha256, own, evidence, judged_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (content_sha256) DO UPDATE SET "
                "own = EXCLUDED.own, evidence = EXCLUDED.evidence, "
                "judged_at = EXCLUDED.judged_at",
                [(sha, bool(out), json.dumps(out, sort_keys=True) if out else None, at)
                 for sha, out in sorted(judged.items())])
            cur.executemany(
                f"INSERT INTO {TABLE} (object_id, object_type, content_sha256, evidence, "
                "detected_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (object_id) DO NOTHING",
                [(*r, at) for r in rows])
    return _summary(len(tasks), judged, rows, errors, started, mode="refresh")


def _objects(conn: Any) -> dict[str, list[tuple[str, str]]]:
    """Every supplier-typed object by its document's digest."""
    by_sha: dict[str, list[tuple[str, str]]] = {}
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute(_OBJECTS_SQL, (sorted(TYPES),))
        for object_id, object_type, handle in cur:
            sha = handle_sha(handle)
            if sha is not None:
                by_sha.setdefault(sha, []).append((object_id, object_type))
    return by_sha


def _write(conn: Any, rows: Sequence[tuple[str, str, str, str]],
           judged: Mapping[str, Mapping[str, Any]] | None = None) -> None:
    """Replace the hidden rows with ``rows`` (and, given ``judged``, the judged digests
    with it) in one transaction."""
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with conn.transaction():
        conn.execute(f"TRUNCATE {TABLE}")
        with conn.cursor().copy(f"COPY {TABLE} (object_id, object_type, content_sha256, "
                                "evidence, detected_at) FROM STDIN") as copy:
            for row in rows:
                copy.write_row((*row, at))
        if judged is not None:
            conn.execute(f"TRUNCATE {JUDGED}")
            with conn.cursor().copy(f"COPY {JUDGED} (content_sha256, own, evidence, "
                                    "judged_at) FROM STDIN") as copy:
                for sha, out in sorted(judged.items()):
                    copy.write_row((sha, bool(out), json.dumps(dict(out), sort_keys=True)
                                    if out else None, at))


def apply(conn: Any, decisions: Iterable[str]) -> dict[str, Any]:
    """Write the hidden rows of a ``build --dry-run --decisions`` file against the objects
    the store holds now (a document's objects are its current supplier-typed ones; a
    decision line of any other shape stops the apply before anything is written)."""
    by_sha = _objects(conn)
    rows: list[tuple[str, str, str, str]] = []
    judged: dict[str, dict[str, Any]] = {}
    docs = 0
    for line in decisions:
        if not line.strip():
            continue
        got = json.loads(line)
        if not (isinstance(got, list) and len(got) == 3 and isinstance(got[0], str)
                and re.fullmatch(r"[0-9a-f]{64}", got[0])
                and (got[1] is None or isinstance(got[1], dict))):
            raise ValueError("own_issued_decision_invalid")
        sha, out, error = got
        if not error:
            judged[sha] = out or {}
        for object_type, evidence in (out or {}).items():
            if object_type not in TYPES or not isinstance(evidence, dict) \
                    or evidence.get("rule") != RULE:
                raise ValueError("own_issued_decision_invalid")
        held = [(o, t) for o, t in by_sha.get(sha, ()) if t in (out or {})]
        docs += bool(held)
        rows += [(o, t, sha, json.dumps(out[t], sort_keys=True)) for o, t in held]
    _write(conn, rows, judged)
    return {"hidden_documents": docs, "hidden_objects": len(rows),
            "by_type": {t: sum(1 for r in rows if r[1] == t) for t in sorted(TYPES)}}


def verify(conn: Any) -> dict[str, Any]:
    """How many hidden rows still name a live object of a supplier type (zero once the
    migration has removed them: then the guard can go)."""
    got = conn.execute(
        f"SELECT count(*), count(o.object_id) FROM {TABLE} h LEFT JOIN ontology.objects o "
        "ON o.object_id = h.object_id AND o.object_type = h.object_type").fetchone()
    return {"rows": got[0], "live_supplier_objects": got[1]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.own_issued")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="judge every supplier document; replace the table")
    b.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    b.add_argument("--limit", type=int)
    b.add_argument("--dry-run", action="store_true", help="judge, write nothing")
    b.add_argument("--if-migrated", action="store_true",
                   help="do nothing (exit 0) until migration 0038 made the table")
    b.add_argument("--shas", help="a file of content digests: judge only these (dry run)")
    b.add_argument("--decisions", help="write each document's decision here (JSON lines)")
    r = sub.add_parser("refresh", help="judge only documents no pass judged; add their rows")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--if-migrated", action="store_true",
                   help="do nothing (exit 0) until migration 0038 made the tables")
    a = sub.add_parser("apply", help="write a dry run's decisions (current objects)")
    a.add_argument("decisions")
    sub.add_parser("verify", help="rows and the live supplier objects they still name")
    args = parser.parse_args(argv)
    import psycopg
    from operating.pg import config
    with psycopg.connect(config.dsn(), autocommit=True,
                         application_name="plx-own-issued") as conn:
        if getattr(args, "if_migrated", False) and None in conn.execute(
                "SELECT to_regclass(%s), to_regclass(%s)", (TABLE, JUDGED)).fetchone():
            out = {"skipped": "table_missing"}
        elif args.command == "verify":
            out = verify(conn)
        elif args.command == "refresh":
            out = refresh(conn, workers=max(1, args.workers))
        elif args.command == "apply":
            with open(args.decisions, encoding="utf-8") as fh:
                out = apply(conn, list(fh))
        else:
            shas = None
            if args.shas:
                with open(args.shas, encoding="utf-8") as fh:
                    shas = {line.strip() for line in fh if line.strip()}
            sink = open(args.decisions, "w", encoding="utf-8") if args.decisions else None
            try:
                out = build(conn, workers=max(1, args.workers), limit=args.limit,
                            write=not (args.dry_run or shas is not None), shas=shas,
                            decisions=sink)
            finally:
                if sink is not None:
                    sink.close()
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
