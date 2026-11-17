"""Sheet readings: spreadsheet and PDF attachments and SharePoint files read once per file
version into dated, cited rows the alert rules read by index (``operating.proactive_sheets``,
``proactive_business.stock_out_risk``). A step of its own, outside the alert rules and the
delta cycle's projection step (whose code digest it leaves alone): it reads the text the
captures already stored for each file (``text_sha256``: the workbook extractor's
``[Sheet: <name>]`` and ``A1=value | B1=value`` lines, a CSV's own text, a PDF's text layer)
and never the business systems.

What a reading is
-----------------
Layouts are detected, never coded per file or sender. Each sheet's rows are scanned for a
header: a row of cells whose words map to column roles (``ROLES``: item, customer item,
lot, on hand, unit, expiry, invoice reference, amount paid, discount, payment reference and
date, invoice amount, balance, debit, credit, dates). A table's kind follows from its roles
and the words of its file (name, title rows):

* ``on_hand``: an item column and an on-hand column (a plain quantity column never: it is
  an order, an invoice or a quote as often as a stock list): one reading per item row (item, customer
  item, lot, quantity, unit, expiry), dated by the sheet's own date (a dated label or the
  first date above the header), else a date in the file name, else the day it was received
  (``date_basis`` says which);
* ``remittance``: an invoice reference with an amount paid (payment words, a payment
  reference or payment date column): one reading per invoice line (reference, amount paid,
  discount, invoice amount and date, payment reference and date);
* ``statement``: an invoice reference with an amount or balance in a file that says it is
  a statement or open-item list: one reading per line (reference, dates, amount, balance).

Which side a remittance or statement is on (a customer paying us, our own advice to a
supplier, a supplier's statement or one we sent) is not decided here: the rules decide it by
the records the references match. Every reading cites its cell (``Sheet!B7``, a CSV's
``B7``, a PDF's ``L12``) and its file version; amounts are ``Decimal`` text, never floats,
and carry a currency only when the file states one.

Sources, permissions and people
-------------------------------
A source is one captured file a mailbox or SharePoint drive holds (``sources``): its
handle, the store record that holds its text (``Attachment`` or ``SharePointFile``), that
record's audience and markings, the message's sender and recipients (mail) and when it was
received. A source whose record the store does not hold is kept but ``object_id`` NULL: the
rules never read it (no record to cite or to check a reader against). Nothing here decides
who may read: the rules cite the record, so a reader who may not open the attachment never
sees what it supports, and they clear the people on the message first (fail closed).

Incremental, parallel
---------------------
A file version is its ``content_sha256`` and ``text_sha256``; it is read once per reader code
version (``READER``, the digest of this module's source), on every core
(``ProcessPoolExecutor``), and written in one transaction per batch. A file whose text is
not stored yet (the attachments the OCR backlog defers) is not a source until its text
arrives: every run lists the captures again and reads only the new versions, so
``--follow`` picks them up as they land.

Evidence of a run over a capture is the capture's, read-only; fixture runs are
``isolated-fixture``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping, Sequence

from integrations import sheet_grid as _grid
from ontology import parent_watch
# How a sheet is read (column roles, values, grids, tables, which files): its own module,
# so the alert rules import it without this pass's store and ontology readers.
from integrations.sheet_grid import (_letters, amount, form_of, parse_date, read_text)
from operating import declared_schema as _schemas
from operating.declared_schema import sheet_readings as _SHEET_READINGS

# this module's source and the grid reader's (``sheet_grid``): a change to either reads
# every file again
READER = "sheet-readings/" + hashlib.sha256(
    Path(__file__).read_bytes() + Path(_grid.__file__).read_bytes()).hexdigest()[:12]
KINDS = ("on_hand", "remittance", "statement")

# ----------------------------------------------------------------------------- store



def open_readings(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    _schemas.apply(conn, _SHEET_READINGS)
    return conn


@dataclass(frozen=True)
class Source:
    handle: str
    origin: str             # mail | sharepoint
    name: str
    content_sha256: str
    text_sha256: str
    received_at: str | None
    participants: tuple[str, ...] = ()
    message: str | None = None

    @property
    def file_key(self) -> str:
        return f"{self.content_sha256}:{self.text_sha256}"


def mail_sources(state: Path) -> Iterator[Source]:
    """Every captured mail attachment with stored text whose name is a sheet or a family PDF;
    its message's sender and recipients (lowercase addresses) and received time."""
    db = sqlite3.connect(f"file:{state / 'mail-capture' / 'capture.db'}?mode=ro", uri=True,
                         timeout=60)
    try:
        rows = db.execute(
            "SELECT a.l1_handle, json_extract(a.metadata, '$.name'), a.content_sha256, "
            "a.text_sha256, a.message_handle FROM attachments a WHERE a.content_status = "
            "'captured' AND a.text_sha256 IS NOT NULL AND a.l1_handle IS NOT NULL")
        wanted = [r for r in rows if r[1] and form_of(r[1])]
        from integrations.capture_mail import undecided_free_sql
        free = undecided_free_sql(db)
        withheld: set[str] = set()
        messages: dict[str, tuple[str | None, tuple[str, ...]]] = {}
        handles = sorted({r[4] for r in wanted})
        for i in range(0, len(handles), 500):
            for handle, record in db.execute(
                    "SELECT l1_handle, record FROM messages WHERE superseded_at IS NULL AND "
                    "l1_handle IN (SELECT value FROM json_each(?))",
                    (json.dumps(handles[i:i + 500]),)):
                messages[handle] = _participants(record)
            if free != "1":   # a message naming an undecided address is no reader's
                withheld.update(h for (h,) in db.execute(
                    "SELECT DISTINCT l1_handle FROM messages WHERE l1_handle IN (SELECT "
                    f"value FROM json_each(?)) AND NOT {free}",
                    (json.dumps(handles[i:i + 500]),)))
        for handle, name, content, text, message in wanted:
            if message in withheld:
                continue
            received, people = messages.get(message, (None, ("",)))
            yield Source(handle, "mail", name, content, text, received, people, message)
    finally:
        db.close()


