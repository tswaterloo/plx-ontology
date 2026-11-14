"""The work log: one append-only timeline of what each person does and receives in the
Workbench, linked to the records and people involved, searchable, resumable and the
assistant's memory of that person's work.

**One log, no copies.** Every existing path keeps its own store (the actions journal, the
autopilot queue and its decisions journal, the write intents journal, the watchlists, the
feedback store, the AI watcher's item states); after it succeeds the API appends one
event here naming that store and the item's id there (``source``, ``source_id``). An
event says who (the reader key and name), what (``kind`` and ``verb`` from ``VERBS``; an
unknown pair is refused), when, the records (``event_record`` indexes them for the record
page) and the people (addresses) it involved, and its ``thread``: what "continue"
reopens (``ask:<conversation>``, ``autopilot:<item>``, ``card:<card>``,
``write:<intent>``, ``record:<type>/<id>``). Ask answers were never kept anywhere
durable, so an Ask event keeps the question and the answer as its reader saw it
(``detail``): continuing a session rebuilds it from the log.

**Organise.** A person names, pins, attaches to a case or closes a thread of their own
(``thread``: the only mutable rows; events are append-only, triggers refuse an update or
delete).

**Follow-through.** A message sent through the approvals desk or by an accepted autopilot
item opens a loop (``awaiting``: the reader's mailbox, the recipients, the thread, due
``REPLY_HOURS`` after sending, the autopilot's own reply window). Its state is read, never
stored: ``answered`` when the autopilot's inbound tracking (``operating.autopilot.Queue``
``inbound``: mailbox, thread and sender digests, no text) holds a message in the same
thread or from a recipient to that mailbox after the send; ``closed`` when the person
closed it; ``overdue`` after the window, with a nudge and a follow-up the person can ask
to draft; else ``waiting``.

**Who sees what.** A person reads only their own log; the owner (the owner-open reader,
``Backend._is_owner``) may read everyone's. The people exclusion is fail closed both ways:
with the guard unavailable nothing is written and nothing is shown; an event whose actor
or any person it names is excluded (or undecided inside the company's domains) is never
written, and a row is re-checked against the guard on every read. Text is cleaned as the
feedback store cleans it (no secret-looking string, no excluded person's id or address).

**Memory.** ``recall`` reads one person's log for a window, a record or words; Ask answers
a question about the person's own work ("what did I send yesterday", "where was I on PO
90412") from it (``asks_about_my_work``), without a model and without any record the log
does not already name.

Stored at ``<state>/work-log/work-log.sqlite`` (``PLX_WORK_LOG_DIR`` overrides; memory
without a state directory), 0600 in a 0700 folder.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

from operating import declared_schema as _schemas
from operating.declared_schema import work_log as _WORK_LOG
from operating.app_tables import rows as _rows

from . import feedback as fb
from .ask_drafts import linked_records
from operating.file_modes import keep_private

log = logging.getLogger("operator_api.work_log")

VERBS: dict[str, frozenset[str]] = {
    "ask": frozenset({"asked", "failed"}),
    "alert": frozenset({"seen", "dismissed", "snoozed", "reopened", "rated"}),
    "autopilot": frozenset({"prepared", "edited", "accepted", "rejected", "snoozed",
                            "sent"}),
    "action": frozenset({"sent", "replied", "forwarded", "messaged", "follow_up", "done",
                         "snoozed", "dismissed", "assigned", "prepared"}),
    "write": frozenset({"prepared", "approved", "executed"}),
    "watch": frozenset({"followed", "unfollowed", "rule_added", "rule_removed"}),
    "note": frozenset({"noted", "rated"}),
    "save": frozenset({"saved", "unsaved"}),
}
KINDS = tuple(VERBS)
REPLY_HOURS = 72          # operating.autopilot.REPLY_HOURS: the window a reply counts in
MAX_DETAIL = 200_000      # an Ask answer as JSON
EVENT_BATCH = 500         # matching rows read (and exclusion-checked) at a time (API-008)
# What an answer too large to keep whole keeps (API-010): enough to continue an
# instruction and to approve its drafts; ``_kept`` says it is partial.
_KEPT_CORE = ("question", "conversation_id", "answer_id", "direct_answer", "instruction",
              "drafts")
PARTIAL_TURN = ("This answer was too large to keep in full: its drafts and the question "
                "are kept. Ask again to see the whole answer.")


def _kept_detail(detail: Any) -> str:
    """``detail`` as JSON within ``MAX_DETAIL``: whole, else its core (API-010) with every
    handle the whole one cites (so a re-check of its sources stays complete; drafts go
    when those cannot be decided), else only that it was not kept."""
    whole = json.dumps(detail, ensure_ascii=False, default=str)
    if len(whole) <= MAX_DETAIL:
        return whole
    if not isinstance(detail, dict):
        return json.dumps({"_kept": "none"})
    from .ask_drafts import cited_handles
    core = {k: detail[k] for k in _KEPT_CORE if k in detail}
    try:
        core["_cited"] = {"citations": cited_handles(detail)}
    except ValueError:
        core.pop("drafts", None)
        core["_cited"] = {"system": "undecided", "quote": "a source without a handle"}
    core["_kept"] = "partial"
    for extra in ("facts",):   # what a draft's typed values are checked against
        if extra in detail:
            wider = {**core, extra: detail[extra]}
            if len(json.dumps(wider, ensure_ascii=False, default=str)) <= MAX_DETAIL:
                core = wider
    text = json.dumps(core, ensure_ascii=False, default=str)
    if len(text) <= MAX_DETAIL:
        return text
    return json.dumps({"_kept": "none", "question": str(detail.get("question") or "")[:2000],
                       "conversation_id": detail.get("conversation_id"),
                       "answer_id": detail.get("answer_id")}, default=str)
_THREAD = re.compile(r"^(ask|autopilot|card|write|record|alert|case):[A-Za-z0-9_.:/@#-]{1,400}$")
_CASE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _instant(text: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def valid_thread(thread: Any) -> bool:
    return isinstance(thread, str) and bool(_THREAD.match(thread))


# ---- the people exclusion -------------------------------------------------------------------

def exclusion() -> Any:
    return fb.exclusion()


def _actor_excluded(guard: Any, reader: Any) -> bool:
    """Whether the reader is (or cannot be told from) an excluded person: fail closed
    (``feedback.person_excluded``: their object id decides)."""
    session = reader.session
    ids = [*getattr(session, "identities", ()), getattr(session, "subject_oid", None),
           reader.email, *fb._UUID.findall(reader.key)]
    return fb.person_excluded(guard, [v for v in ids if v])


def _person_excluded(guard: Any, value: str) -> bool:
    value = value.strip().lower()
    if "@" in value:
        return fb._excluded_address(guard, value)
    if fb._UUID.fullmatch(value):
        return fb._excluded_id(guard, value)
    return False


def _row_excluded(guard: Any, row: Mapping[str, Any]) -> bool:
    if guard is None or getattr(guard, "unavailable", "absent"):
        return True
    if any(fb._excluded_id(guard, o.lower()) for o in fb._UUID.findall(row["reader"])):
        return True
    return any(_person_excluded(guard, p) for p in json.loads(row["people"] or "[]"))


# ---- the store ------------------------------------------------------------------------------

class WorkLog:
    """One SQLite file (or memory) behind one lock."""

    def __init__(self, directory: Optional[Path], *,
                 guard: Callable[[], Any] = exclusion,
                 clock: Callable[[], datetime] = _now) -> None:
        self.directory = Path(directory) if directory is not None else None
        self.guard, self.clock = guard, clock
        self._lock = threading.RLock()
        if self.directory is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False,
                                         isolation_level=None)
        else:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            keep_private(self.directory, 0o700)
            path = self.directory / "work-log.sqlite"
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            keep_private(path, 0o600)
            self._conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False,
                                         isolation_level=None)
            self._conn.execute("PRAGMA journal_mode = WAL")
            for side in ("-wal", "-shm"):
                extra = path.with_name(path.name + side)
                if extra.exists():
                    keep_private(extra, 0o600)
        self._conn.row_factory = sqlite3.Row
        _schemas.apply(self._conn, _WORK_LOG)
        # where follow-through reads replies (``operating.autopilot.Queue`` inbound)
        self.inbound_path: Callable[[], Optional[Path]] = lambda: None

    @classmethod
    def for_backend(cls, backend: Any) -> "WorkLog":
        override = os.environ.get("PLX_WORK_LOG_DIR")
        state = getattr(backend, "state_dir", None)
        data = getattr(backend, "data_dir", None)
        directory = (Path(override) if override else Path(state) / "work-log" if state
                     else Path(data) / "work-log" if data else None)
        store = cls(directory)
        queue = getattr(backend, "_ap_queue_path", None)
        if callable(queue):
            store.inbound_path = queue
        return store

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes --

    def record(self, reader: Any, *, kind: str, verb: str, title: str, thread: str,
               source: str, source_id: Optional[str] = None,
               records: Iterable[Mapping[str, Any]] = (), people: Iterable[str] = (),
               detail: Any = None, awaiting: Optional[Mapping[str, Any]] = None,
               once: Optional[str] = None) -> Optional[int]:
        """Append one event; its seq, or None when nothing may be written (the exclusion
        unavailable, the actor or a person excluded) or ``once`` was written before.
        An unknown kind, verb or thread is refused (ValueError)."""
        if verb not in VERBS.get(kind, ()):
            raise ValueError("work_log_verb_unknown")
        if not valid_thread(thread):
            raise ValueError("work_log_thread_invalid")
        guard = self.guard()
        if _actor_excluded(guard, reader):
            return None
        people = sorted({str(p).strip().lower() for p in people if p and str(p).strip()})
        if any(_person_excluded(guard, p) for p in people):
            return None
        refs, ids = [], []
        for r in records:
            oid = str(r.get("object_id") or "").strip()
            if not oid or oid in ids or len(oid) > 700:
                continue
            ids.append(oid)
            refs.append({"object_id": oid, "type": str(r.get("type") or r.get("object_type")
                                                       or "")[:60] or None,
                         "label": fb.clean(r.get("label"), guard, 300)})
        if isinstance(detail, (dict, list)):
            detail = _kept_detail(detail)
        else:
            detail = fb.clean(detail, guard, 4000)
        row = (_iso(self.clock()), reader.key, fb.clean(reader.display_name, guard, 120), kind,
               verb, fb.clean(title, guard, 300) or verb, detail, thread, source,
               (str(source_id)[:400] if source_id else None),
               json.dumps(refs, ensure_ascii=False), json.dumps(people),
               json.dumps(dict(awaiting), sort_keys=True) if awaiting else None, once)
        with self._lock:
            # a savepoint: its own transaction, or one step of a ``batch``
            self._conn.execute("SAVEPOINT event")
            try:
                cur = self._conn.execute(
                    "INSERT INTO event (at, reader, reader_name, kind, verb, title, detail, "
                    "thread, source, source_id, records, people, awaiting, once) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
                seq = cur.lastrowid
                self._conn.executemany("INSERT OR IGNORE INTO event_record VALUES (?, ?)",
                                       [(seq, oid) for oid in ids])
            except BaseException as error:
                self._conn.execute("ROLLBACK TO event")
                self._conn.execute("RELEASE event")
                if isinstance(error, sqlite3.IntegrityError):
                    return None   # ``once`` already written
                raise
            self._conn.execute("RELEASE event")
        return seq

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Several events in one transaction (one disk sync)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def organise(self, reader_key: str, thread: str, *, name: Any = None,
                 pinned: Optional[bool] = None, case_id: Any = None,
                 closed: Optional[bool] = None) -> dict[str, Any]:
        """Name, pin, attach to a case or close one of the reader's own threads (KeyError:
        no event of theirs is in it)."""
        if not valid_thread(thread):
            raise ValueError("work_log_thread_invalid")
        if case_id not in (None, "") and not (isinstance(case_id, str)
                                             and _CASE.match(case_id)):
            raise ValueError("work_log_case_invalid")
        with self._lock:
            if self._conn.execute("SELECT 1 FROM event WHERE reader = ? AND thread = ? "
                                  "LIMIT 1", (reader_key, thread)).fetchone() is None:
                raise KeyError("work_log_thread_not_found")
            meta = self._meta(reader_key, thread)
            if name is not None:
                meta["name"] = fb.clean(name, self.guard(), 120)
            if pinned is not None:
                meta["pinned"] = bool(pinned)
            if case_id is not None:
                meta["case_id"] = case_id or None
            if closed is not None:
                meta["closed_at"] = _iso(self.clock()) if closed else None
            self._conn.execute(
                "INSERT INTO thread (reader, thread, name, pinned, case_id, closed_at, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(reader, thread) DO UPDATE SET "
                "name = excluded.name, pinned = excluded.pinned, case_id = excluded.case_id, "
                "closed_at = excluded.closed_at, at = excluded.at",
                (reader_key, thread, meta["name"], int(meta["pinned"]), meta["case_id"],
                 meta["closed_at"], _iso(self.clock())))
        return meta

    # -- reads --

    def _meta(self, reader_key: str, thread: str) -> dict[str, Any]:
        row = _rows.WORK_LOG_THREAD.first(self._conn, {"reader": reader_key, "thread": thread})
        return {"name": row["name"] if row else None, "pinned": bool(row and row["pinned"]),
                "case_id": row["case_id"] if row else None,
                "closed_at": row["closed_at"] if row else None}

    def events(self, *, reader: Optional[str], q: Optional[str] = None,
               kind: Optional[str] = None, record: Optional[str] = None,
               thread: Optional[str] = None, since: Optional[str] = None,
               until: Optional[str] = None, case_id: Optional[str] = None,
               pinned: bool = False, open_loops: bool = False, limit: int = 50,
               offset: int = 0, sends: bool = False
               ) -> tuple[list[dict[str, Any]], Optional[int]]:
        """Events newest first (``reader``: only theirs; None: everyone's, the owner's
        view) and how many match; ([], None) while the exclusion cannot decide."""
        guard = self.guard()
        if guard is None or getattr(guard, "unavailable", "absent"):
            return [], None
        if kind is not None and kind not in VERBS:
            raise ValueError("work_log_kind_unknown")
        where, args = ["1"], []
        if reader is not None:
            where.append("e.reader = ?")
            args.append(reader)
        if kind:
            where.append("e.kind = ?")
            args.append(kind)
        if sends:   # API-006: a message that went out, whichever path sent it
            where.append(SEND_SQL)
        if record:
            where.append("e.seq IN (SELECT seq FROM event_record WHERE object_id = ?)")
            args.append(record)
        if thread:
            where.append("e.thread = ?")
            args.append(thread)
        if since:
            where.append("e.at >= ?")
            args.append(since)
        if until:
            where.append("e.at < ?")
            args.append(until)
        if case_id:
            where.append("t.case_id = ?")
            args.append(case_id)
        if pinned:
            where.append("t.pinned = 1")
        if open_loops:
            where.append("e.awaiting IS NOT NULL AND t.closed_at IS NULL")
        for word in (q or "").split()[:8]:
            like = "%" + word.lower().replace("\\", "\\\\").replace("%", "\\%") \
                .replace("_", "\\_") + "%"
            where.append("(lower(e.title) LIKE ? ESCAPE '\\' OR lower(e.records) LIKE ? "
                         "ESCAPE '\\' OR lower(coalesce(t.name, '')) LIKE ? ESCAPE '\\' "
                         "OR (e.kind = 'ask' AND lower(coalesce(e.detail, '')) LIKE ? "
                         "ESCAPE '\\'))")
            args += [like] * 4
        sql = (" FROM event e LEFT JOIN thread t ON t.reader = e.reader AND "
               "t.thread = e.thread WHERE " + " AND ".join(where))
        with self._lock:
            # the guard re-checks every match now (an exclusion added later hides a row):
            # the light columns of all of them, then the page's rows in full
            # in bounded batches (API-008): only the page's seqs and a count are held
            cursor = self._conn.execute("SELECT e.seq, e.reader, e.people" + sql
                                        + " ORDER BY e.seq DESC", args)
            page, matched = [], 0
            while True:
                batch = cursor.fetchmany(EVENT_BATCH)
                if not batch:
                    break
                for r in batch:
                    if _row_excluded(guard, r):
                        continue
                    if offset <= matched < offset + limit:
                        page.append(r["seq"])
                    matched += 1
            cursor.close()
            rows = self._conn.execute(
                "SELECT e.*, t.name AS t_name, t.pinned AS t_pinned, t.case_id AS t_case, "
                "t.closed_at AS t_closed FROM event e LEFT JOIN thread t ON t.reader = "
                "e.reader AND t.thread = e.thread WHERE e.seq IN (SELECT value FROM "
                "json_each(?)) ORDER BY e.seq DESC", (json.dumps(page),)).fetchall()
        return [self._event(r) for r in rows], matched

    def ask_detail(self, reader_key: str, answer_id: str) -> Optional[dict[str, Any]]:
        """The reader's own Ask answer (its ``detail``) by answer id; None when it is not
        theirs, was not kept, or the people exclusion cannot decide (fail closed)."""
        guard = self.guard()
        if guard is None or getattr(guard, "unavailable", "absent"):
            return None
        with self._lock:
            row = _rows.WORK_LOG_EVENT.first(
                self._conn, {"reader": reader_key, "kind": "ask", "source": "ask",
                             "source_id": str(answer_id)}, order_by=("seq",), descending=True)
        if row is None or _row_excluded(guard, row):
            return None
        try:
            detail = json.loads(row["detail"] or "null")
        except ValueError:
            return None
        return detail if isinstance(detail, dict) else None

    def written(self, keys: Sequence[str]) -> set[str]:
        """Which of these ``once`` keys are already in the log."""
        with self._lock:
            return {r[0] for r in self._conn.execute(
                "SELECT once FROM event WHERE once IN (SELECT value FROM json_each(?))",
                (json.dumps(list(keys)),))}

    def _event(self, row: sqlite3.Row) -> dict[str, Any]:
        out = {k: row[k] for k in ("seq", "at", "reader", "reader_name", "kind", "verb",
                                   "title", "thread", "source", "source_id")}
        out["records"] = json.loads(row["records"] or "[]")
        out["people"] = json.loads(row["people"] or "[]")
        out["thread_name"], out["pinned"] = row["t_name"], bool(row["t_pinned"])
        out["case_id"] = row["t_case"]
        out["detail"] = row["detail"]
        if out["kind"] == "action" and out["verb"] != "prepared" and \
                _outcome(row) == "dry_run":
            # Logged before dry runs had their own verb: it was never sent, never say so.
            out["verb"] = "prepared"
        out["loop"] = self.loop(row, closed_at=row["t_closed"]) if row["awaiting"] else None
        return out

    # -- follow-through --

    def loop(self, row: Mapping[str, Any], *, closed_at: Optional[str]) -> dict[str, Any]:
        waiting = json.loads(row["awaiting"])
        sent = _instant(row["at"])
        due = _instant(waiting.get("due"))
        out = {"state": "waiting", "due": waiting.get("due"), "answered_at": None,
               "nudge": None}
        if closed_at:
            return {**out, "state": "closed"}
        answered, how = self._reply_at(waiting, row["at"])
        if answered and how in ("thread", "recipient"):
            return {**out, "state": "answered", "answered_at": answered}
        if answered:   # API-007: mail from a recipient, not in the thread: maybe a reply
            out["possible_reply_at"] = answered
        if due is not None and self.clock() >= due:
            waited = max(1, int((self.clock() - sent).total_seconds() // 86400)) if sent else 1
            who = ", ".join(waiting.get("to") or ()) or "them"
            out.update(state="overdue", nudge=(
                f"No reply from {who} in {waited} day{'s' if waited != 1 else ''}."))
            subject = waiting.get("subject")
            out["follow_up"] = ("Draft a short, polite follow-up to " + who
                                + (f" about \"{subject}\"" if subject else "")
                                + f", sent {row['at'][:10]}, asking for an answer.")
        return out

    def _reply_at(self, waiting: Mapping[str, Any], sent_at: str
                  ) -> tuple[Optional[str], Optional[str]]:
        """(the first reply after ``sent_at`` the autopilot's inbound tracking holds, how
        it was matched): ``thread`` when it is in the sent message's conversation (the
        inbound tracking keys mail by its conversation id); when the conversation is
        known, mail from a recipient outside it is only ``counterpart`` (a possible reply,
        API-007). With no conversation known (a new message, or a reply whose message
        could not be resolved to its conversation; a stored message handle is no
        conversation key) mail from a recipient answers it (``recipient``): nothing
        better tells a reply apart, and a real reply must never read as unanswered.
        (None, None) when it holds none or cannot be read."""
        path = self.inbound_path()
        mailbox = str(waiting.get("mailbox") or "").lower()
        if not path or not mailbox or not Path(path).exists():
            return None, None
        from operating.autopilot import _digest
        threads = [_digest("thread", t) for t in waiting.get("threads") or ()
                   if t and not _is_handle(t)]
        parties = [_digest("party", a.lower()) for a in waiting.get("to") or () if a]
        if not threads and not parties:
            return None, None
        try:
            conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                for how, column, values in (
                        ("thread", "thread", threads),
                        ("counterpart" if threads else "recipient", "counterpart", parties)):
                    if not values:
                        continue
                    found = conn.execute(
                        f"SELECT MIN(at) FROM inbound WHERE lower(owner) = ? AND at > ? AND "
                        f"{column} IN (SELECT value FROM json_each(?))",
                        (mailbox, sent_at, json.dumps(values))).fetchone()
                    if found and found[0]:
                        return found[0], how
            finally:
                conn.close()
        except sqlite3.Error:
            return None, None
        return None, None


def _is_handle(value: Any) -> bool:
    """A source handle (``graph-mail://...``), never a conversation id."""
    return "://" in str(value)


def conversation_in(read: Any) -> Optional[str]:
    """The conversation id a mail source read carries (the read itself, or its row,
    record, fields or values), None when it names none."""
    if not isinstance(read, Mapping):
        return None
    for place in (read, *(read.get(k) for k in ("row", "record", "fields", "values"))):
        if isinstance(place, Mapping):
            for key in ("conversation_id", "row.conversation_id", "conversationId"):
                value = place.get(key)
                if isinstance(value, str) and value.strip() and not _is_handle(value):
                    return value.strip()
    return None


def awaiting_reply(mailbox: Optional[str], to: Sequence[str], *, subject: Any = None,
                   threads: Sequence[Any] = (), at: Optional[datetime] = None,
                   answers: Sequence[Any] = (), answered_at: Any = None
                   ) -> Optional[dict[str, Any]]:
    """The loop a sent message opens (None without a mailbox or a recipient). ``answers``:
    the message it answered (and ``answered_at``, when that one arrived), so a later
    message of the thread is not taken as answered by it."""
    if not mailbox or not to:
        return None
    sent = at or _now()
    out = {"mailbox": mailbox.lower(), "to": sorted({a.lower() for a in to}),
           "threads": [str(t) for t in threads if t],
           "subject": str(subject)[:200] if subject else None,
           "due": _iso(sent + timedelta(hours=REPLY_HOURS))}
    named = [str(a) for a in answers if a]
    if named:
        out["answers"] = named
        out["answered_at"] = str(answered_at) if answered_at else None
    return out


# ---- continue -------------------------------------------------------------------------------

def resume(thread: str, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Where "continue" takes the person, and for an Ask session its turns (oldest first),
    rebuilt from the log."""
    scheme, _, ref = thread.partition(":")
    href = {"ask": f"/ask?c={ref}", "autopilot": f"/autopilot?item={ref}",
            "write": "/my-work", "card": "/", "alert": "/"}.get(scheme)
    if scheme == "record" and ref.startswith("page/"):
        href = "/" + ref[len("page/"):]
    elif scheme == "record":
        href = "/object/" + ref
    elif href is None:
        href = "/work-log"
    turns = []
    if scheme == "ask":
        for event in sorted(events, key=lambda e: e["seq"]):
            if event.get("verb") != "asked":
                continue
            try:
                answer = json.loads(event.get("detail") or "null")
            except ValueError:
                answer = None
            if isinstance(answer, dict) and answer.get("_kept") in ("partial", "none"):
                # API-010: kept only in part (or not at all): said, never shown as whole
                turns.append({"question": answer.get("question") or event["title"],
                              "answer": None, "withheld": PARTIAL_TURN, "at": event["at"]})
            elif isinstance(answer, dict):
                answer.pop("_review", None)    # the usage review's, not the person's view
                turns.append({"question": answer.get("question") or event["title"],
                              "answer": answer, "at": event["at"]})
    return {"href": href, "turns": turns}


# ---- memory: Ask about my own work ---------------------------------------------------------

_ACTS = (r"(?:do|ask|send|approve|dismiss|save|reply|repl(?:y|ied) to|work on|look at|follow|"
         r"forward|snooze|accept|reject|edit|note)")
_MINE = re.compile(
    r"\b(?:what|which|who|when)\s+(?:did|have)\s+i\s+" + _ACTS + r"\b"
    r"|\bdid i\s+" + _ACTS + r"\b"
    r"|\bwhat i\s+(?:did|sent|asked|approved|dismissed|saved)\b"
    r"|\bwhere (?:was|am) i\b"
    r"|\bmy (?:work ?log|activity in the workbench)\b"
    r"|\b(?:still )?waiting (?:on|for) (?:a )?repl(?:y|ies)\b"
    r"|\bwho (?:hasn't|has not|didn't|did not) (?:replied|reply|answered|got back)\b",
    re.IGNORECASE)
_RECORDISH = re.compile(r"\b[A-Za-z]{0,6}[-#]?\d{3,}[A-Za-z0-9-]*\b")


def asks_about_my_work(question: str) -> bool:
    """Whether a question is about the asker's own work in the Workbench (answered from
    their log); a question about the business records is not."""
    return bool(_MINE.search(question or ""))


_MAILED = re.compile(r"\b(?:send|sent|e-?mail(?:ed)?|mail(?:ed)?|repl(?:y|ied)|forward(?:ed)?)\b",
                     re.IGNORECASE)


def asks_about_my_mail(question: str) -> bool:
    """Whether a question about the asker's own work asks what they mailed: with nothing in
    their log, their sent mail answers it (the Ask path), not an empty log."""
    return asks_about_my_work(question) and bool(_MAILED.search(question or ""))


# what a person also does outside the Workbench: approvals given in Mission Control or
# Business Central (a save, a note, a dismissed card or an accepted item is the log's alone)
_APPROVED = re.compile(r"\b(?:approv(?:e|ed|al|als)|sign(?:ed)? off)\b", re.IGNORECASE)


def asks_about_work_done_elsewhere(question: str) -> bool:
    """Whether a question about the asker's own work may be about something they did
    outside the Workbench (mail they sent, an approval they gave): with nothing in their
    log the Ask path answers it from the business systems, never an empty log."""
    return asks_about_my_mail(question) or (
        asks_about_my_work(question) and bool(_APPROVED.search(question or "")))


def _window(question: str, now: datetime) -> tuple[Optional[datetime], Optional[datetime], str]:
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo("America/Toronto"))
    except Exception:  # noqa: BLE001
        local = now
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    text = question.lower()
    if "yesterday" in text:
        return day - timedelta(days=1), day, "yesterday"
    if "today" in text or "this morning" in text:
        return day, None, "today"
    if "last week" in text:
        start = day - timedelta(days=day.weekday() + 7)
        return start, start + timedelta(days=7), "last week"
    if "this week" in text:
        return day - timedelta(days=day.weekday()), None, "this week"
    if _RECORDISH.search(question):
        return None, None, "so far"
    return day - timedelta(days=7), None, "in the last 7 days"


def recall(store: WorkLog, reader_key: str, question: str, *, limit: int = 12
           ) -> dict[str, Any]:
    """The reader's own events a question about their work names: a window ("yesterday",
    "this week"; default the last 7 days), a record or number it names, a kind it names."""
    now = store.clock()
    start, end, said = _window(question, now)
    text = question.lower()
    sends = bool(re.search(r"\b(send|sent|repl(y|ied)|email)", text))
    kind = ("ask" if re.search(r"\bask(ed)?\b", text) else
            None if sends else
            "autopilot" if "autopilot" in text else
            "alert" if re.search(r"\b(alert|dismiss)", text) else None)
    words = [w for w in _RECORDISH.findall(question)][:2]
    loops = bool(re.search(r"\bwaiting\b|\breply\b.*\b(yet|back)\b", text))
    events, total = store.events(reader=reader_key, q=" ".join(words) or None,
                                 kind=None if words or loops else kind,
                                 sends=sends and not words and not loops
                                 and kind is None,
                                 since=_iso(start) if start else None,
                                 until=_iso(end) if end else None,
                                 open_loops=loops, limit=limit)
    return {"said": said, "events": events, "total": total, "words": words}


_SAY = {"asked": "Asked", "seen": "Read", "dismissed": "Dismissed", "snoozed": "Snoozed",
        "reopened": "Reopened", "rated": "Rated", "prepared": "Prepared for you",
        "failed": "Asked, no answer", "edited": "Edited", "accepted": "Approved",
        "rejected": "Rejected", "sent": "Sent",
        "replied": "Replied", "forwarded": "Forwarded", "messaged": "Messaged",
        "follow_up": "Follow-up set", "done": "Marked done", "assigned": "Assigned",
        "approved": "Approved", "executed": "Ran", "followed": "Started watching",
        "unfollowed": "Stopped watching", "rule_added": "Added an alert rule",
        "rule_removed": "Removed an alert rule", "noted": "Noted", "saved": "Saved",
        "unsaved": "Unsaved"}


# A dry run (``status: dry_run``) of an action: nothing went out or changed.
_SAY_KIND = {("action", "prepared"): "Prepared (not sent)"}


def say(event: Mapping[str, Any]) -> str:
    verb = _SAY_KIND.get((event.get("kind"), event["verb"])) or \
        _SAY.get(event["verb"], event["verb"])
    line = f"{verb}: {event['title']}"
    loop = event.get("loop") or {}
    if loop.get("state") == "overdue":
        line += f" ({loop['nudge']})"
    elif loop.get("state") == "answered":
        line += " (they replied)"
    elif loop.get("state") == "waiting":
        line += " (waiting for a reply)"
    if loop.get("possible_reply_at") and loop.get("state") in ("waiting", "overdue"):
        line += (f" (mail from them arrived {str(loop['possible_reply_at'])[:16]} outside "
                 "the thread: check whether it answers)")
    return line


def recall_answer(found: Mapping[str, Any]) -> str:
    events, total = found["events"], found["total"]
    if total is None:
        return ("Your work log can't be read right now: the list of people kept out of the "
                "Workbench isn't loaded, so nothing is shown.")
    about = f" on {' '.join(found['words'])}" if found["words"] else ""
    if not events:
        return f"Your work log has nothing{about} {found['said']}."
    lines = [f"- {e['at'][:16].replace('T', ' ')}: {say(e)}" for e in events]
    more = f"\n\n{total - len(events)} more are in your work log." if total > len(events) \
        else ""
    return (f"Your work log{about} {found['said']} ({total} "
            f"{'entry' if total == 1 else 'entries'}):\n" + "\n".join(lines) + more)


# ---- the paths that write it ----------------------------------------------------------------

_ACTION_VERB = {"send_email": "sent", "reply_in_thread": "replied", "forward": "forwarded",
                "teams_message": "messaged", "create_follow_up": "follow_up",
                "mark_done": "done", "snooze": "snoozed", "dismiss": "dismissed",
                "assign": "assigned"}
# A message that went out (API-006), across the paths that send: an autopilot item sent,
# or a card action sent, replied, forwarded or messaged whose recorded status is ``sent``
# (an older event without a status: when it opened a reply loop, as ``_went_out``).
SENT_SQL = ("((e.kind = 'autopilot' AND e.verb = 'sent') OR (e.kind = 'action' AND e.verb "
            "IN ('sent', 'replied', 'forwarded', 'messaged') AND coalesce(CASE WHEN "
            "json_valid(e.detail) THEN json_extract(e.detail, '$.status') END, CASE WHEN "
            "e.awaiting IS NOT NULL THEN 'sent' END) = 'sent'))")
# What "what did I send" lists: what went out, and a dry run of a send, which reads
# "Prepared (not sent)" (API-005); never a snooze, dismissal, assignment or follow-up.
SEND_SQL = ("(" + SENT_SQL + " OR (e.kind = 'action' AND (e.verb = 'prepared' OR (e.verb "
            "IN ('sent', 'replied', 'forwarded', 'messaged') AND CASE WHEN "
            "json_valid(e.detail) THEN json_extract(e.detail, '$.status') END = "
            "'dry_run'))))")
_DONE = frozenset({"sent", "dry_run", "follow_up", "done", "snoozed", "dismissed",
                   "assigned"})
_DECISION_VERB = {"accept": "accepted", "edit": "edited", "reject": "rejected",
                  "snooze": "snoozed"}
_ALERT_VERB = {"dismissed": "dismissed", "snoozed": "snoozed", "open": "reopened"}


def _dump(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _ref(found: Mapping[str, Any]) -> list[dict[str, Any]]:
    oid = found.get("object_id")
    return [{"object_id": oid, "type": found.get("object_type") or found.get("type"),
             "label": found.get("label") or found.get("subject_label")}] if oid else []


class Notes:
    """What each existing path appends after it succeeded. A failure to write the log is
    logged and never fails the person's own action (it already happened)."""

    def __init__(self, store: WorkLog, mailbox: Callable[[Any], Optional[str]],
                 conversation_of: Optional[Callable[[Any, str], Optional[str]]] = None
                 ) -> None:
        self.store, self.mailbox = store, mailbox
        # (reader, mail handle) -> the conversation id of that message, as the reader
        # may open it (None: unknown); a card reply's loop is matched on it
        self.conversation_of = conversation_of
        # the person's memory (``operator_api.memory``), told what each path did; None:
        # nothing remembered beyond this log
        self.memory: Any = None

    def _remember(self, what: str, reader: Any, *args: Any) -> None:
        if self.memory is None:
            return
        try:
            self.memory.observe(what, reader, *args)
        except Exception as error:  # noqa: BLE001 - memory never fails the action
            log.error("memory not told (%s): %s", what, type(error).__name__)

    def _conversation(self, reader: Any, replied: Any) -> Optional[str]:
        """The conversation a reply or forward continues: a cited message handle
        resolved to its conversation id (None when it cannot be); a bare id as given."""
        if not replied:
            return None
        if not _is_handle(replied):
            return str(replied)
        if self.conversation_of is None:
            return None
        try:
            got = self.conversation_of(reader, str(replied))
        except Exception:  # noqa: BLE001 - unknown: recipient mail answers it
            return None
        return got if isinstance(got, str) and got and not _is_handle(got) else None

    def _put(self, reader: Any, **event: Any) -> Optional[int]:
        try:
            return self.store.record(reader, **event)
        except Exception as error:  # noqa: BLE001 - the log never fails the action
            log.error("work log event not written (%s %s): %s", event.get("kind"),
                      event.get("verb"), type(error).__name__, exc_info=error)
            return None

    def ask(self, reader: Any, answer: Any, review: Optional[Mapping[str, Any]] = None
            ) -> None:
        """An answer as its reader saw it."""
        data = _dump(answer)
        if review:
            data = {**data, "_review": dict(review)}
        self._put(reader, kind="ask", verb="asked", title=data.get("question") or "",
                  thread=f"ask:{data['conversation_id']}", source="ask",
                  source_id=data.get("answer_id"),
                  records=linked_records(data), detail=data, once=f"ask:{data.get('answer_id')}")
        self._remember("answer", reader, {k: v for k, v in data.items() if k != "_review"})

    def ask_failed(self, reader: Any, request: Any, code: str, *,
                   steps: Sequence[Mapping[str, Any]] = (), stream_ms: int = 0) -> None:
        """A question that ended in an error (no answer): kept so the usage review sees it
        and "continue" can ask it again."""
        conversation = getattr(request, "conversation_id", None) or "failed"
        thread = f"ask:{conversation}"
        self._put(reader, kind="ask", verb="failed", title=request.question or "",
                  thread=thread if valid_thread(thread) else "ask:failed", source="ask",
                  detail={"question": request.question, "outcome": "error",
                          "_review": {"error": str(code)[:80], "steps": list(steps),
                                      "stream_ms": stream_ms}})

    def alert_state(self, reader: Any, done: Any, request: Any) -> None:
        data = _dump(done)
        title = "Briefing item " + {"dismissed": "dismissed", "snoozed": "snoozed",
                                    "open": "put back"}[data["state"]]
        if getattr(request, "reason", None):
            title += f": {request.reason}"
        self._put(reader, kind="alert", verb=_ALERT_VERB[data["state"]], title=title,
                  thread=f"alert:{data['item_id']}", source="ai_watcher",
                  source_id=data["item_id"])

    def alerts_read(self, reader: Any, alert_ids: Sequence[str], marked: int) -> None:
        if not marked:
            return
        ids = [a for a in list(alert_ids)[:50] if valid_thread(f"alert:{a}")]
        done = self.store.written([f"seen:{reader.key}:{a}" for a in ids])
        for alert in ids:
            if f"seen:{reader.key}:{alert}" not in done:
                self._put(reader, kind="alert", verb="seen", title="Alert read",
                          thread=f"alert:{alert}", source="watchlists", source_id=alert,
                          once=f"seen:{reader.key}:{alert}")

    def item_rated(self, reader: Any, body: Any) -> None:
        self._put(reader, kind="alert", verb="rated",
                  title=f"Marked {body.verdict.replace('_', ' ')}: "
                        f"{body.label or body.item_id}",
                  thread=f"alert:{body.item_id}" if valid_thread(f"alert:{body.item_id}")
                  else "alert:item", source="feedback", source_id=body.item_id,
                  records=_ref({"object_id": body.object_id, "object_type": body.object_type,
                                "label": body.label}))

    def answer_rated(self, reader: Any, body: Any, entry: Mapping[str, Any]) -> None:
        conversation = (entry.get("context") or {}).get("conversation_id")
        thread = f"ask:{conversation}" if conversation else "ask:rated"
        self._put(reader, kind="note", verb="rated",
                  title=("Rated an answer " + ("up" if (body.rating or 0) > 0 else "down")
                         + (f": {entry.get('question')}" if entry.get("question") else "")),
                  thread=thread if valid_thread(thread) else "ask:rated",
                  source="feedback", source_id=entry.get("id"))
        self._remember("feedback", reader, body.rating, getattr(body, "note", None),
                       entry.get("question"), entry.get("id"), conversation)

    def note(self, reader: Any, body: Any, entry: Mapping[str, Any]) -> None:
        records = _ref({"object_id": body.object_id, "object_type": body.object_type,
                        "label": body.label})
        page = str(getattr(body, "page", None) or "").strip().lstrip("/")
        # the screen it was given on, when it names no record
        thread = (f"record:{body.object_type or 'record'}/{body.object_id}" if records
                  else f"record:page/{page}" if page and valid_thread(f"record:page/{page}")
                  else "record:note/general")
        self._put(reader, kind="note", verb="noted", title=body.note,
                  thread=thread if valid_thread(thread) else "record:note/general",
                  source="feedback", source_id=entry.get("id"), records=records)
        self._remember("note", reader, body.note, [r["object_id"] for r in records],
                       entry.get("id"))

    def autopilot_prepared(self, reader: Any, area: Any) -> None:
        """Each item prepared for the reader, once, when they first see it."""
        items = [i for i in _dump(area).get("items") or () if i.get("mine")]
        done = self.store.written([f"prepared:{reader.key}:{i['item_id']}" for i in items])
        items = [i for i in items if f"prepared:{reader.key}:{i['item_id']}" not in done]
        if not items:
            return
        try:
            with self.store.batch():
                for item in items:
                    self._put(reader, kind="autopilot", verb="prepared", title=item["title"],
                              thread=f"autopilot:{item['item_id']}", source="autopilot_queue",
                              source_id=item["item_id"],
                              records=_ref(item.get("happened") or {}),
                              once=f"prepared:{reader.key}:{item['item_id']}")
        except Exception as error:  # noqa: BLE001 - the log never fails the view
            log.error("work log: prepared items not written: %s", type(error).__name__)

    def autopilot_decided(self, reader: Any, item: Any) -> None:
        data = _dump(item)
        decision = data.get("decision") or {}
        verb = _DECISION_VERB.get(decision.get("decision"))
        if verb is None:
            return
        common = dict(thread=f"autopilot:{data['item_id']}", source="autopilot_decisions",
                      source_id=data["item_id"], records=_ref(data.get("happened") or {}))
        title = data["title"] + (f" ({decision['outcome'].replace('_', ' ')})"
                                 if decision.get("outcome") else "")
        self._put(reader, kind="autopilot", verb=verb, title=title, **common)
        self._remember("autopilot", reader, data)
        draft = (data.get("prepared") or {}).get("draft") or {}
        to = [t.get("address") if isinstance(t, Mapping) else t for t in draft.get("to") or ()]
        # a send only when the email's own action is done, never the first action's outcome
        if verb == "accepted" and decision.get("sent") is True and to:
            happened = data.get("happened") or {}
            self._put(reader, kind="autopilot", verb="sent", title=data["title"],
                      people=[a for a in to if a], **common,
                      awaiting=awaiting_reply(self.mailbox(reader), [a for a in to if a],
                                              subject=draft.get("subject"),
                                              threads=[happened.get("conversation_id")],
                                              at=self.store.clock(),
                                              answers=[happened.get("object_id")],
                                              answered_at=happened.get("at")))

    def action(self, reader: Any, result: Any, row: Optional[Mapping[str, Any]],
               card: Optional[Mapping[str, Any]]) -> None:
        data = _dump(result)
        if data.get("replayed") or data.get("status") not in _DONE:
            return
        row = row or {}
        subject = (card or {}).get("subject") or {}
        to = [*(row.get("to") or ()), *(row.get("cc") or ())]
        if row.get("assignee"):
            to.append(row["assignee"])
        outbound = data["kind"] in ("send_email", "reply_in_thread", "forward")
        title = row.get("subject") or row.get("card_label") or data.get("text") or \
            data["kind"].replace("_", " ")
        verb = "prepared" if data["status"] == "dry_run" else _ACTION_VERB[data["kind"]]
        self._put(reader, kind="action", verb=verb, title=title,
                  thread=f"card:{data['card_id']}" if valid_thread(f"card:{data['card_id']}")
                  else "card:unknown", source="actions_journal",
                  source_id=f"{data['card_id']}#{data.get('seq')}",
                  records=_ref({"object_id": subject.get("object_id") or subject.get("id"),
                                "object_type": subject.get("type")
                                or subject.get("object_type"),
                                "label": subject.get("label")}),
                  people=to, detail={"status": data["status"]},
                  awaiting=awaiting_reply(self.mailbox(reader), row.get("to") or (),
                                          subject=row.get("subject"),
                                          threads=[self._conversation(
                                              reader, row.get("in_reply_to"))],
                                          at=self.store.clock())
                  if outbound and data["status"] == "sent" else None)
        self._remember("action", reader, data["kind"], data["status"], title,
                       list(row.get("to") or ()),
                       [r["object_id"] for r in _ref({"object_id": subject.get("object_id")
                                                      or subject.get("id")})],
                       f"{data['card_id']}#{data.get('seq')}")

    def write(self, reader: Any, verb: str, view: Any) -> None:
        data = _dump(view)
        if data.get("replayed"):
            return
        intent = data.get("intent") or {}
        iid = intent.get("intent_id") or "unknown"
        self._put(reader, kind="write", verb=verb,
                  title=data.get("text") or f"{intent.get('operation')} in "
                                            f"{str(intent.get('system') or '').upper()}",
                  thread=f"write:{iid}", source="write_intents", source_id=iid,
                  detail={"status": data.get("status")},
                  records=[{"object_id": intent["record_id"],
                            "type": intent.get("entity_set"), "label": None}]
                  if intent.get("record_id") else ())

    def watch(self, reader: Any, verb: str, found: Any) -> None:
        data = _dump(found)
        if "rule_id" in data:
            title = data.get("description") or data.get("text") or "Alert rule"
            self._put(reader, kind="watch", verb=verb, title=title,
                      thread=f"record:rule/{data['rule_id']}", source="watchlists",
                      source_id=data["rule_id"])
            return
        thread = f"record:{data.get('object_type') or 'record'}/{data['object_id']}"
        self._put(reader, kind="watch", verb=verb,
                  title=data.get("label") or data["object_id"],
                  thread=thread if valid_thread(thread) else "record:watch/unknown",
                  source="watchlists", source_id=data["object_id"], records=_ref(data))


