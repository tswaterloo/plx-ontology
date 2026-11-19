"""The excluded people's own data as one precomputed set (business rule 2026-11-17).

A full reader (the owner and the configured full readers, ``EmployeeEntitlement.
unrestricted``) has no runtime permission check: no audience list, no marking, no per-item
gate. The one rule that still holds for them is the people exclusion, and it is decided
here once per delta, never per row: the object versions that are an excluded person's own
data (migration 0040, ``ontology.people_excluded_objects``):

* ``own_unit``: every version of an object in an excluded person's own unit (their mailbox
  ``mail:<oid>``, calendar ``meetings:calendar:<oid>`` or organizer unit
  ``meetings:organizer:<oid>``), and of one whose owner is no object id (nothing can clear
  it; the full reader's policy denied those units before, ``ontology_host.
  _full_reader_policy``);
* ``participant``: every version of a message, chat message or event whose sender,
  To/Cc/Bcc recipient, organizer or participant the participant rule withholds
  (``operating.excluded_people.participants_withheld``: an excluded person, or a tenant
  identity nothing can decide).

Not covered ("excluded people aren't that deep"): an
email of someone else that merely quotes their address in its body, and business records
(contacts, supplier rows, MC tasks, list items, GL lines) naming them as a contact or
creator. Those stay visible.

The rows never leave Postgres: a process reads only the
state row (:func:`excluded`, re-read every ``REFRESH_S``), a full reader's object filter is a
``NOT EXISTS`` over the rows inside the reader's own statement (``operating.search.
document_marks.sql``, the ``object_id`` index), and the item gate looks a handle's rows up by
index (:meth:`Excluded.withholds`), each statement re-checking the state row (the same list
and ``covered_ver``), so an undo (TRUNCATE) or a rebuild for another list takes effect on
the very next statement. Only full readers use the set: staff keep their gates, which
already withhold an excluded person's items. :func:`excluded` answers None (the full reader
then keeps every per-item gate, fail closed) whenever the set cannot be used: Postgres does
not serve, the table or its state row is missing or unreadable, or the state was computed
for another exclusion list than the one this process holds (``PeopleExclusion.
members_checked_digest``). It never answers an empty set for an undecided exclusion.

The set is a snapshot of the served versions up to one version number (the state row's
``covered_ver``: every ``ontology.object_versions.ver`` at or below it was committed before
the set was computed; read under the delta's lock, :func:`watermark`). A handle is decided by
the set only while it is *covered* (:meth:`Excluded.covered`): Postgres serves it (or its
attachment, for a page) at or below that number and holds no newer version of it, its page
form or its message. Mail, Teams and meetings recorded after the set was computed, or a
handle Postgres does not hold, go through the per-item gate as before (fail closed), until
the incremental update (:func:`update`, after each delta cycle) decides the new versions and
moves ``covered_ver`` up in the same transaction. A failed update changes nothing.

  python3 -m operating.pg.people_excluded build     (TRUNCATE + COPY, one transaction)
  python3 -m operating.pg.people_excluded update    (only the versions after covered_ver;
                                                     a full build when the list changed)
  python3 -m operating.pg.people_excluded verify    (counts only; names nobody)

Undo: TRUNCATE ontology.people_excluded_objects, ontology.people_excluded_state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

TABLE = "ontology.people_excluded_objects"
STATE = "ontology.people_excluded_state"
ENV = "PLX_ONTOLOGY_PG"
REFRESH_S = 30.0   # the state row only: one short statement
RETRY_S = 30.0
# ``uncaptured``: a served mail, Teams or meetings version whose captured row the capture no
# longer holds (purged, another store generation): its item gate admits it for nobody.
REASONS = ("own_unit", "participant", "uncaptured")
# The handle schemes the set decides whole (their item gate is the people exclusion over the
# capture's rows); every other scheme (SharePoint files, list items, originals) keeps its gate.
COVERED_SCHEMES = ("graph-mail", "teams-chat", "teams-channel", "calendar-event",
                   "planner-task", "meeting-transcript")
_TEAMS = ("teams-chat", "teams-channel")
_MEETINGS = ("calendar-event", "planner-task", "meeting-transcript")
_MEETING_PARTS = ("calendar", "plan", "organizer")
# A person's own units: their mailbox, calendar and organizer (transcripts) unit.
OWN_PREFIXES = ("mail:", "meetings:calendar:", "meetings:organizer:")


@dataclass(frozen=True)
class Excluded:
    """The set's state row as this process read it: the exclusion tag the rows were
    computed for and the newest version number they decided. The rows themselves stay in
    Postgres (:meth:`withheld`, ``document_marks.sql``)."""
    exclusion: str
    # The newest version number the set decided (state ``covered_ver``); None: covers no
    # handle (every gate stays on).
    covered_ver: int | None = None

    def covers(self, handle: Any) -> bool:
        """Whether the set decides ``handle`` whole (:func:`decides`)."""
        return decides(handle)

    def covered(self, handle: str, container: str | None = None,
                versions: Mapping[str, int] | None = None) -> bool:
        """Whether the set decided ``handle`` (a member of ``container``) as Postgres serves
        it now: the handle (or, for a page, its attachment) is served at a version at or
        below ``covered_ver`` and no newer version of the handle, its page form, its
        message or its container exists. ``versions`` (handle -> newest ``ver``,
        :func:`latest_versions` of :func:`related`) is read when not given; an unreadable
        lookup covers nothing (the gate decides)."""
        top = self.covered_ver
        if top is None or not handle:
            return False
        names = related(handle, container)
        if versions is None:
            versions = latest_versions(names)
            if versions is None:
                return False
        if not any(versions.get(n) is not None for n in names[:2]):
            return False   # not served (nor its attachment): never computed
        return all(versions.get(n) is None or versions[n] <= top for n in names)

    def withheld(self, names: Iterable[str]) -> frozenset[str] | None:
        """Those of ``names`` (handles) the set holds, read by index in one statement that
        also re-reads the state row; None when the lookup fails or the state is no longer
        this one's (another list, an older ``covered_ver``, no state after an undo): then
        nothing is decided here (the gate decides)."""
        if self.covered_ver is None:
            return None
        names = sorted({n for n in names if n})
        try:
            return _LOOKUP.withheld(names, self.exclusion, self.covered_ver)
        except Exception:  # noqa: BLE001 - unreadable: the gate decides
            return None

    def withholds(self, handle: str, container: str | None = None,
                  held: frozenset[str] | None = None) -> bool | None:
        """Whether ``handle`` (a page of it, a member of ``container``) is the set's: the
        handle, the handle without its page, its message (before ``#``), or its
        container (the same checks as ``ontology.sq_item_gate``'s full reader branch).
        ``held``: :meth:`withheld` of :func:`withhold_names` read ahead (else read here).
        None when that cannot be read (the gate decides)."""
        names = withhold_names(handle, container)
        if held is None:
            held = self.withheld(names)
            if held is None:
                return None
        return any(n in held for n in names)


def withhold_names(handle: str, container: str | None = None) -> tuple[str, ...]:
    """The handles :meth:`Excluded.withholds` looks for: of ``handle`` and ``container``
    each, itself, without its page, and its message (before ``#``)."""
    out: list[str] = []
    for one in (handle, container):
        if one:
            out.extend((one, _PAGE.sub("", one), one.partition("#")[0]))
    return tuple(dict.fromkeys(out))


_PAGE = re.compile(r"[#&]page=[0-9]+$")
# The handle forms the set decides: an item itself (no fragment) or a mail attachment (its
# member or page). Any other fragment (a page of a message, a form the gates do not know) is
# left to the item gate, which denies what it cannot read (fail closed); the same pattern
# guards ``ontology.sq_item_gate``'s full reader branch (migration 0040).
DECIDED_FORM = r"^[^#]*(#attachment=[^#]*)?$"
_DECIDED = re.compile(DECIDED_FORM)
# A person citation (``ontology.people_directory``: ``graph-mail://<mailbox or people>/
# person/<key>``) is no captured message: its gate decides it on the reader's mailboxes and
# the exclusion now. The set never holds or decides one (the first dry run on served data
# held all 46,135 as 'uncaptured', which would have hidden every mail person).
PERSON_FORM = r"^graph-mail://[^/#]+/person/"
_PERSON = re.compile(PERSON_FORM)


def related(handle: str, container: str | None = None) -> tuple[str, ...]:
    """The served handles the set's answer for ``handle`` rests on: the handle, the handle
    without its page (these two: one must be served), its message (before ``#``) and its
    ``container``."""
    out = [handle, _PAGE.sub("", handle), handle.partition("#")[0]]
    if container:
        out.append(container)
    return tuple(out)


class _Lookup:
    """One autocommit connection per process for :func:`latest_versions` (a lock around
    it; reopened after a failure, when the DSN changes, or in a forked child, which never
    uses its parent's connection)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conn: Any = None
        self.dsn: str | None = None
        self.pid: int | None = None

    def versions(self, handles: Sequence[str]) -> dict[str, int]:
        return dict(self._query(
            "SELECT l1_handle, max(ver) FROM ontology.object_versions "
            "WHERE l1_handle = ANY(%s::text[]) GROUP BY 1", (list(handles),)))

    def withheld(self, handles: Sequence[str], exclusion: str, covered: int
                 ) -> frozenset[str] | None:
        """Those of ``handles`` the set holds, and the state row checked in the same
        statement (one snapshot): None unless it is still of ``exclusion`` at
        ``covered`` or later."""
        row = self._query(
            f"SELECT s.exclusion, s.covered_ver, ARRAY(SELECT DISTINCT e.l1_handle FROM "
            f"{TABLE} e WHERE e.l1_handle = ANY(%s::text[])) FROM {STATE} s",
            (list(handles),))
        if len(row) != 1 or row[0][0] != exclusion or not isinstance(row[0][1], int) \
                or row[0][1] < covered:
            return None
        return frozenset(row[0][2] or ())

    def _query(self, sql: str, args: tuple) -> list:
        import psycopg
        from operating.pg import config
        dsn = config.dsn()
        with self.lock:
            try:
                if self.pid != os.getpid():
                    self.conn, self.dsn = None, None   # the parent's: never touched here
                if self.conn is None or self.conn.closed or self.dsn != dsn:
                    self.close_locked()
                    self.conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
                    self.dsn, self.pid = dsn, os.getpid()
                return self.conn.execute(sql, args).fetchall()
            except Exception:
                self.close_locked()
                raise

    def close_locked(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001 - closing a broken connection
                pass
        self.conn, self.dsn = None, None


_LOOKUP = _Lookup()


def latest_versions(handles: Iterable[str]) -> dict[str, int] | None:
    """Handle -> its newest served version number (``ontology.object_versions.ver``; a
    handle Postgres does not hold is absent), one index lookup per handle in one
    statement; None when the lookup fails (then nothing is covered: the gates decide)."""
    names = sorted({h for h in handles if h})
    if not names:
        return {}
    try:
        return _LOOKUP.versions(names)
    except Exception:  # noqa: BLE001 - unreadable: nothing is covered
        return None


def decides(handle: Any) -> bool:
    """Whether the set decides ``handle`` alone: a scheme of ``COVERED_SCHEMES`` and a
    form of ``DECIDED_FORM``, never a person citation (``PERSON_FORM``)."""
    return (isinstance(handle, str) and handle.partition("://")[0] in COVERED_SCHEMES
            and _DECIDED.match(handle) is not None and _PERSON.match(handle) is None)
FULL_KEY = "full:"


def reader_key(material: Any) -> str | None:
    """The full reader's item-gate key (``full:<exclusion tag>``) for the gate key
    material ``("people-excluded", tag)``, else None."""
    if isinstance(material, tuple) and len(material) == 2 \
            and material[0] == "people-excluded" and isinstance(material[1], str) \
            and material[1]:
        return FULL_KEY + material[1]
    return None


def full_reader(base: Any, actor: Any) -> str:
    """The exclusion tag when ``actor`` is a full reader whose item gate on ``base``
    decides by the set (its key material is ``("people-excluded", tag)``,
    ``case_tools_host_gates``), else "" (staff, no usable set, an unreadable gate). The
    object filter (``document_marks.sql``) applies to that reader only."""
    gate = getattr(base, "item_gate", None)
    tag_of = getattr(gate, "full_reader_tag", None)
    if callable(tag_of):   # the gate's own answer, without its whole key material
        try:
            tag = tag_of(actor)
        except Exception:  # noqa: BLE001 - undecidable: no full reader
            return ""
        if isinstance(tag, str):
            return tag if reader_key(("people-excluded", tag)) is not None else ""
        # anything but text is no answer of that hook: the key material decides
    material_of = getattr(gate, "key_material", None)
    if not callable(material_of):
        return ""
    try:
        material = material_of(actor)
    except Exception:  # noqa: BLE001 - no material: no full reader
        return ""
    return material[1] if reader_key(material) is not None else ""


def filter_ready() -> bool:
    """Whether a reader statement may name the set's tables: Postgres serves and this
    process read them (``document_marks.sql``)."""
    return serving() and _KEPT.get() is not None


def serving() -> bool:
    """Whether Postgres serves the ontology (the table's ids are its object ids)."""
    return os.environ.get(ENV) in ("1", "only")


def current_exclusion(environ: Mapping[str, str] | None = None) -> str | None:
    """The one-way tag of the exclusion list this process holds, or None when the guard is
    unavailable (no list, an invalid entry: nothing can be cleared)."""
    from integrations.people_exclusion import PeopleExclusion
    try:
        guard = PeopleExclusion.from_env(os.environ if environ is None else environ)
    except Exception:  # noqa: BLE001 - an unreadable list decides nothing
        return None
    if getattr(guard, "unavailable", "unknown"):
        return None
    return guard.members_checked_digest()


class _Kept:
    """The set's state row, read once per process and refreshed in the background every
    ``REFRESH_S``; None until a read succeeds."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.kept: tuple[float, Excluded | None] | None = None
        self.failed_at: float | None = None
        self.refreshing = False

    @staticmethod
    def _read() -> Excluded | None:
        import psycopg
        from operating.pg import config
        with psycopg.connect(config.dsn(), connect_timeout=5, autocommit=True) as conn:
            for name in (TABLE, STATE):
                if conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is None:
                    return None
            state = conn.execute(f"SELECT exclusion, covered_ver FROM {STATE}").fetchone()
        if state is None or not isinstance(state[0], str) or not state[0] \
                or not isinstance(state[1], int):
            return None
        return Excluded(state[0], state[1])

    def _load(self) -> Excluded | None:
        try:
            got = self._read()
        except Exception:  # noqa: BLE001 - unreadable: none (the gates decide)
            with self.lock:
                self.failed_at, self.refreshing = time.monotonic(), False
                self.kept = None
            return None
        with self.lock:
            self.kept, self.failed_at, self.refreshing = (time.monotonic(), got), None, False
        return got

    def get(self) -> Excluded | None:
        now = time.monotonic()
        with self.lock:
            kept, failed = self.kept, self.failed_at
            if kept is not None:
                if now - kept[0] >= REFRESH_S and not self.refreshing:
                    self.refreshing = True
                    threading.Thread(target=self._load, daemon=True,
                                     name="plx-people-excluded").start()
                return kept[1]
            if failed is not None and now - failed < RETRY_S:
                return None
        return self._load()

    def forget(self) -> None:
        with self.lock:
            self.kept, self.failed_at, self.refreshing = None, None, False


_KEPT = _Kept()


def excluded() -> Excluded | None:
    """The precomputed set when it may be used now (module docstring), else None."""
    if not serving():
        return None
    got = _KEPT.get()
    if got is None:
        return None
    if got.exclusion != current_exclusion():
        return None   # computed for another list: never trusted
    return got


def forget() -> None:
    """Drop the loaded set (the next read re-reads it): after a build, in tests."""
    _KEPT.forget()
    with _LOOKUP.lock:
        _LOOKUP.close_locked()


# ------------------------------------------------------------------ the build

def own_unit_keys(store_keys: Iterable[str], guard: Any) -> list[str]:
    """The audience keys of ``store_keys`` the full reader's policy denies
    (``ontology_host._full_reader_policy``): an excluded person's own unit (the guard
    decides the owner, as there), a person's unit whose owner is no object id, and a
    ``meetings:`` key of no unit kind it grants (fail closed)."""
    from integrations.people_exclusion import is_object_id
    from operating.excluded_people import people_excluded
    out = []
    for key in store_keys:
        key = str(key)
        if key.startswith("meetings:"):
            part, _, owner = key[len("meetings:"):].partition(":")
            if part not in _MEETING_PARTS or not owner.strip():
                out.append(key)   # no meetings unit the full reader's policy grants
                continue
        prefix = next((p for p in OWN_PREFIXES if key.startswith(p)), None)
        if prefix is None:
            continue
        owner = key[len(prefix):].strip().lower()
        if not is_object_id(owner) or people_excluded(guard, [owner]):
            out.append(key)
    return sorted(set(out))


def participant_rows(records: Iterable[tuple[str, str, Sequence[Any], Sequence[Any]]],
                     withheld: Callable[[Sequence[Any], Sequence[Any]], bool]
                     ) -> Iterable[tuple[str, str, str]]:
    """(handle, object id, ``participant``) for each record version ((object id, handle,
    senders, recipients)) the participant rule ``withheld(senders, recipients)`` withholds
    (``excluded_people.participants_withheld`` over the people directory). A rule that
    raises withholds (fail closed)."""
    for object_id, handle, senders, recipients in records:
        try:
            out = withheld(senders, recipients) is not False
        except Exception:  # noqa: BLE001 - undecidable: withheld
            out = True
        if out:
            yield handle, object_id, "participant"


def _checked(rows: Iterable[tuple[str, str, str]]) -> dict[tuple[str, str], str]:
    seen: dict[tuple[str, str], str] = {}   # (handle, object id): one handle may back
    for handle, object_id, reason in rows:  # several objects, each is the set's
        if reason not in REASONS or not handle or not object_id:
            raise ValueError("row_invalid")
        seen.setdefault((handle, object_id), reason)
    return seen


def write(conn: Any, rows: Iterable[tuple[str, str, str]], exclusion: str,
          covered_ver: int) -> dict[str, int]:
    """Replace the set with ``rows`` ((handle, object id, reason)) and its state row (the
    list's tag, ``covered_ver``: the :func:`watermark` the rows were computed up to), in one
    transaction (a reader sees the old set or the new one, never a part)."""
    if not exclusion:
        raise ValueError("exclusion_undecided")
    if not isinstance(covered_ver, int) or covered_ver < 0:
        raise ValueError("covered_ver_invalid")
    seen = _checked(rows)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"TRUNCATE {TABLE}, {STATE}")
        with cur.copy(f"COPY {TABLE} (l1_handle, object_id, reason) FROM STDIN") as copy:
            for (handle, object_id), reason in seen.items():
                copy.write_row((handle, object_id, reason))
        objects = len({o for _h, o in seen})
        cur.execute(f"INSERT INTO {STATE} (exclusion, built_at, objects, handles, "
                    "covered_ver) VALUES (%s, %s, %s, %s, %s)",
                    (exclusion, now, objects, len({h for h, _o in seen}), covered_ver))
    return {"objects": objects, "handles": len({h for h, _o in seen}),
            "covered_ver": covered_ver}


# The delta's own lock (``ontology_delta.apply_layer`` holds it while it numbers and writes
# versions); the build takes it only for the instant it reads the watermark.
DELTA_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))"
# One build or update at a time (a session lock; a second one finds it held and stops).
BUILD_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('people_excluded_build'))"
BUILD_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('people_excluded_build'))"
WATERMARK_LOCK_TIMEOUT = "30s"   # one try's wait for the locks
# a delta writing longer than one try: tried again after a pause, then raises (the
# next timer tick tries again); 5 tries stay well inside the 10-minute tick
WATERMARK_TRIES = 5
WATERMARK_PAUSE_SECONDS = 15.0


def watermark(conn: Any) -> int:
    """The newest served version number below which every version is committed: read in a
    transaction of its own that holds the delta's lock (no delta is numbering or writing
    versions) and a SHARE lock on ``object_versions`` (no other writer has an insert in
    flight), then ends at once. Each try waits at most ``WATERMARK_LOCK_TIMEOUT``; a lock
    still held is tried again after ``WATERMARK_PAUSE_SECONDS``, up to ``WATERMARK_TRIES``
    tries (then raises: nothing is written)."""
    from psycopg.errors import LockNotAvailable
    from psycopg.pq import TransactionStatus
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("watermark_in_transaction")
    for attempt in range(1, WATERMARK_TRIES + 1):
        try:
            with conn.transaction():
                conn.execute(f"SET LOCAL lock_timeout = '{WATERMARK_LOCK_TIMEOUT}'")
                conn.execute(DELTA_LOCK_SQL)
                conn.execute("LOCK TABLE ontology.object_versions IN SHARE MODE")
                top = conn.execute("SELECT coalesce(max(ver), 0) FROM "
                                   "ontology.object_versions").fetchone()[0]
            return int(top)
        except LockNotAvailable:
            if attempt == WATERMARK_TRIES:
                raise
            _pause(WATERMARK_PAUSE_SECONDS)
    raise AssertionError("unreachable")


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def own_unit_rows(conn: Any, guard: Any, pairs: Sequence[tuple[str, str]] | None = None
                  ) -> list[tuple[str, str, str]]:
    """Every version of every object in an own unit (:func:`own_unit_keys` of the store's
    audience keys, read by the audience-key index); with ``pairs`` ((handle, object id),
    :func:`update`), only those."""
    from operating.ontology_tools_policy import AUDIENCE_KEYS_SQL
    if pairs is not None:
        keyed = dict(conn.execute(
            "SELECT object_id, audience_key FROM ontology.objects WHERE object_id = "
            "ANY(%s::text[])", (sorted({o for _h, o in pairs}),)).fetchall())
        units = set(own_unit_keys({k for k in keyed.values() if k is not None}, guard))
        return [(h, o, "own_unit") for h, o in pairs if h and keyed.get(o) in units]
    keys = [r[0] for r in conn.execute(AUDIENCE_KEYS_SQL.replace(
        "FROM objects", "FROM ontology.objects")).fetchall()]
    units = own_unit_keys(keys, guard)
    if not units:
        return []
    rows = conn.execute(
        "SELECT v.l1_handle, o.object_id FROM ontology.objects o JOIN "
        "ontology.object_versions v ON v.object_id = o.object_id "
        "WHERE o.audience_key = ANY(%s::text[])", (units,)).fetchall()
    return [(h, o, "own_unit") for h, o in rows if h]


def verify(conn: Any) -> dict[str, Any]:
    """Counts only: the state row and the rows by reason (names nobody)."""
    state = conn.execute(f"SELECT built_at, objects, handles FROM {STATE}").fetchone()
    reasons = dict(conn.execute(f"SELECT reason, count(*) FROM {TABLE} GROUP BY 1").fetchall())
    return {"built_at": state[0] if state else None,
            "objects": state[1] if state else 0, "handles": state[2] if state else 0,
            "by_reason": reasons, "matches_this_list": bool(
                state) and conn.execute(f"SELECT exclusion FROM {STATE}").fetchone()[0]
            == current_exclusion()}



# The participant half reads the capture stores the item gates read (mail, Teams, meetings),
# read-only, and decides each served version as its gate does for a full reader:
# * mail (``CaptureCaseHostTools._mail_search_gate`` + ``RuntimeCitations``): the latest
#   captured row of the message; withheld when its mailbox is not captured or the exclusion
#   names it (``own_unit``), when it names an address the last purge left undecided, or the
#   participant rule withholds its From/Sender (required) or any To/Cc/Bcc (``participant``);
#   an attachment, page or member with its message, and an attachment for the mail its bytes
#   carry (``MailCaptureStore.carried_withholds``);
# * Teams (``CaseRuntime._capture_resolve``): ``people_excluded`` or the participant rule
#   (object ids only) over the row's ``from``, ``participants`` and the chat's ACL identities;
# * meetings: the same over organizer, attendees, assignees, created_by, completed_by and the
#   ACL identities (every address too);
# * a served version the capture holds no row of: ``uncaptured``.
# Offline: the guard decides addresses with what the mail capture recorded
# (``people_exclusion.with_directory``: its mailboxes, its last tenant listing), never a live
# listing; an address only a live listing could decide stays withheld (fail closed).

_WORK: dict[str, Any] = {}


def _ro(path: Any) -> Any:
    """A read-only SQLite connection to a capture store. A store whose shared-memory file
    this user cannot open is read immutable only while its WAL is empty (the main file is
    then the whole store); otherwise the error stands (no build)."""
    import sqlite3
    from pathlib import Path
    path = Path(path)
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return db
    except sqlite3.OperationalError:
        wal = Path(f"{path}-wal")
        if wal.exists() and wal.stat().st_size:
            raise
        db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return db


_MAIL_SQL = ("SELECT m.l1_handle, m.mailbox_id, json_extract(m.record, '$.from', "
             "'$.sender', '$.to', '$.cc', '$.bcc'), {free} FROM messages m WHERE {where} "
             "AND m.version_id = (SELECT MAX(x.version_id) FROM messages x "
             "WHERE x.l1_handle = m.l1_handle)")


def _mail_reason(mailbox: Any, named: Any, readable: Any, boxes: frozenset,
                 directory: Any, memo: dict) -> str:
    """One message's latest captured row decided as its gate decides it for a full
    reader: ``own_unit``, ``participant`` or ``-`` (served)."""
    from operating.excluded_people import participants_withheld
    if mailbox not in boxes:
        return "own_unit"
    if not readable:
        return "participant"
    values = json.loads(named) if named is not None else [None] * 5
    sent, to = (values[0], values[1]), tuple(values[2:5])
    key = (json.dumps(sent, sort_keys=True), json.dumps(to, sort_keys=True))
    if key not in memo:
        try:
            memo[key] = participants_withheld(directory, [list(to)], sender=list(sent),
                                              sender_required=True) is not False
        except Exception:  # noqa: BLE001 - undecidable: withheld
            memo[key] = True
    return "participant" if memo[key] else "-"


def _mail_chunk(bounds: tuple[int, int]) -> tuple[str, int]:
    """One worker's slice of the mail capture (version ids ``bounds``): every message's
    latest row decided, written as ``handle<TAB>reason`` (``-``: served) to a file of its
    own (the parent COPYs them; nothing large crosses the process boundary)."""
    import sqlite3
    from integrations.capture_mail import undecided_free_sql
    work = _WORK
    directory, boxes = work["directory"], work["boxes"]
    db = _ro(work["mail_db"])
    db.row_factory = sqlite3.Row
    free = undecided_free_sql(db, "m")
    memo: dict[Any, bool] = {}
    out = os.path.join(work["tmp"], f"mail-{bounds[0]}.tsv")
    count = 0
    with open(out, "w", encoding="utf-8") as fh:
        for handle, mailbox, named, readable in db.execute(_MAIL_SQL.format(
                free=free, where="m.version_id >= ? AND m.version_id < ?"), bounds):
            count += 1
            if "\t" in handle or "\n" in handle:
                raise ValueError("handle_invalid")
            reason = _mail_reason(mailbox, named, readable, boxes, directory, memo)
            fh.write(f"{handle}\t{reason}\n")
    db.close()
    return out, count


def _mail_messages(mail_db: str, messages: Sequence[str], boxes: frozenset, directory: Any
                   ) -> dict[str, str]:
    """The given messages' latest captured rows decided (``_mail_reason``), in this
    process (:func:`update`'s few new messages); a message the capture holds no row of is
    absent."""
    import sqlite3
    from integrations.capture_mail import undecided_free_sql
    db = _ro(mail_db)
    try:
        db.row_factory = sqlite3.Row
        free = undecided_free_sql(db, "m")
        memo: dict[Any, bool] = {}
        out: dict[str, str] = {}
        for lo in range(0, len(messages), CARRIED_BATCH):
            batch = list(messages[lo:lo + CARRIED_BATCH])
            marks = ",".join("?" * len(batch))
            for handle, mailbox, named, readable in db.execute(_MAIL_SQL.format(
                    free=free, where=f"m.l1_handle IN ({marks})"), batch):
                out[handle] = _mail_reason(mailbox, named, readable, boxes, directory, memo)
        return out
    finally:
        db.close()


def _capture_named(scheme: str, row: Mapping[str, Any], meta: Mapping[str, Any]) -> list:
    """What a Teams or meetings item gate names of a captured row (``CaseRuntime.
    _capture_resolve``)."""
    acl = ((meta or {}).get("source_acl") or {}).get("identities")
    if scheme in _TEAMS:
        named = [row.get("from"), row.get("participants")]
        if scheme == "teams-chat":
            named.append(acl)
        return named
    return [*(row.get(k) for k in ("organizer", "attendees", "assignees", "created_by",
                                   "completed_by")), acl]


def capture_withheld(scheme: str, row: Mapping[str, Any], meta: Mapping[str, Any],
                     guard: Any, directory: Any) -> bool:
    """Whether a captured Teams or meetings row is withheld from every reader now (its
    item gate's people checks); any failure withholds."""
    from operating.excluded_people import participants_withheld, people_excluded
    try:
        named = _capture_named(scheme, row, meta)
        return bool(people_excluded(guard, named)) or participants_withheld(
            directory, [named], object_ids_only=scheme in _TEAMS) is not False
    except Exception:  # noqa: BLE001 - undecidable: withheld
        return True


def _store_generation(db: Any) -> str | None:
    try:
        found = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
    except Exception:  # noqa: BLE001 - no meta: no generation
        return None
    return found[0] if found else None


def capture_rows(conn: Any, stores: Mapping[str, Any], guard: Any, directory: Any,
                 pairs: Sequence[tuple[str, str]] | None = None
                 ) -> list[tuple[str, str, str]]:
    """The Teams and meetings rows: every served version of those schemes decided on the
    captured row its handle names (``stores``: scheme family -> open read-only capture
    connection, ``teams`` / ``meetings``; a family with no store: every version
    ``uncaptured``); with ``pairs`` ((handle, object id), :func:`update`), only those of
    these schemes."""
    from integrations.capture_store import parse_handle
    out: list[tuple[str, str, str]] = []
    generations = {name: _store_generation(db) for name, db in stores.items()}

    def decide(served: Iterable[tuple[str, str]]) -> None:
        for handle, object_id in served:
            scheme = handle.partition("://")[0]
            family = "teams" if scheme in _TEAMS else "meetings"
            db = stores.get(family)
            try:
                kind, path, generation, capture_id = parse_handle(handle)
                if db is None or generation != generations.get(family):
                    raise LookupError
                found = db.execute("SELECT payload FROM versions WHERE capture_id = ? AND "
                                   "kind = ? AND path = ?",
                                   (capture_id, kind.name, handle.partition("://")[2]
                                    .rpartition("@")[0])).fetchone()
                if found is None:
                    raise LookupError
            except (ValueError, LookupError):
                out.append((handle, object_id, "uncaptured"))
                continue
            payload = json.loads(found[0])
            if capture_withheld(scheme, payload.get("row") or {}, payload.get("meta") or {},
                                guard, directory):
                out.append((handle, object_id, "participant"))

    if pairs is not None:
        decide([(h, o) for h, o in pairs if h.partition("://")[0] in _TEAMS + _MEETINGS])
        return out
    with conn.cursor(name="people_excluded_capture") as cur:
        cur.itersize = 20000
        cur.execute(*_schemes_sql(_TEAMS + _MEETINGS))
        decide(cur)
    return out


# The build runs beside the served app: at most two mail workers and two Postgres parallel
# workers.
BUILD_WORKERS = 2
CARRIED_BATCH = 500
# Handle order is byte order only under these collations: the handle ranges below are
# exact under them alone (any other: the build refuses, readers keep every gate).
_BYTE_ORDER = ("C", "POSIX", "C.UTF-8", "C.utf8", "ucs_basic")
# The served versions of a carried attachment: the attachment itself, and its members and
# pages (``<attachment>&member=...``, ``<attachment>&page=<n>``: the attachment handle then
# ``&``). Two index lookups per handle (an equality, and the range from ``h&`` up to
# ``h'``, chr(39) the byte after ``&``), never a scan.
_CARRIED_SQL = (
    "SELECT v.l1_handle, v.object_id FROM unnest(%s::text[]) a(h) "
    "JOIN ontology.object_versions v ON v.l1_handle = a.h "
    "UNION ALL SELECT x.l1_handle, x.object_id FROM unnest(%s::text[]) a(h) "
    "CROSS JOIN LATERAL (SELECT v.l1_handle, v.object_id FROM ontology.object_versions v "
    "WHERE v.l1_handle >= a.h || '&' AND v.l1_handle < a.h || chr(39)) x")


def byte_ordered(conn: Any) -> bool:
    """Whether the database orders text by bytes (the handle ranges are exact)."""
    found = conn.execute("SELECT datcollate FROM pg_database WHERE datname = "
                         "current_database()").fetchone()
    return bool(found) and found[0] in _BYTE_ORDER


def _schemes_sql(schemes: Sequence[str]) -> tuple[str, list[str]]:
    """The served versions of ``schemes``: one index range per scheme (``<scheme>://`` up
    to ``<scheme>:/0``, ``0`` the byte after ``/``)."""
    ranges = " OR ".join("(l1_handle >= %s AND l1_handle < %s)" for _ in schemes)
    params: list[str] = []
    for scheme in schemes:
        params += [f"{scheme}://", f"{scheme}:/0"]
    return (f"SELECT l1_handle, object_id FROM ontology.object_versions WHERE {ranges}",
            params)


def carried_versions(cur: Any, carried: Sequence[str]) -> list[tuple[str, str]]:
    """Every served version of the ``carried`` attachments (``_CARRIED_SQL``), in batches
    of ``CARRIED_BATCH``."""
    out: list[tuple[str, str]] = []
    for lo in range(0, len(carried), CARRIED_BATCH):
        batch = list(carried[lo:lo + CARRIED_BATCH])
        out += cur.execute(_CARRIED_SQL, (batch, batch)).fetchall()
    return out


def _is_carried(handle: str, carried: frozenset) -> bool:
    """Whether ``handle`` is a carried attachment, or a member or page of one (the
    attachment handle then ``&``; ``_CARRIED_SQL``)."""
    if handle in carried:
        return True
    at = handle.find("&")
    while at >= 0:
        if handle[:at] in carried:
            return True
        at = handle.find("&", at + 1)
    return False


def mail_rows(conn: Any, state_dir: Any, guard: Any, directory: Any, *,
              workers: int | None = None, pairs: Sequence[tuple[str, str]] | None = None
              ) -> list[tuple[str, str, str]]:
    """The mail rows: every served graph-mail version whose message (its handle before
    ``#``) is withheld or not captured, and attachments withheld for the mail they carry;
    with ``pairs`` ((handle, object id), :func:`update`), only those graph-mail versions,
    decided in this process."""
    import multiprocessing
    import shutil
    from pathlib import Path
    from integrations.capture_mail import MailCaptureStore
    from operating.excluded_people import people_excluded
    store = MailCaptureStore(state_dir, people=guard)
    try:
        listed = store.withheld_mailboxes(guard)
        withheld = None if listed is None else set(listed)
        boxes = set()
        for box, upn, identities in store.db.execute(
                "SELECT mailbox_id, upn, identities FROM mailboxes"):
            if withheld is None or box in withheld:
                continue
            if not people_excluded(guard, [box, upn, *json.loads(identities or "[]")]):
                boxes.add(box)
        top = store.db.execute("SELECT coalesce(MAX(version_id), 0) FROM messages"
                               ).fetchone()[0]
        carried = [r[0] for r in store.db.execute(
            "SELECT a.l1_handle FROM attachments a JOIN carried_people c "
            "ON c.content_sha256 = a.content_sha256")] if store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='carried_people'").fetchone() else []
        mail_db = str(store.path)
    finally:
        store.db.close()
    if pairs is not None:
        mine = [(h, o) for h, o in pairs if h.startswith("graph-mail://")
                and _PERSON.match(h) is None]
        decided = _mail_messages(mail_db, sorted({h.partition("#")[0] for h, _o in mine}),
                                 frozenset(boxes), directory)
        out = []
        for h, o in mine:
            reason = decided.get(h.partition("#")[0], "uncaptured")
            if reason != "-":
                out.append((h, o, reason))
        carried_set = frozenset(carried)
        held = [(h, o) for h, o in mine if _is_carried(h, carried_set)]
        if held:
            store = MailCaptureStore(state_dir, people=guard)
            try:
                out += [(h, o, "participant") for h, o in held
                        if store.carried_withholds(h, guard)]
            finally:
                store.db.close()
        return out
    from ontology import parent_watch, tmp_root
    tmp = tmp_root.mkdtemp(prefix="people-excluded-")
    try:
        _WORK.update(directory=directory, boxes=frozenset(boxes), mail_db=mail_db, tmp=tmp)
        step = max(1, top // 256 + 1)
        slices = [(lo, lo + step) for lo in range(0, top + 1, step)]
        workers = workers or BUILD_WORKERS
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(workers, **parent_watch.pool_init()) as pool:
            files = pool.map(_mail_chunk, slices, chunksize=1)
        _WORK.clear()
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE people_excluded_mail (h text PRIMARY KEY, "
                        "reason text NOT NULL) ON COMMIT DROP")
            with cur.copy("COPY people_excluded_mail (h, reason) FROM STDIN") as copy:
                for path, _count in files:
                    with open(path, encoding="utf-8") as fh:
                        for line in fh:
                            handle, reason = line.rstrip("\n").split("\t")
                            copy.write_row((handle, reason))
            cur.execute("ANALYZE people_excluded_mail")
            rows = cur.execute(
                "SELECT v.l1_handle, v.object_id, coalesce(t.reason, 'uncaptured') "
                "FROM ontology.object_versions v LEFT JOIN people_excluded_mail t "
                "ON t.h = split_part(v.l1_handle, '#', 1) "
                "WHERE v.l1_handle >= 'graph-mail://' AND v.l1_handle < 'graph-mail:/0' "
                "AND v.l1_handle !~ %s AND (t.h IS NULL OR t.reason <> '-')",
                (PERSON_FORM,)).fetchall()
            held = carried_versions(cur, carried)
            cur.execute("DROP TABLE people_excluded_mail")
    finally:
        _WORK.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    out = [(h, o, r) for h, o, r in rows]
    if held:
        store = MailCaptureStore(state_dir, people=guard)
        try:
            out += [(h, o, "participant") for h, o in held
                    if store.carried_withholds(h, guard)]
        finally:
            store.db.close()
    return out


def internal_domains() -> tuple[str, ...]:
    """The case mail scope's internal domains, as the served runtime's ``mail_scope`` (the
    built-in scope: ``CaseRuntime`` takes it when no live reader names another)."""
    from operating.case_mail_scope import MailScope
    return tuple(sorted(MailScope.builtin().internal_domains))


class _Inputs:
    """What every decision reads: the list's tag, the guard and people directory over what
    the mail capture recorded, and the Teams and meetings capture stores (read-only)."""

    def __init__(self, conn: Any, state_dir: Any, environ: Mapping[str, str]) -> None:
        from pathlib import Path
        from integrations.capture_mail import MailCaptureStore, store_exists
        from integrations.people_exclusion import PeopleExclusion, with_directory
        from operating.people_graph import PeopleDirectory
        self.tag = current_exclusion(environ)
        if not self.tag:
            raise RuntimeError("exclusion_undecided")
        self.guard = with_directory(PeopleExclusion.from_env(environ), state_dir)
        if not store_exists(state_dir):
            raise RuntimeError("mail_capture_absent")
        store = MailCaptureStore(state_dir, people=self.guard)
        try:
            withheld = store.withheld_mailboxes(self.guard)
            boxes = [dict(r) for r in store.db.execute(
                "SELECT mailbox_id, upn, identities FROM mailboxes ORDER BY mailbox_id")]
        finally:
            store.db.close()
        self.directory = PeopleDirectory(self.guard, boxes, withheld=withheld,
                                         internal_domains=internal_domains())
        if self.directory.unavailable:
            raise RuntimeError("exclusion_undecided")
        if not byte_ordered(conn):
            raise RuntimeError("collation_not_byte_ordered")
        self.stores: dict[str, Any] = {}
        for family, sub in (("teams", "teams-capture"), ("meetings", "meetings-capture")):
            path = Path(state_dir) / sub / "capture.db"
            if path.is_file():
                self.stores[family] = _ro(path)

    def close(self) -> None:
        for db in self.stores.values():
            db.close()

    def rows(self, conn: Any, state_dir: Any, *, workers: int | None = None,
             pairs: Sequence[tuple[str, str]] | None = None) -> list[tuple[str, str, str]]:
        rows = own_unit_rows(conn, self.guard, pairs)
        rows += mail_rows(conn, state_dir, self.guard, self.directory, workers=workers,
                          pairs=pairs)
        rows += capture_rows(conn, self.stores, self.guard, self.directory, pairs)
        return rows


def _by_reason(rows: Iterable[tuple[str, str, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for _h, _o, reason in rows:
        out[reason] = out.get(reason, 0) + 1
    return out


def _commit_then(conn: Any, fn: Callable[..., Any], *args: Any) -> Any:
    """End the reads' transaction, then ``fn``: its ``conn.transaction()`` is a
    transaction of its own (committed when it ends), never a savepoint of the reads."""
    conn.commit()
    return fn(*args)


def build(conn: Any, state_dir: Any, *,
          environ: Mapping[str, str] | None = None, workers: int | None = None,
          dry_run: bool = False) -> dict[str, Any]:
    """Compute the whole set up to the :func:`watermark` and write it (:func:`write`).
    Refuses (raises) when the exclusion cannot be decided: no state row is written,
    readers keep every gate."""
    environ = os.environ if environ is None else environ
    if not current_exclusion(environ):
        raise RuntimeError("exclusion_undecided")
    top = watermark(conn)   # before any row is read: every version at or below it is seen
    inputs = _Inputs(conn, state_dir, environ)
    conn.execute("SET max_parallel_workers_per_gather = 2")
    try:
        rows = inputs.rows(conn, state_dir, workers=workers)
    finally:
        inputs.close()
    counts = ({"objects": len({o for _h, o, _r in rows}),
               "handles": len({h for h, _o, _r in rows}), "covered_ver": top}
              if dry_run else _commit_then(conn, write, conn, rows, inputs.tag, top))
    if not dry_run:
        counts = {**counts, "door": _door_follows(conn)}
    return {**counts, "by_reason": _by_reason(rows)}


def _door_follows(conn: Any, *, stale_only: bool = False) -> dict[str, Any]:
    """The DOOR v2 tables follow the set just written (``operating.pg.door.exclude`` on
    every v2 schema). Until they do, the door refuses its readers (``door.serves``)."""
    from operating.pg import door
    conn.commit()
    out = door.exclude_everywhere(conn, stale_only=stale_only)
    conn.commit()
    return out


# The served versions of the new versions' messages: the message itself and everything
# under it (``<message>#...``: attachments, members, pages; ``$`` is the byte after ``#``),
# two index lookups per message, never a scan (byte-ordered collation, :func:`byte_ordered`).
_UNDER_SQL = (
    "SELECT DISTINCT x.l1_handle, x.object_id FROM unnest(%s::text[]) a(h) "
    "CROSS JOIN LATERAL (SELECT v.l1_handle, v.object_id FROM ontology.object_versions v "
    "WHERE v.l1_handle = a.h UNION ALL SELECT v.l1_handle, v.object_id "
    "FROM ontology.object_versions v WHERE v.l1_handle >= a.h || '#' "
    "AND v.l1_handle < a.h || '$') x")


def _state(conn: Any) -> tuple[str, int | None] | None:
    for name in (TABLE, STATE):
        if conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is None:
            raise LookupError("not_migrated")
    found = conn.execute(f"SELECT exclusion, covered_ver FROM {STATE}").fetchone()
    return None if found is None else (found[0], found[1])


def update(conn: Any, state_dir: Any, *, environ: Mapping[str, str] | None = None,
           workers: int | None = None) -> dict[str, Any]:
    """Decide only the versions recorded after the set's ``covered_ver`` (the index range
    on ``ver`` up to a new :func:`watermark`), with every version of their messages (an
    attachment is decided with its message), replace those handles' rows and move
    ``covered_ver`` up, in one transaction that first checks the state is still the one
    read (a build or another update in between: it raises, nothing is written). A set of
    another list, or with no ``covered_ver``, is rebuilt whole (:func:`build`). No set or
    no tables: nothing to do. Any failure leaves the set as it was: the versions after its
    ``covered_ver`` stay with their per-item gates."""
    environ = os.environ if environ is None else environ
    try:
        state = _state(conn)
    except LookupError as exc:
        conn.rollback()
        return {"skipped": str(exc)}
    conn.rollback()   # end the read: the watermark takes a transaction of its own
    if state is None:
        return {"skipped": "not_built"}
    tag = current_exclusion(environ)
    if not tag:
        raise RuntimeError("exclusion_undecided")
    if state[0] != tag or not isinstance(state[1], int):
        return {**build(conn, state_dir, environ=environ, workers=workers), "full": True}
    low = state[1]
    top = watermark(conn)
    if top <= low:
        return {"covered_ver": low, "versions": 0, "handles": 0, "by_reason": {},
                "door": _door_follows(conn, stale_only=True)}
    inputs = _Inputs(conn, state_dir, environ)
    try:
        new = [r[0] for r in conn.execute(
            "SELECT DISTINCT l1_handle FROM ontology.object_versions "
            "WHERE ver > %s AND ver <= %s", (low, top)).fetchall()]
        bases = sorted({h.partition("#")[0] for h in new})
        pairs: set[tuple[str, str]] = set()
        with conn.cursor() as cur:
            for lo in range(0, len(bases), CARRIED_BATCH):
                pairs.update(cur.execute(_UNDER_SQL, (bases[lo:lo + CARRIED_BATCH],))
                             .fetchall())
        ordered = sorted(pairs)
        rows = inputs.rows(conn, state_dir, workers=workers, pairs=ordered)
    finally:
        inputs.close()
    seen = _checked(rows)
    handles = sorted({h for h, _o in ordered})
    conn.commit()   # the reads end; the write below is one transaction of its own
    from operating.pg import door
    with conn.transaction(), conn.cursor() as cur:
        now = cur.execute(f"SELECT exclusion, covered_ver FROM {STATE} FOR UPDATE").fetchone()
        if now is None or (now[0], now[1]) != (tag, low):
            raise RuntimeError("set_changed")
        # the objects this update adds to the set (read before its rows are replaced):
        # a door decided against the set before holds no row of any other excluded object
        objects = sorted({object_id for _handle, object_id in seen})
        held: set[str] = set()
        for lo in range(0, len(objects), CARRIED_BATCH):
            held.update(r[0] for r in cur.execute(
                f"SELECT DISTINCT p.object_id FROM unnest(%s::text[]) a(o) "
                f"JOIN {TABLE} p ON p.object_id = a.o", (objects[lo:lo + CARRIED_BATCH],)))
        grown = [o for o in objects if o not in held]
        for lo in range(0, len(handles), CARRIED_BATCH):
            cur.execute(f"DELETE FROM {TABLE} WHERE l1_handle = ANY(%s::text[])",
                        (handles[lo:lo + CARRIED_BATCH],))
        with cur.copy(f"COPY {TABLE} (l1_handle, object_id, reason) FROM STDIN") as copy:
            for (handle, object_id), reason in seen.items():
                copy.write_row((handle, object_id, reason))
        cur.execute(f"UPDATE {STATE} SET covered_ver = %s, updated_at = %s, "
                    f"objects = (SELECT count(DISTINCT object_id) FROM {TABLE}), "
                    f"handles = (SELECT count(DISTINCT l1_handle) FROM {TABLE})",
                    (top, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        # the DOOR v2 tables follow the grown set in this same short transaction, by the
        # added objects alone (``door.exclude_within``: the lineage's indexes, under a
        # second): the grown set takes effect when it commits, with its rows dropped, and
        # no reader is refused meanwhile. (Committed apart, every reader was refused for
        # the minutes a whole-set follow takes; followed whole inside it, the old rows
        # were served those minutes after the set was decided.)
        followed = door.exclude_within(conn, json.dumps([tag, low]), grown)
    if any(not isinstance(v, int) for v in followed.values()):
        # a schema that was behind or failed: decided whole now (refused until then)
        followed = {**followed, **_door_follows(conn, stale_only=True)}
    return {"covered_ver": top, "versions": len(ordered), "handles": len(handles),
            "objects_added": len(grown), "by_reason": _by_reason(rows), "door": followed}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.people_excluded")
    parser.add_argument("command", choices=("build", "update", "verify"))
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="build: compute and count, write nothing")
    args = parser.parse_args(argv)
    import psycopg
    from operating.pg import config
    with psycopg.connect(config.dsn(), connect_timeout=10) as conn:
        if args.command == "verify":
            print(json.dumps(verify(conn), sort_keys=True))
            return 0
        if not conn.execute(BUILD_LOCK_SQL).fetchone()[0]:
            print(json.dumps({"skipped": "busy"}))
            return 0
        conn.commit()   # the session lock stays; the watermark needs an idle session
        try:
            if args.command == "update":
                result = update(conn, args.state_dir, workers=args.workers)
            else:
                result = build(conn, args.state_dir, workers=args.workers,
                               dry_run=args.dry_run)
        except RuntimeError as exc:
            print(json.dumps({"refused": str(exc)}))
            return 1
        finally:
            conn.rollback()
            conn.execute(BUILD_UNLOCK_SQL)
            conn.commit()
        forget()
        print(json.dumps(result, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