def _participants(record: str) -> tuple[str | None, tuple[str, ...]]:
    try:
        rec = json.loads(record)
    except ValueError:
        return None, ("",)
    found: list[str] = []
    for key in ("from", "to", "cc"):
        value = rec.get(key)
        for one in (value if isinstance(value, list) else [value]):
            if not one:
                continue
            addr = one.get("emailAddress", one) if isinstance(one, dict) else {"address": one}
            text = str(addr.get("address") or "").strip().lower() \
                if isinstance(addr, dict) else str(addr).strip().lower()
            found.append(text)    # "" (no address) never clears downstream
    return rec.get("received_at") or rec.get("sent_at"), tuple(dict.fromkeys(found))


def sharepoint_sources(state: Path) -> Iterator[Source]:
    db = sqlite3.connect(f"file:{state / 'sharepoint-capture' / 'capture.db'}?mode=ro",
                         uri=True, timeout=60)
    try:
        for handle, path, content, text, at in db.execute(
                "SELECT l1_handle, path, content_sha256, text_sha256, json_extract(record, "
                "'$.last_modified') FROM items WHERE superseded_at IS NULL AND removed_reason "
                "IS NULL AND text_sha256 IS NOT NULL AND content_sha256 IS NOT NULL"):
            name = path.rsplit("/", 1)[-1]
            if form_of(name):
                yield Source(handle, "sharepoint", name, content, text, at)
    finally:
        db.close()


def _blob(dirs: Sequence[Path], digest: str) -> bytes | None:
    from integrations.captured_text import read_blob
    for d in dirs:
        got = read_blob(d, digest)
        if got is not None:
            return got
    return None


MAX_PDF_PAGES = 12          # pages of a PDF laid out (a remittance or statement is short)
MAX_PDF_BYTES = 20_000_000  # a larger PDF is read by its stored text only
PDF_SECONDS = 30            # wall time for one PDF's layout; past it the stored text is read