# ---- time saved (O-03) ------------------------------------------------------------------------

# What one event of the log saved its person, in the minutes the same work takes by hand
# (``operating.autopilot_replay.MINUTES``, the weights the day-coverage measure uses): the
# basis it counts under and that basis's words. Everything else (reading, dismissing,
# snoozing, watching, notes, a dry-run send) saves nothing and is not counted.
SAVED_BASIS: dict[tuple[str, str], str] = {
    ("autopilot", "sent"): "mail_sent", ("action", "sent"): "mail_sent",
    ("action", "replied"): "mail_sent", ("action", "forwarded"): "mail_sent",
    ("action", "messaged"): "teams_sent", ("autopilot", "accepted"): "record",
    ("write", "executed"): "record", ("ask", "asked"): "record"}
SAVED_WORDS = {"mail_sent": "messages sent from a prepared draft",
               "teams_sent": "Teams messages sent from a prepared draft",
               "record": "lookups and entries done for you (an accepted autopilot item "
                         "without a message, a write made live, an answered Ask)"}
SAVED_METHOD = ("An estimate, not a measurement: each thing the Workbench did for a person "
                "counts the minutes the same work takes by hand in the autopilot replay's "
                "weights ({weights}). Only work that was used counts: a message actually "
                "sent (a dry run counts nothing), an autopilot item accepted, a write "
                "made live in the business system (a would-write counts nothing), an Ask "
                "answered. Reading, dismissing and watching count "
                "nothing. Weeks start Monday, US Eastern.")
