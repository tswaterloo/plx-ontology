"""Feedback on anything the Workbench shows, kept in the Workbench's own state, and the loop
it feeds.

**What is captured.** An answer (thumbs up or down, what was wrong, the claims that were
wrong, "the right answer is"), a briefing or alert item (useful, not useful, wrong) and a
free note on a page, a record, a list or the app itself. Each entry carries what it is
about, attached by the server where it can be (the question, the answer, its claims and
citations from the answer kept for that reader, the session reference, the served data
version and the API version) and the app version the browser reports. Never a secret: a
token-like string in any text is removed before it is stored.

**Where.** ``<state>/feedback/feedback.sqlite`` (``PLX_FEEDBACK_DIR`` overrides; the mock
keeps it in memory). Beside it: ``signals.json`` (the per-item signal the AI watcher and the
proactive engine read, ``operating.feedback_signals``), ``digest-YYYY-MM-DD.json`` (the
nightly digest for improvement agents). Files are 0600 in a 0700 folder.

**Who sees what.** A reader lists only their own entries; the owner (the owner-open full-read
grant) and the admins (``PLX_FEEDBACK_ADMINS``: object ids, emails or principals) list every
entry and alone change an entry's status. The people exclusion is applied to everything
stored: a citation of an excluded person's mailbox or chat is dropped and an excluded
person's address or object id in any text is withheld (an unavailable exclusion withholds
every address and object id: fail closed).

**Who reads the answer.** An answer entry keeps the sensitive markings (finance, PII,
purchasing prices) its reader held when it was served (``context.markings``; unknown:
every one). The owner and the entry's own reader see it whole. Another admin sees it as
their own markings allow, decided by the read policy (``Backend.feedback_grant``): the
answer, its claims and the right answer are withheld when the entry carries a marking the
admin lacks, a claim whose citation the admin may not open is withheld, and a note or
question reading as personal financial content is withheld without finance and PII. An
excluded person's own feedback names them to nobody: admins see "a colleague" and no id.

**The loop.** Triage statuses ``new``, ``accepted``, ``fixed``, ``wont_fix``. The product
stores the question, the reader's right answer and the claims and citations as the reader
saw them, nothing more: it never imports eval code and never builds a key. Eval candidates
are made offline by ``scripts.eval_feedback_candidates``, which drops any item matching a
held-out item before it writes. Thumbs-down answers and wrong alerts go into the nightly
digest (without the right answer, which reaches agents only through that tool). An alert marked not useful or wrong is suppressed for the reader who
said so; marked wrong by the owner or an admin, or accepted in triage, it is suppressed for
every reader until the entry is closed as won't fix.

    python3 -m operator_api.feedback digest --state-dir DIR [--day YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from operating import declared_schema as _schemas
from operating.declared_schema import feedback as _FEEDBACK
from operating.file_modes import keep_private

KINDS = ("answer", "alert", "page", "record", "list", "ui", "general")
STATUSES = ("new", "accepted", "fixed", "wont_fix")
VERDICTS = ("useful", "not_useful", "wrong")
SIGNALS_FORMAT = "plx-feedback-signals/v1"
DIGEST_FORMAT = "plx-feedback-digest/v1"
# The markings whose values an admin without them never sees in another reader's entry.
SENSITIVE = ("finance", "pii", "purchasing_prices")
COLLEAGUE = "a colleague"
MAX_TEXT = 4000



def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def eastern_day(moment: datetime) -> date:
    """The business day (US Eastern) of ``moment``; UTC where no zone database is."""
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 - a host without tzdata (Windows) counts in UTC
        return moment.astimezone(timezone.utc).date()


# ---- what is never stored -----------------------------------------------------------------

_SECRET = re.compile(
    r"(?:\b(?:sk|pk|rk|ghp|gho|xox[abp]|eyJ)[A-Za-z0-9_\-.]{16,}"
    r"|\b(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*\S+"
    r"|\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/]{48,}={0,2})", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}\b")
_ADDRESS = re.compile(r"\b[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
WITHHELD = "[withheld]"


# The mail capture's state directory (``attach_capture_directory``, set as the API
# starts) and the guard last made with its directory: (the list's guard, made at, guard).
_CAPTURE: dict[str, Any] = {}
_CAPTURE_LOCK = threading.Lock()
DIRECTORY_TTL = 300.0   # seconds a directory read is kept (the capture records it daily)


def attach_capture_directory(state_dir: Any) -> None:
    """Decide addresses by what the mail capture under ``state_dir`` recorded (its
    mailboxes' directory and last tenant listing: ``people_exclusion.with_directory``),
    so a colleague's address is decided and an outsider's (outside the tenant's domains)
    kept; without it every address is withheld. None: no directory."""
    with _CAPTURE_LOCK:
        _CAPTURE.clear()
        if state_dir:
            _CAPTURE["dir"] = state_dir


def exclusion() -> Any:
    """The people exclusion as the read paths load it, with the mail capture's directory
    attached when the API named one (never raises; may be unavailable)."""
    try:
        import time
        from integrations.people_exclusion import with_directory
        from operating.pg.permissions import load_exclusion
        guard = load_exclusion()
        with _CAPTURE_LOCK:
            state_dir, made = _CAPTURE.get("dir"), _CAPTURE.get("made")
            if state_dir is None:
                return guard
            now = time.monotonic()
            if made is not None and made[0] is guard and now - made[1] < DIRECTORY_TTL:
                return made[2]
            attached = with_directory(guard, state_dir)
            _CAPTURE["made"] = (guard, now, attached)
            return attached
    except Exception:  # noqa: BLE001 - no guard: everything it would decide is withheld
        return None


def _excluded_id(guard: Any, oid: str) -> bool:
    try:
        return guard is None or bool(guard.unavailable) or bool(guard.is_excluded(oid))
    except Exception:  # noqa: BLE001 - undecidable: withheld
        return True


_ANGLED = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>")


def _address_forms(address: str) -> list[str]:
    """The forms one address is decided by: the address inside ``Name <...>`` and, for a
    plus address (``bstn+orders@``, delivered to ``bstn@``), the mailbox it reaches."""
    plain = str(address).strip().lower()
    angled = _ANGLED.search(plain)
    plain = angled.group(1) if angled else plain
    local, at, domain = plain.rpartition("@")
    forms = [plain]
    if at and "+" in local.lstrip("+"):
        forms.append(local.split("+", 1)[0] + "@" + domain)
    return forms


def _excluded_address(guard: Any, address: str) -> bool:
    """Whether ``address`` is (or cannot be told from) an excluded person's: any of its
    forms (``_address_forms``) excluded counts."""
    return any(_one_address_excluded(guard, form) for form in _address_forms(address))


def _one_address_excluded(guard: Any, address: str) -> bool:
    try:
        if guard is None or guard.unavailable:
            return True
        if guard.decides(address):
            return bool(guard.is_excluded(address))
        # undecided: outside the tenant's known mail domains it is no tenant person's; a
        # guard that knows none (the object ids alone, as the API loads it with no
        # directory attached) cannot tell a colleague's address from an outsider's, so
        # it is withheld (an excluded person's address was shown before: CT-T-06)
        domains = set(guard.known_domains())
        return not domains or address.rsplit("@", 1)[1].lower() in domains
    except Exception:  # noqa: BLE001 - undecidable: withheld
        return True


def person_excluded(guard: Any, values: Any) -> bool:
    """Whether the signed-in person these identities name (their object id, addresses,
    principal keys) is excluded, or cannot be told apart from one (fail closed). An
    object id decides; an address is consulted where the guard decides it, and one it
    cannot decide counts as excluded only when no object id identifies the person (a
    guard of object ids alone decides no address: a signed-in reader is then decided by
    their object id, never shut out for their address)."""
    if guard is None or getattr(guard, "unavailable", "absent"):
        return True
    oids: set[str] = set()
    addresses: set[str] = set()
    for value in values:
        tail = str(value).strip().lower().rsplit(":", 1)[-1]
        if _UUID.fullmatch(tail):
            oids.add(tail)
        elif _ADDRESS.fullmatch(tail):
            addresses.add(tail)
    if any(_excluded_id(guard, oid) for oid in oids):
        return True
    for address in addresses:
        try:
            if guard.decides(address):
                if guard.is_excluded(address):
                    return True
                continue
        except Exception:  # noqa: BLE001 - undecidable
            pass
        if not oids and _excluded_address(guard, address):
            return True
    return False


def clean(text: Any, guard: Any, limit: int = MAX_TEXT) -> Optional[str]:
    """``text`` as stored: no secret-looking string, no excluded person's object id or
    address (fail closed), at most ``limit`` characters; None when empty."""
    if not isinstance(text, str):
        return None
    out = _SECRET.sub("[removed]", text)
    out = _UUID.sub(lambda m: WITHHELD if _excluded_id(guard, m.group(0).lower())
                    else m.group(0), out)
    out = _ADDRESS.sub(lambda m: WITHHELD if _excluded_address(guard, m.group(0).lower())
                       else m.group(0), out)
    out = out.strip()[:limit]
    return out or None


def clean_handle(handle: Any, guard: Any) -> Optional[str]:
    """A citation handle as stored, or None when it names an excluded (or undecidable)
    person's object id or address (a mailbox, a chat member)."""
    if not isinstance(handle, str) or not handle.strip():
        return None
    from urllib.parse import unquote
    plain = unquote(handle)
    if any(_excluded_id(guard, oid.lower()) for oid in _UUID.findall(plain)) or any(
            _excluded_address(guard, a.lower()) for a in _ADDRESS.findall(plain)):
        return None
    return handle.strip()[:600]


# ---- who may see all ------------------------------------------------------------------------

def owner_open(session: Any) -> bool:
    """The one owner test (S-16; ``Backend._is_owner`` and the app's ``is_owner`` use it): the
    session holds the owner-open grant, full read over every source and every mailbox, usable
    now. ``SessionIdentity.access`` is ``EmployeeEntitlement.full_read_code()``, set by the
    access layer at sign-in and on every request; None when the grant is missing, refused or
    the people exclusion cannot be decided, so it is exactly ``unrestricted()``."""
    try:
        from operating.employee_permissions import FULL_READ_SCOPES
        code = "full_read:" + "+".join(sorted(FULL_READ_SCOPES)) + ":mail_all"
    except Exception:  # noqa: BLE001
        return False
    return getattr(session, "access", None) == code


def is_admin(reader: Any, environ: Mapping[str, str] | None = None) -> bool:
    """The owner or a configured feedback admin (``PLX_FEEDBACK_ADMINS``)."""
    session = reader.session
    if owner_open(session):
        return True
    env = os.environ if environ is None else environ
    named = {v.strip().lower() for v in (env.get("PLX_FEEDBACK_ADMINS") or "").split(",")
             if v.strip()}
    if not named:
        return False
    held = {str(v).lower() for v in (*getattr(session, "identities", ()),
                                      *getattr(session, "principals", ()))}
    if reader.email:
        held.add(reader.email.lower())
    return bool(named & held)


def reader_ref(key: str) -> str:
    """A reader as the digest names them: a short one-way reference, never the person."""
    return "r-" + hashlib.sha256(key.encode()).hexdigest()[:10]


# ---- the store ----------------------------------------------------------------------------

class FeedbackStore:
    """One SQLite file (or memory, for the mock) behind one lock."""

    def __init__(self, directory: Optional[Path]) -> None:
        self.directory = Path(directory) if directory is not None else None
        self._lock = threading.Lock()
        if self.directory is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False,
                                         isolation_level=None)
        else:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            keep_private(self.directory, 0o700)   # an existing folder too
            path = self.directory / "feedback.sqlite"
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            keep_private(path, 0o600)
            self._conn = sqlite3.connect(str(path), timeout=30,
                                         check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode = WAL")
            for side in ("-wal", "-shm"):   # SQLite gives them the file's mode; made sure
                extra = path.with_name(path.name + side)
                if extra.exists():
                    keep_private(extra, 0o600)
        self._conn.row_factory = sqlite3.Row
        _schemas.apply(self._conn, _FEEDBACK)

    @classmethod
    def for_backend(cls, backend: Any) -> "FeedbackStore":
        """``PLX_FEEDBACK_DIR``, else ``<state>/feedback``, else the app's data directory's
        ``feedback``, else memory."""
        override = os.environ.get("PLX_FEEDBACK_DIR")
        state = getattr(backend, "state_dir", None)
        data = getattr(backend, "data_dir", None)
        directory = (Path(override) if override else Path(state) / "feedback" if state
                     else Path(data) / "feedback" if data else None)
        return cls(directory)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes --

    def add(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        fid = "fb-" + secrets.token_hex(6)
        row = {"id": fid, "at": entry.get("at") or _iso(_now()), "status": "new",
               "context": json.dumps(entry.get("context") or {}, ensure_ascii=False,
                                     sort_keys=True)}
        for column in ("kind", "reader", "reader_name", "by_admin", "rating", "verdict",
                       "note", "expected", "page", "object_type", "object_id", "label",
                       "item_id", "answer_id", "question", "answer"):
            row[column] = entry.get(column)
        row["by_admin"] = 1 if row["by_admin"] else 0
        if row["kind"] not in KINDS:
            raise ValueError("feedback_kind_unknown")
        names = ", ".join(row)
        with self._lock:
            self._conn.execute(f"INSERT INTO feedback ({names}) VALUES "
                               f"({', '.join('?' for _ in row)})", list(row.values()))
        if row["kind"] == "alert":
            self.write_signals()
        return self.get(fid)

    def remove(self, fid: str) -> None:
        """Undo an entry whose paired write failed (``record_answer``)."""
        with self._lock:
            self._conn.execute("DELETE FROM feedback WHERE id = ?", (fid,))

    def set_status(self, fid: str, status: str, by: str) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError("feedback_status_unknown")
        with self._lock:
            done = self._conn.execute(
                "UPDATE feedback SET status = ?, status_by = ?, status_at = ? WHERE id = ?",
                (status, by, _iso(_now()), fid)).rowcount
        if not done:
            raise KeyError("feedback_not_found")
        entry = self.get(fid)
        if entry["kind"] == "alert":
            self.write_signals()
        return entry

    # -- reads --

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["context"] = json.loads(out.get("context") or "{}")
        out["by_admin"] = bool(out["by_admin"])
        return out

    def get(self, fid: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM feedback WHERE id = ?", (fid,)).fetchone()
        if row is None:
            raise KeyError("feedback_not_found")
        return self._entry(row)

    def list(self, *, reader: Optional[str], kind: Optional[str] = None,
             page: Optional[str] = None, status: Optional[str] = None,
             oldest_first: bool = False, limit: int = 50, offset: int = 0
             ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """Entries newest first (``reader``: only theirs; None: every entry), the total that
        matches and, over the same filters but the status, the count per status."""
        where, args = [], []
        if reader is not None:
            where.append("reader = ?")
            args.append(reader)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if page:
            where.append("(page = ? OR page LIKE ? ESCAPE '\\')")
            args += [page, page.replace("\\", "\\\\").replace("%", "\\%")
                     .replace("_", "\\_").rstrip("/") + "/%"]
        base = " AND ".join(where) or "1"
        with self._lock:
            counts = {s: 0 for s in STATUSES}
            for row in self._conn.execute(
                    f"SELECT status, COUNT(*) n FROM feedback WHERE {base} GROUP BY status",
                    args):
                counts[row["status"]] = row["n"]
            if status:
                base += " AND status = ?"
                args.append(status)
            total = self._conn.execute(f"SELECT COUNT(*) FROM feedback WHERE {base}",
                                       args).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM feedback WHERE {base} ORDER BY at {'ASC' if oldest_first else 'DESC'}, "
                f"id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return [self._entry(r) for r in rows], total, counts

    def between(self, start: str, end: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM feedback WHERE at >= ? AND at < ? "
                                      "ORDER BY at", (start, end)).fetchall()
        return [self._entry(r) for r in rows]

    # -- the per-item signal --

    def signals(self) -> dict[str, Any]:
        """Per alert item: suppressed for every reader (``global``) when marked wrong by the
        owner or an admin or accepted/fixed in triage; else for each reader who marked it
        not useful or wrong. An entry closed as won't fix suppresses nothing."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, reader, verdict, note, by_admin, status, at FROM feedback "
                "WHERE kind = 'alert' AND item_id IS NOT NULL AND verdict IN "
                "('not_useful', 'wrong') AND status != 'wont_fix' ORDER BY at").fetchall()
        items: dict[str, Any] = {}
        for row in rows:
            words = "Marked wrong" if row["verdict"] == "wrong" else "Marked not useful"
            signal = {"verdict": row["verdict"], "at": row["at"],
                      "reason": f"{words}: {row['note']}" if row["note"] else words}
            entry = items.setdefault(row["item_id"], {"global": None, "readers": {}})
            if row["verdict"] == "wrong" and (row["by_admin"] or row["status"] in
                                              ("accepted", "fixed")):
                entry["global"] = signal
            elif row["status"] in ("accepted", "fixed"):
                entry["global"] = signal
            else:
                entry["readers"][row["reader"]] = signal
        return {"format": SIGNALS_FORMAT, "updated_at": _iso(_now()), "items": items}

    def write_signals(self) -> Optional[Path]:
        if self.directory is None:
            return None
        return _write_json(self.directory / "signals.json", self.signals())

    # -- the nightly digest --

    def digest(self, day: date) -> dict[str, Any]:
        """One day's (US Eastern) thumbs-down answers, wrong or not useful alerts and other
        notes: what improvement agents read. Readers are one-way references; the right answer
        is left out (``scripts.eval_feedback_candidates`` carries it, held-out items
        dropped)."""
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=14)
        end = start + timedelta(days=1, hours=28)   # a wide UTC window, filtered by day below
        rows = [r for r in self.between(_iso(start), _iso(end))
                if eastern_day(datetime.fromisoformat(r["at"].replace("Z", "+00:00"))) == day]

        def raw(r: Mapping[str, Any]) -> dict[str, Any]:
            ctx = r["context"]
            return {k: v for k, v in {
                "id": r["id"], "at": r["at"], "kind": r["kind"], "reader": reader_ref(r["reader"]),
                "status": r["status"], "rating": r["rating"], "verdict": r["verdict"],
                "note": r["note"], "page": r["page"],
                "object_type": r["object_type"], "object_id": r["object_id"],
                "label": r["label"], "item_id": r["item_id"], "question": r["question"],
                "answer": r["answer"], "claims": ctx.get("claims"),
                "flagged_claims": ctx.get("flagged"), "served_version": ctx.get("served_version"),
                "data_as_of": ctx.get("data_as_of"), "app_version": ctx.get("app_version"),
                "api_version": ctx.get("api_version"), "session_ref": ctx.get("session_ref"),
            }.items() if v not in (None, [], {})}

        guard = exclusion()

        def shown(r: Mapping[str, Any]) -> dict[str, Any]:
            """The entry through today's exclusion (it may have changed since it was
            written)."""
            out = raw(r)
            reclean(out, guard)
            return {k: v for k, v in out.items() if v not in (None, [], {})}

        down = [shown(r) for r in rows if r["kind"] == "answer" and r["rating"] == -1]
        alerts = [shown(r) for r in rows if r["kind"] == "alert"
                  and r["verdict"] in ("wrong", "not_useful")]
        notes = [shown(r) for r in rows if r["kind"] not in ("answer", "alert")]
        return {"format": DIGEST_FORMAT, "day": day.isoformat(), "generated_at": _iso(_now()),
                "counts": {"entries": len(rows), "thumbs_down": len(down),
                           "thumbs_up": sum(1 for r in rows if r["kind"] == "answer"
                                            and r["rating"] == 1),
                           "alerts_wrong": sum(1 for a in alerts if a["verdict"] == "wrong"),
                           "alerts_not_useful": sum(1 for a in alerts
                                                    if a["verdict"] == "not_useful"),
                           "notes": len(notes)},
                "thumbs_down": down, "alerts": alerts, "notes": notes}

    def write_digest(self, day: date) -> Optional[Path]:
        if self.directory is None:
            return None
        return _write_json(self.directory / f"digest-{day.isoformat()}.json", self.digest(day))


def _write_json(path: Path, body: Any) -> Path:
    """``body`` as ``path``, 0600, replaced in one step."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def start_nightly(store: FeedbackStore, stop: Optional[threading.Event] = None,
                  every: float = 900.0) -> Optional[threading.Thread]:
    """Write yesterday's digest (US Eastern) once it is missing, checked every ``every``
    seconds while the API runs. A store kept in memory has none."""
    if store.directory is None:
        return None
    stop = stop or threading.Event()

    def loop() -> None:
        while True:
            try:
                day = eastern_day(_now()) - timedelta(days=1)
                if not (store.directory / f"digest-{day.isoformat()}.json").exists():
                    store.write_digest(day)
            except Exception:  # noqa: BLE001 - the digest never stops the API
                pass
            if stop.wait(every):
                return

    thread = threading.Thread(target=loop, name="feedback-digest", daemon=True)
    thread.start()
    return thread


# ---- who wrote it, and what another admin may read of it -----------------------------------

def reader_excluded(reader: Any, guard: Any) -> bool:
    """Whether the reader is an excluded person (or undecidable: fail closed)."""
    session = reader.session
    ids = [*getattr(session, "identities", ()), *getattr(session, "principals", ()),
           getattr(session, "subject_oid", None), reader.email]
    return person_excluded(guard, [v for v in ids if v])


def _who(reader: Any, guard: Any) -> dict[str, Any]:
    """The reader columns of an entry: an excluded person's name is never stored."""
    hidden = reader_excluded(reader, guard)
    return {"reader": reader.key, "reader_name": None if hidden else reader.display_name,
            "by_admin": is_admin(reader), "_hidden": hidden}


def _grant(backend: Any, reader: Any, handles: Sequence[str]
           ) -> tuple[Optional[frozenset[str]], dict[str, Any]]:
    """(the markings the reader holds, each handle's withheld marker or None) from the read
    policy (``Backend.feedback_grant``); (None, every handle undecided) when unknown."""
    try:
        held, decided = backend.feedback_grant(reader, list(handles))
        from ontology.store_ddl import with_implied   # finance holds purchasing prices
        return with_implied(held), dict(decided)
    except Exception:  # noqa: BLE001 - undecidable: as if nothing is held (fail closed)
        return None, {h: {"reason": "undecided"} for h in handles}


def served_markings(backend: Any, reader: Any) -> Optional[list[str]]:
    """The sensitive markings ``reader`` holds as an answer is served, kept with the answer
    so a later rating labels it by what it was served under (None: unknown, read as all)."""
    held, _ = _grant(backend, reader, [])
    return None if held is None else sorted(set(held) & set(SENSITIVE))


def _personal(text: Any) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        from operating.chunking.sensitive import personal_financial
        return personal_financial(text) is not None
    except Exception:  # noqa: BLE001 - undecidable: treated as sensitive
        return True


def reclean(out: dict[str, Any], guard: Any) -> None:
    """An entry's text, people and claim citations (``out["claims"]``) through the
    exclusion as it is now (``clean``/``clean_handle``; fail closed with no list)."""
    for name in ("note", "question", "label", "expected", "answer", "page", "object_id"):
        value = out.get(name)
        if isinstance(value, str):
            out[name] = clean(value, guard, max(len(value), 1))
    claims = []
    for c in out.get("claims") or ():
        text = c.get("text")
        cites = []
        for cite in c.get("citations") or ():
            handle = clean_handle(cite.get("handle"), guard)
            if handle is not None:
                label = cite.get("label")
                cites.append({**cite, "handle": handle,
                              "label": clean(label, guard, max(len(label), 1))
                              if isinstance(label, str) else label})
        claims.append({**c, "text": clean(text, guard, max(len(text), 1))
                       if isinstance(text, str) else text, "citations": cites})
    if "claims" in out:
        out["claims"] = claims


class Viewer:
    """What one reader may read of the entries they list: the owner and an entry's own
    reader read it whole; another admin reads it as their own markings and grants allow."""

    def __init__(self, backend: Any, reader: Any, rows: Sequence[Mapping[str, Any]],
                 guard: Any = None) -> None:
        self.reader = reader
        # the people exclusion as it is now: text cleaned when an entry was written is
        # cleaned again on every read, so a person excluded since is withheld (SEC-013);
        # no list (unavailable): every address and object id is withheld (fail closed)
        self.guard = exclusion() if guard is None else guard
        self.whole = owner_open(reader.session)
        self.held: Optional[frozenset[str]] = None
        self.decided: dict[str, Any] = {}
        others = [r for r in rows if r.get("reader") != reader.key]
        if not self.whole and others:
            handles = sorted({c.get("handle") for r in others
                              for f in (r.get("context") or {}).get("claims") or ()
                              for c in f.get("citations") or () if c.get("handle")})
            self.held, self.decided = _grant(backend, reader, handles)

    def _now_clean(self, out: dict[str, Any]) -> None:
        reclean(out, self.guard)

    def _writer_excluded(self, key: Any) -> bool:
        """Whether the entry's writer (their principals key) is excluded now (or the list
        cannot decide: fail closed)."""
        guard = self.guard
        if guard is None or getattr(guard, "unavailable", None):
            return True
        for value in str(key or "").lower().split(","):
            tail = value.rsplit(":", 1)[-1]
            if (_UUID.fullmatch(tail) and _excluded_id(guard, tail)) or (
                    _ADDRESS.fullmatch(tail) and _excluded_address(guard, tail)):
                return True
        return False

    def entry(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """``row`` with ``claims``, ``flagged`` and ``withheld`` (markings not shown)."""
        ctx = row.get("context") or {}
        out = dict(row, claims=list(ctx.get("claims") or []), flagged=ctx.get("flagged") or [],
                   withheld=[])
        self._now_clean(out)
        if row.get("reader") != self.reader.key and (
                ctx.get("reader_hidden") or self._writer_excluded(row.get("reader"))):
            out["reader_name"] = COLLEAGUE
        if self.whole or row.get("reader") == self.reader.key:
            return out
        held = self.held or frozenset()
        carried = ctx.get("markings")
        lacking = set(SENSITIVE if carried is None else carried) - held
        if lacking:
            out.update(answer=None, expected=None,
                       claims=[{"i": c.get("i"), "text": None, "citations": []}
                               for c in out["claims"]])
        else:
            claims = []
            for c in out["claims"]:
                cites = [s for s in c.get("citations") or ()
                         if self.decided.get(s.get("handle"), {"reason": "undecided"}) is None]
                if len(cites) < len(c.get("citations") or ()):
                    lacking.add("source")
                    claims.append({"i": c.get("i"), "text": None, "citations": cites})
                else:
                    claims.append(c)
            out["claims"] = claims
            if "source" in lacking:
                # the answer and the expected text carry what the withheld claim said:
                # one disclosure decision for the whole entry (SEC-001)
                out.update(answer=None, expected=None)
        if {"finance", "pii"} - held:
            for field in ("note", "question", "label", "expected", "answer"):
                if _personal(out.get(field)):
                    out[field] = None
                    lacking |= {"finance", "pii"} - held
        out["withheld"] = sorted(lacking)
        return out


# ---- what the routes record ---------------------------------------------------------------

def _context_common(backend: Any, reader: Any, app_version: Optional[str],
                    hidden: bool = False) -> dict[str, Any]:
    from . import models as m
    served = None
    try:
        served = backend.served_version()
    except Exception:  # noqa: BLE001 - unknown: left out
        served = None
    return {k: v for k, v in {
        "session_ref": None if hidden else getattr(reader.session, "session_ref", None),
        "reader_hidden": hidden or None,
        "served_version": served, "data_label": getattr(backend, "data_label", None),
        "app_version": app_version, "api_version": m.API_VERSION}.items() if v}


def _add(store: FeedbackStore, who: Mapping[str, Any], fields: Mapping[str, Any]
         ) -> dict[str, Any]:
    return store.add({**{k: v for k, v in who.items() if not k.startswith("_")}, **fields})


def record_answer(store: FeedbackStore, backend: Any, reader: Any, request: Any,
                  then: Optional[Callable[[], Any]] = None) -> tuple[dict[str, Any], Any]:
    """An answer's rating with what the server kept of the answer for this reader, and
    ``then()`` (the backend's own rating record) as one write: when ``then`` raises, the
    entry is removed and the error raised. Returns (entry, what ``then`` returned)."""
    guard = exclusion()
    kept = backend.answer_context(reader, request.answer_id)
    claims = []
    for i, fact in enumerate(kept.get("facts") or []):
        cites = []
        for s in fact.get("sources") or []:
            handle = clean_handle(s.get("handle"), guard)
            if handle:
                cites.append({"system": s.get("system"), "handle": handle,
                              "label": clean(s.get("label"), guard, 200)})
        claims.append({"i": i, "text": clean(fact.get("text"), guard, 600), "citations": cites})
    flagged = sorted({i for i in (request.claims or []) if 0 <= i < len(claims)})
    who = _who(reader, guard)
    question = clean(kept.get("question"), guard) or ""
    expected = clean(request.expected, guard, 2000) if request.rating == -1 else None
    answer = clean(kept.get("answer"), guard)
    note = clean(request.note, guard, 2000)
    # the sensitive markings the answer was served under, kept with it when served, and
    # any the reader holds now (a narrowed grant never relabels an old answer as less
    # sensitive; unknown either way: None, read as all)
    served = kept.get("markings")
    held, _ = _grant(backend, reader, [])
    markings = None if held is None or not isinstance(served, list) \
        else (set(served) | set(held)) & set(SENSITIVE)
    if markings is not None and any(_personal(t) for t in (
            question, answer, expected, note, *(c["text"] for c in claims))):
        markings |= {"finance", "pii"}
    context = {**_context_common(backend, reader, request.app_version, who["_hidden"]),
               "claims": claims, "flagged": flagged,
               "markings": None if markings is None else sorted(markings),
               "conversation_id": kept.get("conversation_id"),
               "data_as_of": kept.get("as_of"),
               "subjects": [{"type": s.get("type"), "object_id": clean(s.get("object_id"), guard, 200),
                             "label": clean(s.get("label"), guard, 200)}
                            for s in kept.get("subjects") or []]}
    entry = _add(store, who, {
        "kind": "answer", "rating": request.rating, "note": note, "expected": expected,
        "page": clean(request.page, guard, 300) or "/ask", "answer_id": request.answer_id,
        "question": question, "answer": answer, "label": question[:200] or None,
        "context": context})
    done = None
    if then is not None:
        try:
            done = then()
        except BaseException:
            store.remove(entry["id"])
            raise
    return entry, done


def record_item(store: FeedbackStore, backend: Any, reader: Any, request: Any
                ) -> dict[str, Any]:
    guard = exclusion()
    who = _who(reader, guard)
    context = {**_context_common(backend, reader, request.app_version, who["_hidden"]),
               "item_kind": request.item_kind, "event_type": request.event_type}
    return _add(store, who, {
        "kind": "alert", "verdict": request.verdict,
        "note": clean(request.note, guard, 2000), "page": clean(request.page, guard, 300),
        "item_id": request.item_id, "object_type": request.object_type,
        "object_id": clean(request.object_id, guard, 200),
        "label": clean(request.label, guard, 300), "context": context})


def record_note(store: FeedbackStore, backend: Any, reader: Any, request: Any
                ) -> dict[str, Any]:
    """A note: general feedback needs nothing but the note; the page and the record are
    kept only when sent."""
    guard = exclusion()
    who = _who(reader, guard)
    context = _context_common(backend, reader, request.app_version, who["_hidden"])
    return _add(store, who, {
        "kind": request.kind, "note": clean(request.note, guard, 2000),
        "page": clean(request.page, guard, 300), "object_type": request.object_type,
        "object_id": clean(request.object_id, guard, 200),
        "label": clean(request.label, guard, 300), "context": context})


# ---- command line -------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operator_api.feedback")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("digest", help="write a day's digest (default: yesterday, ET)")
    one.add_argument("--state-dir", type=Path, required=True)
    one.add_argument("--day", type=date.fromisoformat)
    args = parser.parse_args(argv)
    store = FeedbackStore(Path(os.environ.get("PLX_FEEDBACK_DIR") or args.state_dir / "feedback"))
    try:
        day = args.day or eastern_day(_now()) - timedelta(days=1)
        print(json.dumps({"digest": str(store.write_digest(day))}))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