_LAYOUT_CHILD = """
import io, sys
from pypdf import PdfReader
reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()))
pages = [p.extract_text(extraction_mode="layout") or "" for p in reader.pages[:%d]]
sys.stdout.write("\\f".join(pages))
""" % MAX_PDF_PAGES
PDF_CHILD_BYTES = 2 << 30   # address space of one PDF's layout process


def _child_limits() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (PDF_CHILD_BYTES, PDF_CHILD_BYTES))


def pdf_layout(content: bytes) -> str | None:
    """A PDF's pages laid out by position (pypdf's layout mode keeps columns apart), pages
    joined by ``\\f``; None when pypdf is not installed, the file does not open, or it runs
    past ``PDF_SECONDS`` (the capture's stored text is read instead). The layout runs in a
    child process of its own (bounded memory, killed at the time limit): pypdf can
    stall in native code where no in-process timer reaches it."""
    import subprocess
    try:
        done = subprocess.run([sys.executable, "-c", _LAYOUT_CHILD], input=content,
                              capture_output=True, timeout=PDF_SECONDS,
                              preexec_fn=_child_limits, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace") or None


def _read_one(job: tuple) -> tuple[str, str, list, float]:
    """Worker: one file version's tables (``status``: read, no_table, text_missing,
    unreadable). A PDF is laid out from its bytes (``pdf_layout``) when they are held."""
    file_key, name, text_digest, dirs = job[:4]
    started = time.monotonic()
    if name.lower().endswith(".pdf") and len(job) > 4:
        content = _blob([Path(d) for d in dirs], job[4])
        laid = pdf_layout(content) if content is not None and len(content) <= MAX_PDF_BYTES \
            else None
        if laid:
            try:
                found = read_text(laid, "layout", name)
            except Exception as exc:   # noqa: BLE001 - counted, never stops a run
                return file_key, "unreadable:" + type(exc).__name__, [], \
                    time.monotonic() - started
            if found:
                return file_key, "read", found, time.monotonic() - started
    raw = _blob([Path(d) for d in dirs], text_digest)
    if raw is None:
        return file_key, "text_missing", [], time.monotonic() - started
    try:
        found = read_text(raw.decode("utf-8", "replace"), form_of(name) or "text", name)
    except Exception as exc:   # noqa: BLE001 - one bad file never stops a run (counted)
        return file_key, "unreadable:" + type(exc).__name__, [], time.monotonic() - started
    return file_key, ("read" if found else "no_table"), found, time.monotonic() - started


WAVE = 512                      # file versions handed to the workers at once
WAVE_SECONDS = 4 * PDF_SECONDS + 120   # a wave not done by then: its stragglers are dropped


def _results(todo: list[tuple], width: int) -> Iterator[tuple[str, str, list, float]]:
    """Each job's reading, on ``width`` worker processes in waves; a file whose worker
    has not answered within ``WAVE_SECONDS`` of its wave (a reader stuck in native code)
    is ``timed_out`` and its workers are replaced, so one file never stalls a pass."""
    if width == 1 or len(todo) < 64:
        yield from map(_read_one, todo)
        return
    from concurrent.futures import wait
    pool = ProcessPoolExecutor(width, **parent_watch.pool_init())
    try:
        for start in range(0, len(todo), WAVE):
            wave = {pool.submit(_read_one, job): job for job in todo[start:start + WAVE]}
            done, late = wait(wave, timeout=WAVE_SECONDS)
            for fut in done:
                try:
                    yield fut.result()
                except Exception as exc:   # noqa: BLE001 - a dead worker: counted
                    yield wave[fut][0], "unreadable:" + type(exc).__name__, [], 0.0
            if late:
                for fut in late:
                    yield wave[fut][0], "timed_out", [], float(WAVE_SECONDS)
                for proc in list(getattr(pool, "_processes", {}).values()):
                    proc.kill()          # this pass's own workers only
                pool.shutdown(wait=False, cancel_futures=True)
                pool = ProcessPoolExecutor(width, **parent_watch.pool_init())
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _store_conn(store: Path | None) -> Any:
    """The ontology the sources' records are resolved in: Postgres when it serves the
    generation at ``store`` (``PLX_ONTOLOGY_PG``; ``only``: always, with no generation
    file), through the compat layer that runs ``_record_of``'s SQL as written; else the
    SQLite generation file; else None (no store: no source has a record). With Postgres
    the only store and not whole, the pass stops here (``store_not_whole``) and the
    sources keep the records they had, never rewritten without one."""
    from operating.pg import ontology_reader
    if ontology_reader.pg_only():
        problem = ontology_reader.whole()
        if problem is not None:
            raise RuntimeError(f"store_not_whole:{problem}")
    if ontology_reader.serves(store):
        from operating.pg.sqlite_compat import PgCompatConnection
        return PgCompatConnection(ontology_reader.dsn())
    if store is not None and Path(store).is_file():
        return sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=60)
    return None