SAVED_TZ = "America/New_York"


def saved_minutes() -> dict[str, int]:
    from operating.autopilot_replay import MINUTES
    return {b: int(MINUTES[b]) for b in sorted(set(SAVED_BASIS.values()))}


def _week_start(at: datetime) -> str:
    from zoneinfo import ZoneInfo
    local = at.astimezone(ZoneInfo(SAVED_TZ))
    return (local.date() - timedelta(days=local.weekday())).isoformat()


def _outcome(row: Mapping[str, Any]) -> Optional[str]:
    """The status an action or write event recorded (``Notes.action``/``Notes.write``)."""
    try:
        detail = json.loads(row["detail"] or "null")
    except (TypeError, ValueError):
        return None
    status = detail.get("status") if isinstance(detail, dict) else None
    return status if isinstance(status, str) else None


def _went_out(row: Mapping[str, Any]) -> bool:
    """An action counts only when its message was sent, a write only when it was written
    live: a dry run (``dry_run``), a would-write record or an event that does not say how
    it ended counts nothing. A mail event logged before the outcome was recorded counts
    when it opened a reply loop, which only a sent message does."""
    if row["kind"] == "action":
        status = _outcome(row)
        if status is None:
            return row["verb"] in ("sent", "replied", "forwarded") and bool(row["awaiting"])
        return status == "sent"
    if row["kind"] == "write":
        return _outcome(row) == "written"
    return True