def _resolve(store: Any, handles: Sequence[str]
             ) -> tuple[dict[str, tuple[str, str, str, str]], set[str]]:
    """(``_record_of`` of ``handles``, the handles left unsure). On Postgres each batch is
    followed by a check that Postgres was whole while it was read (a load emptying tables
    mid-pass); a handle of a batch read while it was not, and found in no record, is unsure:
    its source is skipped this pass (it keeps the record it had), never written without
    one. A handle found keeps its record (a row read is a row held)."""
    if store is None or isinstance(store, sqlite3.Connection):
        return _record_of(store, handles), set()
    from operating.pg import ontology_reader
    out: dict[str, tuple[str, str, str, str]] = {}
    unsure: set[str] = set()
    for i in range(0, len(handles), RESOLVE_BATCH):
        batch = handles[i:i + RESOLVE_BATCH]
        found = _record_of(store, batch)
        out.update(found)
        if ontology_reader.whole() is not None:
            unsure.update(h for h in batch if h not in found)
    return out, unsure


# sources whose records are read between two checks that Postgres is whole
RESOLVE_BATCH = 500


def _record_of(store: sqlite3.Connection | None, handles: Sequence[str]
               ) -> dict[str, tuple[str, str, str, str]]:
    """handle -> (object id, type, audience, markings) of the store record holding its text,
    else of the Attachment or SharePointFile record whose current version cites it."""
    out: dict[str, tuple[str, str, str, str]] = {}
    if store is None:
        return out
    for i in range(0, len(handles), 500):
        for handle, oid, typ, aud, marks in store.execute(
                "SELECT t.citation, t.object_id, o.object_type, o.audience_key, t.markings "
                "FROM text_fields t JOIN objects o ON o.object_id = t.object_id WHERE "
                "t.citation IN (SELECT value FROM json_each(?)) AND t.field_path = "
                "'row.content_text'", (json.dumps(list(handles[i:i + 500])),)):
            out.setdefault(handle, (oid, typ, aud, marks))
    # a file whose text the store does not index (most PDFs): its record by the version
    # that cites the handle, at that version's markings
    rest = [h for h in handles if h not in out]
    for i in range(0, len(rest), 500):
        # two index reads (versions by handle, then their records by id): one join let
        # the planner walk the whole record table (a pass stalled 40 minutes on gen 11)
        versions = store.execute(
            "SELECT l1_handle, object_id, markings FROM object_versions WHERE l1_handle IN "
            "(SELECT value FROM json_each(?)) AND superseded_at IS NULL",
            (json.dumps(rest[i:i + 500]),)).fetchall()
        records = {oid: (typ, aud) for oid, typ, aud in store.execute(
            "SELECT object_id, object_type, audience_key FROM objects WHERE object_id IN "
            "(SELECT value FROM json_each(?))", (json.dumps([v[1] for v in versions]),))}
        for handle, oid, marks in versions:
            typ, aud = records.get(oid, (None, None))
            if typ in ("Attachment", "SharePointFile"):
                out.setdefault(handle, (oid, typ, aud, marks))
    return out


def run(state: Path, out: Path, *, store: Path | None, workers: int | None = None,
        sources: Iterable[Source] | None = None, blob_dirs: Sequence[Path] | None = None,
        now: datetime | None = None, limit: int | None = None,
        records: Mapping[str, tuple[str, str, str, str]] | None = None) -> dict[str, Any]:
    """One incremental pass: list the sources, read the new file versions in parallel, store
    their tables and readings, and (re)resolve each source's store record (``records``:
    handle -> (object id, type, audience, markings) given instead). Counts only."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    started = time.monotonic()
    listed = list(sources) if sources is not None else [
        *mail_sources(state), *sharepoint_sources(state)]
    dirs = tuple(str(d) for d in (blob_dirs or (
        state / "mail-capture" / "blobs" / "sha256", state / "mail-capture" / "blobs.nvme" / "sha256",
        state / "sharepoint-capture" / "blobs" / "sha256", state / "sharepoint-capture" / "blobs")))
    conn = open_readings(out)
    done = {k for (k,) in conn.execute("SELECT file_key FROM files WHERE reader = ?", (READER,))}
    jobs: dict[str, tuple] = {}
    for s in listed:
        if s.file_key not in done and s.file_key not in jobs:
            jobs[s.file_key] = (s.file_key, s.name, s.text_sha256, dirs, s.content_sha256)
    todo = list(jobs.values())[:limit] if limit is not None else list(jobs.values())
    counts: dict[str, Any] = {"sources": len(listed), "versions_new": len(todo),
                              "versions_known": len(done)}
    status: dict[str, int] = {}
    kinds: dict[str, int] = {}
    width = max(1, workers or os.cpu_count() or 1)

    def store_batch(batch: list[tuple[str, str, list, float]]) -> None:
        with conn:
            for key, st, found, secs in batch:
                status[st.split(":")[0]] = status.get(st.split(":")[0], 0) + 1
                conn.execute("DELETE FROM tables WHERE file_key = ?", (key,))
                conn.execute("DELETE FROM readings WHERE file_key = ?", (key,))
                n_rows = 0
                for t_no, table in enumerate(found):
                    kinds[table["kind"]] = kinds.get(table["kind"], 0) + len(table["rows"])
                    conn.execute("INSERT INTO tables VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (key, t_no, table["kind"], table["sheet"], table["header"],
                                  json.dumps(table["roles"]), json.dumps(table["facts"])))
                    conn.executemany(
                        "INSERT INTO readings VALUES (?, ?, ?, ?, ?, ?)",
                        [(key, t_no, seq, table["kind"],
                          next(iter(row.values()))[1], json.dumps(row))
                         for seq, row in enumerate(table["rows"])])
                    n_rows += len(table["rows"])
                conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (key, READER, st, json.dumps(sorted({t["kind"] for t in found})),
                              len(found), n_rows, round(secs, 4), stamp))
    batch: list = []
    for got in _results(todo, width):
        batch.append(got)
        if len(batch) >= 2000:
            store_batch(batch)
            batch = []
    store_batch(batch)
    counts["read_seconds"] = round(time.monotonic() - started, 1)
    # sources: (re)resolved every pass (a record the store gains later is picked up)
    sconn = _store_conn(store)
    with_tables = {k for (k,) in conn.execute("SELECT file_key FROM files WHERE status = "
                                               "'read'")}
    useful = [s for s in listed if s.file_key in with_tables]
    records, unsure = (dict(records), set()) if records is not None else _resolve(
        sconn, [s.handle for s in useful])
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(s.handle, s.file_key, s.origin, s.name[:200], *(records.get(s.handle) or
              (None, None, None, None)), s.received_at, json.dumps(list(s.participants)),
              stamp) for s in useful if s.handle not in unsure])
    counts["sources_skipped_not_whole"] = len(unsure)
    if sconn is not None:
        sconn.close()
    counts.update(status=status, readings=kinds, sources_with_tables=len(useful),
                  sources_in_store=sum(1 for s in useful if s.handle in records),
                  seconds=round(time.monotonic() - started, 1))
    conn.close()
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--follow", type=float, default=0.0,
                        help="seconds between passes (0: one pass)")
    args = parser.parse_args(argv)
    while True:
        print(json.dumps(run(args.state, args.out, store=args.store, workers=args.workers,
                             limit=args.limit), sort_keys=True), flush=True)
        if not args.follow:
            return 0
        time.sleep(args.follow)


if __name__ == "__main__":
    sys.exit(main())