def time_saved(store: "WorkLog", *, reader: Optional[str], weeks: int = 4,
               now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Per person and week (``weeks`` of them, the current one last), the minutes the
    Workbench saved them (``SAVED_BASIS``) and how many of each basis; ``reader``: only
    theirs, None: everyone's (the owner's view). None while the people exclusion cannot
    decide (fail closed). An autopilot item accepted and then sent counts once, as the
    message."""
    from zoneinfo import ZoneInfo
    guard = store.guard()
    if guard is None or getattr(guard, "unavailable", "absent"):
        return None
    weeks = max(1, min(int(weeks), 12))
    now = now or store.clock()
    local = now.astimezone(ZoneInfo(SAVED_TZ))
    first = local.date() - timedelta(days=local.weekday() + 7 * (weeks - 1))
    starts = [(first + timedelta(days=7 * i)).isoformat() for i in range(weeks)]
    since = datetime(first.year, first.month, first.day,
                     tzinfo=ZoneInfo(SAVED_TZ)).astimezone(timezone.utc)
    pairs = " OR ".join("(kind = ? AND verb = ?)" for _ in SAVED_BASIS)
    args: list[Any] = [_iso(since)] + [v for pair in SAVED_BASIS for v in pair]
    where = f"at >= ? AND ({pairs})"
    if reader is not None:
        where += " AND reader = ?"
        args.append(reader)
    with store._lock:
        rows = store._conn.execute(
            "SELECT seq, at, reader, reader_name, kind, verb, source_id, people, awaiting, "
            "detail "
            f"FROM event WHERE {where} ORDER BY seq", args).fetchall()
    sent_items = {(r["reader"], r["source_id"]) for r in rows
                  if r["kind"] == "autopilot" and r["verb"] == "sent"}
    weights = saved_minutes()
    people: dict[str, dict[str, Any]] = {}
    for r in rows:
        if _row_excluded(guard, r):
            continue
        if not _went_out(r):
            continue   # a dry run, a would-write or an outcome not on the event
        if r["kind"] == "autopilot" and r["verb"] == "accepted" \
                and (r["reader"], r["source_id"]) in sent_items:
            continue   # counted once, as its message
        at = _instant(r["at"])
        if at is None:
            continue
        week = _week_start(at)
        if week not in starts:
            continue
        basis = SAVED_BASIS[(r["kind"], r["verb"])]
        person = people.setdefault(r["reader"], {
            "reader": r["reader"], "name": r["reader_name"],
            "weeks": {s: {"minutes": 0, "items": {}} for s in starts}})
        slot = person["weeks"][week]
        slot["minutes"] += weights[basis]
        slot["items"][basis] = slot["items"].get(basis, 0) + 1
    out = []
    for person in people.values():
        out.append({"reader": person["reader"], "name": person["name"],
                    "weeks": [{"start": s, **person["weeks"][s]} for s in starts]})
    out.sort(key=lambda p: (-p["weeks"][-1]["minutes"], -sum(w["minutes"]
                                                              for w in p["weeks"]),
                            str(p["name"] or "")))
    words = ", ".join(f"{SAVED_WORDS[b].split(' (')[0]} {m} min"
                      for b, m in weights.items())
    return {"weeks": starts, "people": out, "minutes": weights,
            "basis": dict(SAVED_WORDS), "method": SAVED_METHOD.format(weights=words)}


SCORECARD_VERBS = ("prepared", "accepted", "edited", "rejected")


def autopilot_days(store: "WorkLog", *, reader: str, days: int = 14,
                   now: Optional[datetime] = None) -> Optional[list[dict[str, Any]]]:
    """The reader's daily autopilot scorecard from their own log, newest day first (US
    Eastern days, ``days`` of them, today included): items prepared for them (logged when
    first shown), accepted, edited and rejected. An item is counted once per verb (its
    first event of that verb). None while the people exclusion cannot decide (fail
    closed)."""
    from zoneinfo import ZoneInfo
    guard = store.guard()
    if guard is None or getattr(guard, "unavailable", "absent"):
        return None
    days = max(1, min(int(days), 60))
    tz = ZoneInfo(SAVED_TZ)
    today = (now or store.clock()).astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=tz).astimezone(timezone.utc)
    marks = ",".join("?" for _ in SCORECARD_VERBS)
    with store._lock:
        rows = store._conn.execute(
            "SELECT seq, at, reader, reader_name, kind, verb, source_id, people, awaiting, "
            f"detail FROM event WHERE at >= ? AND reader = ? AND kind = 'autopilot' AND "
            f"verb IN ({marks}) ORDER BY seq",
            [_iso(since), reader, *SCORECARD_VERBS]).fetchall()
    slots = {(first + timedelta(days=i)).isoformat(): dict.fromkeys(SCORECARD_VERBS, 0)
             for i in range(days)}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        if _row_excluded(guard, r):
            continue
        at = _instant(r["at"])
        key = (r["verb"], str(r["source_id"]))
        if at is None or key in seen:
            continue
        seen.add(key)
        day = at.astimezone(tz).date().isoformat()
        if day in slots:
            slots[day][r["verb"]] += 1
    return [{"day": d, **slots[d]} for d in sorted(slots, reverse=True)]
