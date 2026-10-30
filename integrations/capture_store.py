"""Versioned L1 capture store for live sources (BC, Teams; mail and SharePoint via the registry).

BC, mail and SharePoint had no
capture store and were re-read live on every resolve. This module keeps every
captured version of a source record in one SQLite file, so a citation resolves
offline to exactly what it cited, a changed source version keeps its history
and the projection (L7) can be rebuilt "as known at" any capture time.

Source kinds. A ``SourceKind`` (registry: ``register_source_kind``) fixes the
citation scheme, the record path grammar (``<scheme>://p1/.../pn``), the
audience unit a record belongs to (the C0 ``objects.audience_key``:
``bc:<company>/<entity set>``, ``mail:<mailbox>``) and the live-audience
resources a reader must hold. The BC kind is defined here (``BC_KIND``); the
mail adapter (L3) registers its own kind the same way. Capture code
(``integrations.bc_read.BcCapture``) owns the source reads; this module never
does I/O beyond its SQLite file.

Handles have the source mirror's shape (``api://<relation>/<key>@<generation>-
<capture-id>``, ``integrations/source_mirror.py``): ``bc://<environment>/
<company-id>/<entitySet>/<record-id>@<generation>-<capture-id>``. The tail is
never a BC etag (``W/"..."`` is quoted to ``W%2F%22...``), so
``is_capture_handle`` tells a capture handle from a live ``bc://...@<etag>``
citation. ``generation`` is random per store file: a rebuilt store never
resolves an old handle to reused capture ids (``capture_unknown``,
``store_generation_mismatch``). A resolved handle returns ``row`` (the source
fields) and ``key``, so ``operating.claim_checks`` checks ``row.<field>``
claims on it unchanged (``claim_rows``).

Versions (R8). One row per captured version: ``recorded_at`` when first
captured, ``superseded_at`` (and ``superseded_by``) when a later capture of the
same record held other content; at most one current version per record. A
record seen again with the same content only updates ``last_seen_*``. A unit
whose run finished complete marks current records it did not see with
``missing_from_source_since`` (missing from the source, never "deleted"); a
record seen again is unmarked and that stretch is kept in ``removals`` (since,
until), so the projection still reads the record as not held inside it
(``REMOVALS_COLUMN``); retention drops a stretch with the versions it outlived (one
that ended by the first version kept). ``as_known_at`` reads the versions known then.

Runs (resumable paging). ``begin_run`` resumes a unit's unfinished run (its
cursor: the next page URL the capture code validated) or starts a new one;
``save_page`` commits a page's records and the next cursor in one
transaction, so a crash or a throttle resumes after the last committed page.
``pause_run`` keeps the cursor with an error and an optional
``retry_not_before`` (a long ``Retry-After``); ``finish_run`` records the
final state. ``operator_completeness`` reports per unit (capture actor and
operators: raw sizes and run counters): ``complete`` only after a full
traversal with nothing withheld or dropped; otherwise ``in_progress``
(resumable), ``partial`` or ``failed`` with the reason. ``completeness`` is the
reader view: the same state, but only visible/withheld record counts after the
item check, and never complete while anything is withheld from the reader. No
capture is an atomic upstream snapshot.

Permissions: the store is opened with a live audience (``allows(actor,
resource)``, ``tenant``; ``live_source_guard.LiveSourceAudience``). Every read
path (resolve, records, completeness counts) checks the actor against the
record's resources (and its version's ``item_resources``) before it returns, ranks or
counts anything; default deny.
Each run declares its unit's resources and every record saved in it must name
exactly those, so a unit (even an empty one) is checked the same way; a unit
never captured reads as not visible.
The first write pins the audience tenant; reopening the file for another tenant
refuses every operation (``binding_mismatch``). A citation read opens the file with
``open_read_only``: it writes nothing and never waits on a capture's write lock.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote, unquote

from ontology.store_ddl import LINEAGES
from operating.capture_age import MAX_CAPTURE_AGE, capture_decides

FORMAT = "plx-capture-store/v1"
# Retention bound: versions kept per record (the current one included). Older
# superseded versions are dropped; their handles resolve as ``capture_unknown``
# with reason ``pruned_by_retention``, never to other content.
DEFAULT_MAX_VERSIONS_PER_RECORD = 50
MAX_BATCH_HANDLES = 5000
MAX_RECORDS_LIMIT = 1000
# How long a unit's recorded source ACL (``set_unit_acl``) decides who sees the unit: the one
# capture-age bound (``operating.capture_age``, 48 hours: one missed daily capture). A
# membership a transient read failure kept (``integrations.capture_teams``) or one no capture
# refreshed since is past it withheld from every reader until a members read records it again.
UNIT_ACL_MAX_AGE = MAX_CAPTURE_AGE
RUN_STATES = ("in_progress", "complete", "partial", "failed")

_GENERATION = re.compile(r"^[0-9a-f]{16}$")
_VERSION_TAIL = re.compile(r"^([0-9a-f]{16})-([1-9][0-9]{0,17})$")
_BC_LINEAGE = re.compile(r"^bc_[a-z0-9][a-z0-9_]{0,47}$")  # format: ^bc_[a-z0-9][a-z0-9_]{0,47}$
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ENVIRONMENT = re.compile(r"^[a-z0-9_-]{1,50}$")
_ENTITY_SET = re.compile(r"^[a-z][A-Za-z0-9]{1,63}$")


class CaptureError(Exception):
    """A typed refusal of a store operation (``code`` is stable)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def lineage_ok(value: Any) -> bool:
    """A C0 lineage (``ontology/store_ddl.py`` LINEAGES or ``bc_<company>``)."""
    return isinstance(value, str) and (value in LINEAGES or _BC_LINEAGE.fullmatch(value)
                                       is not None)


# ---------------------------------------------------------------- source kinds

@dataclass(frozen=True)
class SourceKind:
    """How one live source's records are addressed, cited and permissioned.

    ``canonical_path(parts)`` validates a record path and returns its canonical
    form (or raises ``ValueError``); ``unit(path)`` names the audience unit
    (C0 ``audience_key``, also the completeness unit); ``resources(path)`` are
    the live-audience resources a reader must all hold to see the record;
    ``key(path)`` is the record's native key (``key.<field>`` in claims).
    ``unit_acl``: the unit's latest source ACL (``CaptureStore.set_unit_acl``, e.g. a
    Teams conversation's membership) also gates every version of the unit, superseded
    and missing ones included, so a revoked reader loses old versions too; a unit
    with none recorded shows nothing (``clear_unit_acl``: its membership could not be read),
    nor one whose recorded ACL is older than ``UNIT_ACL_MAX_AGE`` (its ``recorded_at``,
    refreshed by every ``set_unit_acl``; undated or dated after now decides nothing either).
    ``unit_acl_for(acl, payload)``: that unit ACL as it applies to one captured version
    (a Teams chat's members whose current visible history covers the message,
    ``integrations.capture_teams``); without it the unit ACL applies as recorded.
    ``item_resources(path, payload)``: more resources one captured version needs, read
    from its payload (Mission Control's project, ``integrations.mc_sources``); a
    ``ValueError`` hides the version. A superseded version also needs its record's current
    version's (most restrictive wins), so a record moved out of a reader's grant hides
    its history from that reader too.
    ``current_acl``: a superseded version also needs its record's current version's source
    ACL (a SharePoint list item's permissions, ``integrations.capture_sharepoint_lists``), so
    a reader an item's permissions no longer grant loses its history too."""

    name: str
    scheme: str
    path_parts: int
    canonical_path: Callable[[Sequence[str]], tuple[str, ...]]
    unit: Callable[[tuple[str, ...]], str]
    resources: Callable[[tuple[str, ...]], tuple[str, ...]]
    key: Callable[[tuple[str, ...]], dict[str, Any]]
    unit_acl: bool = False
    unit_acl_for: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None
    item_resources: Callable[[tuple[str, ...], Mapping[str, Any]], tuple[str, ...]] | None = None
    current_acl: bool = False


_KINDS: dict[str, SourceKind] = {}
# Source kinds defined by their capture adapter (imported on first registry use, so a
# reader that only imports this module still finds them). Mail (L3, decision
# 2026-10-02-mail-and-mentions-store): ``integrations.capture_mail`` keeps its own
# versioned store (``MailCaptureStore``) and registers ``graph_mail`` here; SharePoint
# (``integrations.capture_sharepoint``, ``SharePointCaptureStore``) registers
# ``sharepoint`` the same way; Teams (``integrations.capture_teams``) keeps its channel
# and chat messages in a store of this module and registers ``teams_channel`` and
# ``teams_chat``; ``integrations.mc_sources`` registers ``mission_control`` (Mission
# Control reads, kept in this store); ``integrations.capture_sharepoint_lists`` keeps
# SharePoint list items here as ``sharepoint_list``. Static imports only (no dynamic
# module access: scripts/check_http_egress.py).
ADAPTER_MODULES: dict[str, str] = {"graph_mail": "integrations.capture_mail",
                                   "sharepoint": "integrations.capture_sharepoint",
                                   "teams_channel": "integrations.capture_teams",
                                   "teams_chat": "integrations.capture_teams",
                                   "mission_control": "integrations.mc_sources",
                                   "sharepoint_list": "integrations.capture_sharepoint_lists",
                                   "brain": "integrations.knowledge_source"}
_ADAPTERS_LOADED = False


def _load_adapters() -> None:
    global _ADAPTERS_LOADED
    if _ADAPTERS_LOADED:
        return
    _ADAPTERS_LOADED = True
    from integrations import capture_mail  # noqa: F401 - registers graph_mail
    from integrations import capture_sharepoint  # noqa: F401 - registers sharepoint
    from integrations import capture_teams  # noqa: F401 - registers teams_channel/_chat
    from integrations import capture_sharepoint_lists  # noqa: F401 - registers sharepoint_list
    from integrations import mc_sources  # noqa: F401 - registers mission_control
    from integrations import knowledge_source  # noqa: F401 - registers brain


def register_source_kind(kind: SourceKind) -> SourceKind:
    """Add a kind; a name or scheme registered twice with another definition is refused."""
    if not isinstance(kind, SourceKind) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", kind.name) \
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", kind.scheme) \
            or not 1 <= kind.path_parts <= 8:
        raise ValueError("source_kind_invalid")
    for other in _KINDS.values():
        if (other.name == kind.name or other.scheme == kind.scheme) and other != kind:
            raise ValueError("source_kind_conflict")
    _KINDS[kind.name] = kind
    return kind


def source_kind(name: str) -> SourceKind:
    if name not in _KINDS:
        _load_adapters()
    try:
        return _KINDS[name]
    except KeyError:
        raise CaptureError("unknown_source_kind", f"no source kind {name!r}") from None


def source_kinds() -> tuple[str, ...]:
    _load_adapters()
    return tuple(sorted(_KINDS))


def _kind_for_scheme(scheme: str) -> SourceKind | None:
    _load_adapters()
    return next((k for k in _KINDS.values() if k.scheme == scheme), None)


# BC (API v2.0): <environment>/<company-id>/<entitySet>/<record-id>, ids lower-case GUIDs.
# Document line sets are visible only with their header set too (as BusinessCentralReader
# .list_lines requires both), so the line -> header map is part of the kind.
BC_LINE_HEADERS: dict[str, str] = {
    "purchaseOrderLines": "purchaseOrders", "purchaseReceiptLines": "purchaseReceipts",
    "purchaseInvoiceLines": "purchaseInvoices", "salesOrderLines": "salesOrders",
    "salesInvoiceLines": "salesInvoices", "salesShipmentLines": "salesShipments",
    "salesCreditMemoLines": "salesCreditMemos",
    "purchaseCreditMemoLines": "purchaseCreditMemos", "salesQuoteLines": "salesQuotes",
    "customerPayments": "customerPaymentJournals", "vendorPayments": "vendorPaymentJournals"}


# Field pages: records of one API v2.0 entity set enriched with fields only a BC web-service
# page serves (``purchaseOrderReferences``: ODataV4 Page 50 ``PurchaseOrder``'s
# ``Your_Reference``, stored under the API v2.0 purchase order's GUID). They are fields of
# that set's documents, so they are visible exactly as that set is: the set's resource only.
BC_FIELD_PAGES: dict[str, str] = {"purchaseOrderReferences": "purchaseOrders"}


def _bc_path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 4 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("bc_path_invalid")
    environment, company, entity_set, record = parts
    environment, company, record = environment.lower(), company.lower(), record.lower()
    if (_ENVIRONMENT.fullmatch(environment) is None or _GUID.fullmatch(company) is None
            or _ENTITY_SET.fullmatch(entity_set) is None or _GUID.fullmatch(record) is None):
        raise ValueError("bc_path_invalid")
    return environment, company, entity_set, record


def _bc_resources(path: tuple[str, ...]) -> tuple[str, ...]:
    environment, company, entity_set, _ = path
    if entity_set in BC_FIELD_PAGES:
        return (f"bc:{environment}:{company}:{BC_FIELD_PAGES[entity_set]}",)
    own = f"bc:{environment}:{company}:{entity_set}"
    header = BC_LINE_HEADERS.get(entity_set)
    return (f"bc:{environment}:{company}:{header}", own) if header else (own,)


def bc_blank_date(value: Any) -> bool:
    """BC sends ``0001-01-01`` (or that day's timestamp) for an empty date: no date at all.
    The one rule for BC dates: ``integrations/bc_purchase.date_value`` and the projector
    (``ontology/project.py``) both read a blank this way."""
    return isinstance(value, str) and value.startswith("0001-01-01")


BC_KIND = register_source_kind(SourceKind(
    name="bc", scheme="bc", path_parts=4, canonical_path=_bc_path,
    unit=lambda path: f"bc:{path[1]}/{path[2]}", resources=_bc_resources,
    key=lambda path: {"id": path[3]}))


def bc_unit(company_id: str, entity_set: str) -> str:
    """C0 audience_key of a BC entity set of one company."""
    return BC_KIND.unit(_bc_path(("x", company_id, entity_set, company_id)))


# ---------------------------------------------------------------- handles

def _path_text(path: Sequence[str]) -> str:
    return "/".join(quote(part, safe="") for part in path)


def make_handle(kind: SourceKind, path: Sequence[str], generation: str, capture_id: int) -> str:
    if _GENERATION.fullmatch(generation) is None:
        raise ValueError("store_generation_invalid")
    return f"{kind.scheme}://{_path_text(path)}@{generation}-{int(capture_id)}"


def parse_handle(handle: Any) -> tuple[SourceKind, tuple[str, ...], str, int]:
    """(kind, canonical path, generation, capture id) or ``ValueError``."""
    if not isinstance(handle, str) or len(handle) > 4096 or "://" not in handle:
        raise ValueError("citation_handle_invalid")
    scheme, _, rest = handle.partition("://")
    kind = _kind_for_scheme(scheme)
    if kind is None or "#" in rest or "?" in rest:
        raise ValueError("citation_handle_invalid")
    body, sep, tail = rest.rpartition("@")
    found = _VERSION_TAIL.fullmatch(tail)
    segments = body.split("/")
    if not sep or found is None or len(segments) != kind.path_parts or not all(segments):
        raise ValueError("citation_handle_invalid")
    try:
        raw = [unquote(s, errors="strict") for s in segments]
    except UnicodeDecodeError:
        raise ValueError("citation_handle_invalid") from None
    path = kind.canonical_path(raw)
    if _path_text(path) != body:  # one spelling per record: no alias handles
        raise ValueError("citation_handle_invalid")
    return kind, path, found[1], int(found[2])


def is_capture_handle(handle: Any) -> bool:
    try:
        parse_handle(handle)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- helpers

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("clock must return aware datetimes")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class CapturedRecord:
    """One source record as read: ``payload`` = ``{"row": {...}, "meta": {...}}`` (JSON
    values only; money as exact decimal text), ``source_version`` the source's own
    version (BC ``@odata.etag``), ``lineage`` a C0 lineage."""

    path: tuple[str, ...]
    payload: Mapping[str, Any]
    source_version: str
    lineage: str


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, unit TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('in_progress','complete','partial','failed')),
        started_at TEXT NOT NULL, finished_at TEXT, updated_at TEXT NOT NULL,
        resources TEXT NOT NULL, cursor TEXT, pages INTEGER NOT NULL DEFAULT 0, records INTEGER NOT NULL DEFAULT 0,
        new_versions INTEGER NOT NULL DEFAULT 0, withheld INTEGER NOT NULL DEFAULT 0,
        dropped INTEGER NOT NULL DEFAULT 0, restarts INTEGER NOT NULL DEFAULT 0,
        reason TEXT, error TEXT, retry_not_before TEXT, detail TEXT)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS runs_open ON runs(kind, unit) "
    "WHERE state = 'in_progress'",
    "CREATE INDEX IF NOT EXISTS runs_by_unit ON runs(kind, unit, run_id)",
    """CREATE TABLE IF NOT EXISTS versions (
        capture_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, path TEXT NOT NULL, unit TEXT NOT NULL,
        source_version TEXT NOT NULL, digest TEXT NOT NULL, lineage TEXT NOT NULL,
        payload TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES runs(run_id),
        recorded_at TEXT NOT NULL, superseded_at TEXT, superseded_by INTEGER,
        last_seen_run INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
        missing_from_source_since TEXT,
        CHECK (superseded_at IS NULL OR superseded_at >= recorded_at))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS versions_current ON versions(kind, path) "
    "WHERE superseded_at IS NULL",
    "CREATE INDEX IF NOT EXISTS versions_by_path ON versions(kind, path, capture_id)",
    "CREATE INDEX IF NOT EXISTS versions_by_unit ON versions(kind, unit, path)",
    # Steps a run finished that are not pages (e.g. the header versions whose lines a
    # document-lines run read), committed with that step's records.
    """CREATE TABLE IF NOT EXISTS run_marks (
        run_id INTEGER NOT NULL REFERENCES runs(run_id), mark TEXT NOT NULL,
        PRIMARY KEY (run_id, mark))""",
    # Superseded versions dropped by the retention bound (their handles report it).
    """CREATE TABLE IF NOT EXISTS pruned (
        capture_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
        unit TEXT NOT NULL, pruned_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS pruned_by_unit ON pruned(kind, unit)",
    # Closed stretches a record was missing from its source (the open one is its current
    # version's missing_from_source_since), kept when the record is seen again.
    """CREATE TABLE IF NOT EXISTS removals (
        kind TEXT NOT NULL, path TEXT NOT NULL, since TEXT NOT NULL, until TEXT NOT NULL,
        CHECK (until >= since))""",
    "CREATE INDEX IF NOT EXISTS removals_by_path ON removals(kind, path)",
    # The latest source ACL of a unit whose kind has ``unit_acl`` (gates all its versions).
    """CREATE TABLE IF NOT EXISTS unit_acl (
        kind TEXT NOT NULL, unit TEXT NOT NULL, acl TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY (kind, unit))""",
)


def wal_journal(db: sqlite3.Connection) -> None:
    """A capture store's writer connection in WAL mode (kept in the file): a reader's open
    snapshot (the projection build, reading while the next capture runs) never blocks the
    writer, nor the writer the reader. A store another connection holds keeps its mode
    for this run (SQLite refuses the switch then); the next writer switches it."""
    try:
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    bound_wal(db)


# A WAL file is cut back to this size each time SQLite resets it (after a checkpoint
# that moved every frame into the database), so one burst of writes never keeps its
# disk space (the incident: an 11 GB WAL beside a 3.7 GB mirror filled the disk).
WAL_SIZE_LIMIT_BYTES = 64 * 1024 ** 2


def bound_wal(db: sqlite3.Connection) -> None:
    """Set ``journal_size_limit`` on a writer connection (a per-connection setting)."""
    db.execute(f"PRAGMA journal_size_limit = {WAL_SIZE_LIMIT_BYTES}")


def wal_bytes(path: str | Path) -> int:
    """The bytes of the WAL file beside the SQLite file ``path`` (0 when there is none)."""
    try:
        return Path(f"{path}-wal").stat().st_size
    except OSError:
        return 0


def checkpoint(db: sqlite3.Connection, *, wait_ms: int = 0) -> dict[str, Any]:
    """``PRAGMA wal_checkpoint(TRUNCATE)`` on ``db``, waiting at most ``wait_ms`` for
    other connections (the connection's own busy timeout is restored after): every WAL
    frame moves into the database and the WAL file is cut to zero bytes, unless a reader
    still holds an older snapshot or a writer holds the lock (``busy`` true: the frames
    up to the oldest snapshot are moved, the file keeps its size until a later
    checkpoint). A writer calls it at the end of each capture run with no wait, so a
    capture never waits for a projection build; the host calls it between refreshes,
    while no build runs, with a short wait. Never raises for a busy or non-WAL file:
    ``state`` is ``truncated``, ``busy``, ``in_transaction`` (nothing done: a checkpoint
    inside an open transaction cannot move its own frames) or ``failed`` with the
    error code (``sqlite_code``); ``closed`` for a connection closed already (a second
    ``close``)."""
    try:
        open_transaction = db.in_transaction
    except sqlite3.ProgrammingError:
        return {"state": "closed"}
    if open_transaction:
        return {"state": "in_transaction"}
    try:
        before = db.execute("PRAGMA busy_timeout").fetchone()[0]
        db.execute(f"PRAGMA busy_timeout = {max(0, int(wait_ms))}")
        try:
            busy, frames, moved = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            db.execute(f"PRAGMA busy_timeout = {int(before)}")
    except sqlite3.Error as exc:
        return {"state": "failed", "error": sqlite_code(exc)}
    return {"state": "busy" if busy else "truncated", "wal_frames": max(frames, 0),
            "frames_moved": max(moved, 0)}


def checkpoint_file(path: str | Path, *, wait_ms: int) -> dict[str, Any] | None:
    """:func:`checkpoint` of the SQLite file at ``path`` on a connection of its own
    (``mode=rw``: never creates a file), with the WAL's bytes before and after; None
    when there is no such file. Only the WAL moves into the file: no row is written."""
    path = Path(path)
    if not path.is_file():
        return None
    before = wal_bytes(path)
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True,
                             isolation_level=None, timeout=max(0, wait_ms) / 1000)
    except sqlite3.Error as exc:
        return {"state": "failed", "error": sqlite_code(exc), "wal_bytes_before": before,
                "wal_bytes": before}
    try:
        bound_wal(db)
        result = checkpoint(db, wait_ms=wait_ms)
    except sqlite3.Error as exc:
        result = {"state": "failed", "error": sqlite_code(exc)}
    finally:
        db.close()
    return {**result, "wal_bytes_before": before, "wal_bytes": wal_bytes(path)}


# SQLite's primary result codes, by the message text Python carries when it gives no
# ``sqlite_errorname`` (Python < 3.11); the code names a cause, never a path or a row.
_SQLITE_MESSAGES = (("disk is full", "disk_full"), ("database is locked", "locked"),
                    ("database table is locked", "locked"), ("disk i/o error", "io_error"),
                    ("malformed", "corrupt"), ("unable to open", "cant_open"),
                    ("readonly", "readonly"), ("read-only", "readonly"),
                    ("not a database", "not_a_database"), ("out of memory", "nomem"),
                    ("interrupted", "interrupted"), ("no such table", "schema"),
                    ("no such column", "schema"))
_SQLITE_NAMES = {"SQLITE_FULL": "disk_full", "SQLITE_BUSY": "locked",
                 "SQLITE_LOCKED": "locked", "SQLITE_IOERR": "io_error",
                 "SQLITE_CORRUPT": "corrupt", "SQLITE_CANTOPEN": "cant_open",
                 "SQLITE_READONLY": "readonly", "SQLITE_NOTADB": "not_a_database",
                 "SQLITE_NOMEM": "nomem", "SQLITE_INTERRUPT": "interrupted"}
# A constraint by its kind (the extended name, else the message's constraint words); the
# table and column the message names are never copied.
_CONSTRAINTS = {"PRIMARYKEY": "constraint_unique", "UNIQUE": "constraint_unique",
                "NOTNULL": "constraint_not_null", "FOREIGNKEY": "constraint_foreign_key",
                "CHECK": "constraint_check"}
_CONSTRAINT_MESSAGES = (("unique constraint failed", "constraint_unique"),
                        ("not null constraint failed", "constraint_not_null"),
                        ("foreign key constraint failed", "constraint_foreign_key"),
                        ("check constraint failed", "constraint_check"))


def sqlite_code(exc: BaseException) -> str:
    """A SQLite error as a short code (``disk_full``, ``locked``, ``io_error``, ...; a
    constraint by its kind, ``constraint_unique``, ``constraint_not_null``, ...;
    ``sqlite_error`` when unknown): the cause, without the message text (which may name
    a table, a column or a path)."""
    name = (getattr(exc, "sqlite_errorname", None) or "").split("_")
    primary = "_".join(name[:2]) if len(name) >= 2 else ""
    if primary in _SQLITE_NAMES:
        return _SQLITE_NAMES[primary]
    text = str(exc).lower()
    if primary == "SQLITE_CONSTRAINT" or isinstance(exc, sqlite3.IntegrityError):
        extended = _CONSTRAINTS.get("_".join(name[2:]))
        return extended or next((code for needle, code in _CONSTRAINT_MESSAGES
                                 if needle in text), "constraint")
    for needle, code in _SQLITE_MESSAGES:
        if needle in text:
            return code
    return "sqlite_error"


def sqlite_bytes(db: sqlite3.Connection) -> int:
    """The bytes of the database ``db`` holds, committed writes included whatever the
    journal mode (a WAL store's writes sit in its WAL file until a checkpoint moves them
    into the file, so the file's size lags behind)."""
    pages = db.execute("PRAGMA page_count").fetchone()[0]
    return pages * db.execute("PRAGMA page_size").fetchone()[0]


class CaptureStore:
    """One SQLite file of versioned captures, read through a live audience."""

    def __init__(self, path: str | Path, audience: Any, *,
                 clock: Callable[[], datetime] | None = None,
                 max_versions_per_record: int = DEFAULT_MAX_VERSIONS_PER_RECORD) -> None:
        self._configure(path, audience, clock, max_versions_per_record)
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        wal_journal(self._db)
        for statement in _SCHEMA:
            self._db.execute(statement)
        with self._write():
            if self._meta("format") is None:
                self._db.execute("INSERT INTO meta VALUES ('format', ?)", (FORMAT,))
                self._db.execute("INSERT INTO meta VALUES ('generation', ?)",
                                 (secrets.token_hex(8),))
                self._db.execute("INSERT INTO meta VALUES ('tenant', ?)", (audience.tenant,))
        if self._meta("format") != FORMAT:
            raise CaptureError("format_mismatch", "capture store has another format")
        self.generation = self._meta("generation") or ""

    @classmethod
    def open_read_only(cls, path: str | Path, audience: Any) -> "CaptureStore":
        """The store opened only to read it (a citation read): ``mode=ro``, no schema
        statement, no journal-mode change and no write transaction, so nothing is created
        or written and a capture holding the write lock never delays the read (a WAL
        reader sees the last commit). A missing file or another format raises
        ``CaptureError``; every writer method fails on the read-only connection. The
        connection may serve another thread (``read_as``): its holder serialises use."""
        store = cls.__new__(cls)
        store._configure(path, audience, None, DEFAULT_MAX_VERSIONS_PER_RECORD)
        if not store.path.is_file():
            raise CaptureError("store_missing", "no capture store at this path")
        store._db = sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True,
                                    isolation_level=None, check_same_thread=False)
        store._read_only = True
        store._db.row_factory = sqlite3.Row
        try:
            store._identify()
        except CaptureError:
            store._db.close()
            raise
        return store

    def read_as(self, audience: Any) -> "CaptureStore":
        """This store read through ``audience`` on the same connection (one connection
        serves many readers, each decided by its own audience; closing either closes
        it). The format and generation are re-read from the file, so a store rewritten
        under an open connection answers for what it holds now (``CaptureError``)."""
        view = copy.copy(self)
        view._configure(self.path, audience, None, self.max_versions_per_record)
        view._identify()
        return view

    def _identify(self) -> None:
        try:
            fmt, generation = self._meta("format"), self._meta("generation") or ""
        except sqlite3.Error:
            fmt = None
        if fmt != FORMAT:
            raise CaptureError("format_mismatch", "capture store has another format")
        self.generation = generation

    def _configure(self, path: str | Path, audience: Any,
                   clock: Callable[[], datetime] | None, max_versions_per_record: int) -> None:
        if not isinstance(getattr(audience, "tenant", None), str) \
                or not callable(getattr(audience, "allows", None)):
            raise ValueError("capture_store_audience_invalid")
        if isinstance(max_versions_per_record, bool) \
                or not isinstance(max_versions_per_record, int) \
                or not 2 <= max_versions_per_record <= 10_000:
            raise ValueError("max_versions_per_record must be 2..10000")
        self.max_versions_per_record = max_versions_per_record
        self.path = Path(path)
        self.audience = audience
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        """Close the connection; a writer first checkpoints its WAL (no wait), so a
        capture run never leaves its writes in a growing WAL file."""
        if not getattr(self, "_read_only", False):
            checkpoint(self._db)
        self._db.close()

    def __enter__(self) -> "CaptureStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- internals

    @contextmanager
    def _write(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def bind_source(self, kind_name: str, binding: str) -> None:
        """Pin the source a kind reads (e.g. BC ``<tenant-id>/<environment>``) on first
        use; a later capture from another source is refused (``binding_mismatch``)."""
        source_kind(kind_name)
        if not isinstance(binding, str) or not binding or len(binding) > 300:
            raise CaptureError("binding_invalid")
        key = f"source_binding:{kind_name}"
        with self._write():
            pinned = self._meta(key)
            if pinned is None:
                self._db.execute("INSERT INTO meta VALUES (?, ?)", (key, binding))
            elif pinned != binding:
                raise CaptureError("binding_mismatch",
                                   "capture store holds captures of another source")

    def stamped_tenant(self) -> str | None:
        """The audience tenant the store's creation pinned (``binding_mismatch`` for any
        other); a caller decides from it whether to capture into this file at all."""
        return self._meta("tenant")

    def _check_binding(self) -> None:
        if self._meta("tenant") != self.audience.tenant:
            raise CaptureError("binding_mismatch",
                               "capture store belongs to another tenant's audience")

    def _now(self) -> str:
        return _iso(self._clock())

    def now(self) -> str:
        """The store clock as fixed-width UTC ISO text (comparable as text)."""
        return self._now()

    def later(self, seconds: float) -> str:
        """``now() + seconds`` in the same form (a ``retry_not_before``)."""
        return _iso(self._clock() + timedelta(seconds=max(0.0, float(seconds))))

    def _visible(self, actor: Any, kind: SourceKind, path: tuple[str, ...],
                 row: sqlite3.Row) -> bool:
        """Every resource of the record and of the version ``row`` (``item_resources``),
        and, where the audience requires it (``source_acl_required``), the version's own
        captured source ACL, exactly as the live reader's ``item_visible`` decides (BC
        exposes none: withheld). A ``unit_acl`` kind also needs the unit's latest source
        ACL (as ``unit_acl_for`` applies it to this version, whatever ACL the version was
        saved with); a superseded version of a ``current_acl`` kind its record's current
        one's."""
        resources = kind.resources(path)
        if not resources:
            return False
        payload = json.loads(row["payload"])
        if kind.item_resources is not None:
            try:
                resources = (*resources, *self._item_resources(kind, path, payload, row))
            except ValueError:
                return False
        source_acl = payload.get("meta", {}).get("source_acl")
        item_visible = getattr(self.audience, "item_visible", None)
        if item_visible is None:
            return not kind.unit_acl and all(self.audience.allows(actor, r) for r in resources)
        acls = [source_acl if isinstance(source_acl, Mapping) else {}]
        superseded = row["superseded_at"] is not None
        if kind.unit_acl:
            row = self._db.execute(
                "SELECT acl, recorded_at FROM unit_acl WHERE kind = ? AND unit = ?",
                (kind.name, kind.unit(path))).fetchone()
            if row is None or not capture_decides(row["recorded_at"], self._clock(),
                                                  UNIT_ACL_MAX_AGE):
                return False
            unit_acl = json.loads(row["acl"])
            acls.append(unit_acl if kind.unit_acl_for is None
                        else kind.unit_acl_for(unit_acl, payload))
        if kind.current_acl and superseded:
            current = self._db.execute(
                "SELECT payload FROM versions WHERE kind = ? AND path = ? "
                "AND superseded_at IS NULL", (kind.name, _path_text(path))).fetchone()
            if current is None:
                return False
            acl = json.loads(current["payload"]).get("meta", {}).get("source_acl")
            acls.append(acl if isinstance(acl, Mapping) else {})
        return all(item_visible(actor, r, acl) for r in resources for acl in acls)

    def _item_resources(self, kind: SourceKind, path: tuple[str, ...],
                        payload: Mapping[str, Any], row: sqlite3.Row) -> tuple[str, ...]:
        """``item_resources`` of a version, plus its current version's when superseded
        (ValueError when there is none)."""
        resources = tuple(kind.item_resources(path, payload))
        if row["superseded_at"] is None:
            return resources
        current = self._db.execute(
            "SELECT payload FROM versions WHERE kind = ? AND path = ? AND superseded_at IS NULL",
            (kind.name, _path_text(path))).fetchone()
        if current is None:
            raise ValueError("current_version_missing")
        return (*resources, *kind.item_resources(path, json.loads(current["payload"])))

    def _path_visible(self, actor: Any, kind: SourceKind, path: tuple[str, ...],
                      path_text: str) -> bool:
        """Whether ``actor`` may see some captured version of ``path``."""
        return any(self._visible(actor, kind, path, row) for row in self._db.execute(
            "SELECT payload, superseded_at FROM versions WHERE kind = ? AND path = ?",
            (kind.name, path_text)))

    # -------------------------------------------------------------- writer side

    def begin_run(self, kind_name: str, unit: str, resources: Sequence[str]) -> dict[str, Any]:
        """The unit's unfinished run (resume from its cursor) or a new one.

        ``resources`` are the live-audience resources a reader must all hold to see
        the unit (every record saved in the run must name exactly these): the
        reader-side checks of an empty or unknown unit use them."""
        source_kind(kind_name)
        self._check_binding()
        if isinstance(resources, (str, bytes)) or not resources \
                or not all(isinstance(r, str) and r for r in resources):
            raise CaptureError("resources_invalid", "a run needs its audience resources")
        declared = _canonical(sorted(r.lower() for r in resources))
        now = self._now()
        with self._write():
            row = self._db.execute("SELECT * FROM runs WHERE kind = ? AND unit = ? "
                                   "AND state = 'in_progress'", (kind_name, unit)).fetchone()
            if row is not None and row["resources"] != declared:
                raise CaptureError("resources_mismatch", "the open run has other resources")
            if row is None:
                cursor = self._db.execute(
                    "INSERT INTO runs (kind, unit, state, resources, started_at, updated_at) "
                    "VALUES (?, ?, 'in_progress', ?, ?, ?)",
                    (kind_name, unit, declared, now, now))
                row = self._db.execute("SELECT * FROM runs WHERE run_id = ?",
                                       (cursor.lastrowid,)).fetchone()
                resumed = False
            else:
                resumed = True
        return {**_run_dict(row), "resumed": resumed}

    def _open_run(self, run_id: int) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row["state"] != "in_progress":
            raise CaptureError("run_not_open", "the run is not in progress")
        return row

    def save_page(self, run_id: int, records: Sequence[CapturedRecord], cursor: str | None, *,
                  withheld: int = 0, dropped: int = 0,
                  marks: Sequence[str] = ()) -> dict[str, int]:
        """Commit one page, the next cursor and the step ``marks`` atomically; returns
        version counts."""
        self._check_binding()
        now = self._now()
        counts = {"records": 0, "new_versions": 0, "unchanged": 0}
        with self._write():
            run = self._open_run(run_id)
            kind = source_kind(run["kind"])
            for record in records:
                if not isinstance(record, CapturedRecord):
                    raise CaptureError("record_invalid")
                path = kind.canonical_path(record.path)
                if kind.unit(path) != run["unit"] or _canonical(sorted(
                        r.lower() for r in kind.resources(path))) != run["resources"]:
                    raise CaptureError("record_outside_unit",
                                       "a record of another unit in this run")
                if not lineage_ok(record.lineage):
                    raise CaptureError("lineage_invalid", "lineage is not a C0 lineage")
                if not isinstance(record.source_version, str) or not record.source_version:
                    raise CaptureError("source_version_missing")
                payload = dict(record.payload)
                if not isinstance(payload.get("row"), Mapping):
                    raise CaptureError("record_invalid", "payload needs a row mapping")
                text = _canonical(payload)
                digest = sha256(text.encode("utf-8")).hexdigest()
                path_text = _path_text(path)
                current = self._db.execute(
                    "SELECT capture_id, digest, lineage, missing_from_source_since FROM "
                    "versions WHERE kind = ? AND path = ? AND superseded_at IS NULL",
                    (kind.name, path_text)).fetchone()
                counts["records"] += 1
                if current is not None and current["missing_from_source_since"] is not None:
                    self._db.execute("INSERT INTO removals VALUES (?, ?, ?, MAX(?, ?))",
                                     (kind.name, path_text,
                                      current["missing_from_source_since"],
                                      current["missing_from_source_since"], now))
                if current is not None and current["digest"] == digest \
                        and current["lineage"] == record.lineage:
                    self._db.execute(
                        "UPDATE versions SET last_seen_run = ?, last_seen_at = ?, "
                        "missing_from_source_since = NULL WHERE capture_id = ?",
                        (run_id, now, current["capture_id"]))
                    counts["unchanged"] += 1
                    continue
                if current is not None:
                    # Supersede first: the partial unique index allows one current version.
                    self._db.execute("UPDATE versions SET superseded_at = ? WHERE capture_id = ?",
                                     (now, current["capture_id"]))
                inserted = self._db.execute(
                    "INSERT INTO versions (kind, path, unit, source_version, digest, lineage, "
                    "payload, run_id, recorded_at, last_seen_run, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (kind.name, path_text, run["unit"], record.source_version, digest,
                     record.lineage, text, run_id, now, run_id, now))
                if current is not None:
                    self._db.execute("UPDATE versions SET superseded_by = ? WHERE capture_id = ?",
                                     (inserted.lastrowid, current["capture_id"]))
                    self._retain(kind.name, path_text, run["unit"], now)
                counts["new_versions"] += 1
            self._db.executemany("INSERT OR IGNORE INTO run_marks VALUES (?, ?)",
                                 [(run_id, str(mark)) for mark in marks])
            self._db.execute(
                "UPDATE runs SET cursor = ?, pages = pages + 1, records = records + ?, "
                "new_versions = new_versions + ?, withheld = withheld + ?, "
                "dropped = dropped + ?, updated_at = ?, error = NULL, retry_not_before = NULL "
                "WHERE run_id = ?",
                (cursor, counts["records"], counts["new_versions"], int(withheld),
                 int(dropped), now, run_id))
        return counts

    def _retain(self, kind_name: str, path_text: str, unit: str, now: str) -> None:
        """Drop the oldest superseded versions beyond ``max_versions_per_record``, and with
        them the removal stretches that ended by the first version kept: no version left
        reads them (before it the record is not known at all)."""
        old = self._db.execute(
            "SELECT capture_id FROM versions WHERE kind = ? AND path = ? "
            "ORDER BY capture_id DESC LIMIT -1 OFFSET ?",
            (kind_name, path_text, self.max_versions_per_record)).fetchall()
        for row in old:
            self._db.execute("DELETE FROM versions WHERE capture_id = ? "
                             "AND superseded_at IS NOT NULL", (row["capture_id"],))
            self._db.execute("INSERT OR IGNORE INTO pruned VALUES (?, ?, ?, ?, ?)",
                             (row["capture_id"], kind_name, path_text, unit, now))
        if old:
            self._db.execute(
                "DELETE FROM removals WHERE kind = ? AND path = ? AND until <= (SELECT "
                "MIN(recorded_at) FROM versions WHERE kind = ? AND path = ?)",
                (kind_name, path_text, kind_name, path_text))

    def run_marks(self, run_id: int) -> set[str]:
        """Writer side: the step marks a run has committed."""
        return {row["mark"] for row in self._db.execute(
            "SELECT mark FROM run_marks WHERE run_id = ?", (run_id,))}

    def current_versions(self, kind_name: str, unit: str) -> list[tuple[tuple[str, ...], int]]:
        """Writer side (no actor): (canonical path, current capture id) of the unit's
        current, not-missing records in path order."""
        kind = source_kind(kind_name)
        return [(kind.canonical_path([unquote(p) for p in row["path"].split("/")]),
                 row["capture_id"])
                for row in self._db.execute(
                    "SELECT path, capture_id FROM versions WHERE kind = ? AND unit = ? "
                    "AND superseded_at IS NULL AND missing_from_source_since IS NULL "
                    "ORDER BY path", (kind_name, unit))]

    def current_records(self, kind_name: str, unit: str, *,
                        seen_in_run: int | None = None) -> list[CapturedRecord]:
        """Writer side (no actor): the unit's current, not-missing versions as records,
        e.g. to save them again under a changed source ACL; with ``seen_in_run`` only those
        that run saved or saw unchanged (on any of its resumed segments)."""
        kind = source_kind(kind_name)
        seen = "" if seen_in_run is None else " AND last_seen_run = ?"
        return [CapturedRecord(kind.canonical_path([unquote(p) for p in row["path"].split("/")]),
                               json.loads(row["payload"]), row["source_version"], row["lineage"])
                for row in self._db.execute(
                    "SELECT path, payload, source_version, lineage FROM versions WHERE kind = ? "
                    "AND unit = ? AND superseded_at IS NULL AND missing_from_source_since IS NULL"
                    + seen + " ORDER BY path",
                    (kind_name, unit, *(() if seen_in_run is None else (seen_in_run,))))]

    def set_unit_acl(self, run_id: int, acl: Mapping[str, Any]) -> None:
        """Record the open run's unit's current source ACL (a ``unit_acl`` kind): from
        now on it gates every version of the unit, whatever ACL a version was saved with."""
        self._check_binding()
        if not isinstance(acl, Mapping):
            raise CaptureError("acl_invalid", "a unit ACL is a mapping")
        with self._write():
            run = self._open_run(run_id)
            if not source_kind(run["kind"]).unit_acl:
                raise CaptureError("unit_acl_unsupported", "this kind has no unit ACL")
            self._db.execute("INSERT OR REPLACE INTO unit_acl VALUES (?, ?, ?, ?)",
                             (run["kind"], run["unit"], _canonical(dict(acl)), self._now()))

    def clear_unit_acl(self, kind_name: str, unit: str) -> None:
        """Forget a unit's source ACL (a ``unit_acl`` kind whose membership could not be
        read): until one is recorded again, none of the unit's versions is visible and the
        unit is not projected."""
        self._check_binding()
        if not source_kind(kind_name).unit_acl:
            raise CaptureError("unit_acl_unsupported", "this kind has no unit ACL")
        with self._write():
            self._db.execute("DELETE FROM unit_acl WHERE kind = ? AND unit = ?",
                             (kind_name, unit))

    def set_cursor(self, run_id: int, cursor: str | None) -> None:
        """Advance the cursor without records (an empty step, e.g. a document with no lines)."""
        with self._write():
            self._open_run(run_id)
            self._db.execute("UPDATE runs SET cursor = ?, updated_at = ? WHERE run_id = ?",
                             (cursor, self._now(), run_id))

    def restart_run(self, run_id: int, reason: str) -> None:
        """Drop the cursor (e.g. an expired continuation): the next step starts over.
        Records already saved by this run keep their ``last_seen_run``."""
        with self._write():
            self._open_run(run_id)
            self._db.execute("UPDATE runs SET cursor = NULL, restarts = restarts + 1, "
                             "reason = ?, updated_at = ? WHERE run_id = ?",
                             (reason, self._now(), run_id))

    def pause_run(self, run_id: int, *, reason: str, error: str | None = None,
                  retry_not_before: str | None = None) -> None:
        """Keep the run resumable (cursor kept) with why it stopped."""
        with self._write():
            self._open_run(run_id)
            self._db.execute("UPDATE runs SET reason = ?, error = ?, retry_not_before = ?, "
                             "updated_at = ? WHERE run_id = ?",
                             (reason, error, retry_not_before, self._now(), run_id))

    def finish_run(self, run_id: int, state: str, *, reason: str | None = None,
                   error: str | None = None, detail: Mapping[str, Any] | None = None
                   ) -> dict[str, Any]:
        """Close a run. ``complete`` (a full traversal, nothing withheld or dropped)
        marks the unit's current records this run did not see as missing."""
        if state not in ("complete", "partial", "failed"):
            raise CaptureError("run_state_invalid")
        now = self._now()
        with self._write():
            run = self._open_run(run_id)
            if state == "complete" and (run["withheld"] or run["dropped"]):
                state, reason = "partial", reason or "withheld_or_dropped"
            marked = 0
            if state == "complete":
                marked = self._db.execute(
                    "UPDATE versions SET missing_from_source_since = ? WHERE kind = ? "
                    "AND unit = ? AND superseded_at IS NULL AND last_seen_run <> ? "
                    "AND missing_from_source_since IS NULL",
                    (now, run["kind"], run["unit"], run_id)).rowcount
            self._db.execute(
                "UPDATE runs SET state = ?, finished_at = ?, updated_at = ?, reason = ?, "
                "error = ?, cursor = NULL, detail = ? WHERE run_id = ?",
                (state, now, now, reason, error,
                 None if detail is None else _canonical(dict(detail)), run_id))
        checkpoint(self._db)   # the run's safe point: its writes leave the WAL
        return {"run_id": run_id, "state": state, "marked_missing": marked}

    def state(self, key: str) -> str | None:
        """Writer side: a capture's own small state under ``key`` (e.g. a rotation
        cursor), or None."""
        return self._meta(f"state:{key}")

    def set_state(self, key: str, value: str) -> None:
        with self._write():
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"state:{key}", value))

    def units(self, kind_name: str) -> list[str]:
        """Writer side: every unit of ``kind_name`` that has a run, sorted."""
        source_kind(kind_name)
        return [row[0] for row in self._db.execute(
            "SELECT DISTINCT unit FROM runs WHERE kind = ? ORDER BY unit", (kind_name,))]

    def run(self, kind_name: str, unit: str) -> dict[str, Any] | None:
        """Writer side: the unit's latest run, or None."""
        row = self._db.execute("SELECT * FROM runs WHERE kind = ? AND unit = ? "
                               "ORDER BY run_id DESC LIMIT 1", (kind_name, unit)).fetchone()
        return None if row is None else _run_dict(row)

    def unit_acl(self, kind_name: str, unit: str) -> dict[str, Any] | None:
        """Writer side (no actor): the unit's latest recorded source ACL (a ``unit_acl``
        kind), or None when none is recorded."""
        source_kind(kind_name)
        row = self._db.execute("SELECT acl FROM unit_acl WHERE kind = ? AND unit = ?",
                               (kind_name, unit)).fetchone()
        return None if row is None else json.loads(row["acl"])

    # -------------------------------------------------------------- reader side

    def _require_unit(self, actor: Any, kind_name: str, unit: str) -> dict[str, Any]:
        """The unit's latest run after the actor's check against the unit's declared
        resources; a unit never captured reads as not visible (nothing to tell)."""
        source_kind(kind_name)
        self._check_binding()
        latest = self.run(kind_name, unit)
        if latest is None or not all(self.audience.allows(actor, r)
                                     for r in json.loads(latest["resources"])):
            raise CaptureError("not_visible", "the unit is not visible to this actor")
        return latest

    def completeness(self, actor: Any, kind_name: str, unit: str) -> dict[str, Any]:
        """Reader-facing completeness of one unit for ``actor`` (who must hold every
        resource of it), with the same disclosure as :meth:`records`.

        Every current version is checked against the actor (item ACL included)
        before it is counted: ``records_visible`` (and ``records_missing`` among
        them) are what this reader may see, ``withheld_count`` how many current
        versions it may not (the convention of ``records``). ``complete`` holds
        only when the latest run traversed everything *and* nothing is withheld
        from this reader (``reason: withheld``; else the run's own reason). No
        sizes, version totals, retention or run counters: those are the operator's
        (:meth:`operator_completeness`)."""
        latest = self._require_unit(actor, kind_name, unit)
        kind = source_kind(kind_name)
        visible = withheld = missing = 0
        for row in self._db.execute(
                "SELECT path, payload, superseded_at, missing_from_source_since FROM versions "
                "WHERE kind = ? AND unit = ? AND superseded_at IS NULL", (kind_name, unit)):
            path = kind.canonical_path([unquote(p) for p in row["path"].split("/")])
            if not self._visible(actor, kind, path, row):
                withheld += 1
                continue
            visible += 1
            missing += row["missing_from_source_since"] is not None
        state = latest["state"]
        result = {"unit": unit, "state": state,
                  "complete": state == "complete" and withheld == 0,
                  "records_visible": visible, "records_missing": missing,
                  "withheld_count": withheld, "resumable": state == "in_progress",
                  "atomic_upstream_snapshot": False, "deletions_proven": False}
        # The run's own reason (an enum: incremental, throttled, ...) describes the state
        # already shown; "withheld" when only this reader's view is incomplete.
        result["reason"] = "withheld" if withheld and state == "complete" else latest["reason"]
        return result

    def operator_completeness(self, actor: Any, kind_name: str, unit: str) -> dict[str, Any]:
        """Operator/writer completeness of one unit: run state and raw counters
        (sizes, version totals, retention, run counters). For the capture actor
        and operator reports only, never a reader tool: the counters are not
        filtered per item (an item ACL the actor fails still counts here). The
        actor must still hold every resource of the unit.

        ``state``: ``complete`` (the latest finished run traversed everything),
        ``in_progress`` (resumable: cursor kept), ``partial`` / ``failed`` (with
        ``reason``/``error``). Counts are of current versions."""
        latest = self._require_unit(actor, kind_name, unit)
        last_finished = self._db.execute(
            "SELECT * FROM runs WHERE kind = ? AND unit = ? AND state <> 'in_progress' "
            "ORDER BY run_id DESC LIMIT 1", (kind_name, unit)).fetchone()
        counts = self._db.execute(
            "SELECT count(*) AS current, "
            "sum(missing_from_source_since IS NOT NULL) AS missing FROM versions "
            "WHERE kind = ? AND unit = ? AND superseded_at IS NULL",
            (kind_name, unit)).fetchone()
        versions, payload_bytes = self._db.execute(
            "SELECT count(*), coalesce(sum(length(payload)), 0) FROM versions "
            "WHERE kind = ? AND unit = ?", (kind_name, unit)).fetchone()
        pruned = self._db.execute("SELECT count(*) FROM pruned WHERE kind = ? AND unit = ?",
                                  (kind_name, unit)).fetchone()[0]
        state = latest["state"]
        result = {"unit": unit, "state": state, "complete": state == "complete",
                  "records_current": counts["current"], "records_missing": counts["missing"] or 0,
                  "versions_total": versions, "payload_bytes": payload_bytes,
                  "retention": {"max_versions_per_record": self.max_versions_per_record,
                                "pruned_versions": pruned},
                  "atomic_upstream_snapshot": False,
                  "deletions_proven": False}
        result.update({k: latest[k] for k in ("run_id", "pages", "records", "new_versions",
                                              "withheld", "dropped", "restarts", "reason",
                                              "error", "retry_not_before", "started_at",
                                              "finished_at")})
        result["resumable"] = latest["state"] == "in_progress"
        if last_finished is not None:
            result["last_finished"] = {"run_id": last_finished["run_id"],
                                       "state": last_finished["state"],
                                       "finished_at": last_finished["finished_at"]}
        return result

    def _row_view(self, row: sqlite3.Row, kind: SourceKind,
                  path: tuple[str, ...]) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        return {"citation": make_handle(kind, path, self.generation, row["capture_id"]),
                "kind": kind.name, "audience_key": row["unit"], "key": kind.key(path),
                "row": payload["row"], "meta": payload.get("meta", {}),
                "source_version": row["source_version"], "digest": row["digest"],
                "lineage": row["lineage"], "recorded_at": row["recorded_at"],
                "superseded_at": row["superseded_at"],
                "missing_from_source_since": row["missing_from_source_since"]}

    def resolve(self, handle: str, actor: Any) -> dict[str, Any]:
        """The exact captured version a handle names, offline, or a typed error."""
        return self.resolve_many([handle], actor)["results"][0]

    def resolve_many(self, handles: Sequence[str], actor: Any) -> dict[str, Any]:
        """Resolve many handles offline; visibility is checked per record before reading."""
        if isinstance(handles, (str, bytes)) or not isinstance(handles, Sequence) \
                or len(handles) > MAX_BATCH_HANDLES:
            raise CaptureError("invalid_handles",
                               f"handles must be a list of at most {MAX_BATCH_HANDLES}")
        results: list[dict[str, Any]] = []
        try:
            self._check_binding()
            binding = None
        except CaptureError as exc:
            binding = exc
        for handle in handles:
            results.append(self._resolve_one(handle, actor, binding))
        return {"schema": "plx-capture-resolve-batch/v1", "count": len(results),
                "ok_count": sum(1 for r in results if r.get("ok")), "results": results,
                "atomic_upstream_snapshot": False, "deletions_proven": False}

    def _resolve_one(self, handle: Any, actor: Any,
                     binding: CaptureError | None) -> dict[str, Any]:
        try:
            kind, path, generation, capture_id = parse_handle(handle)
        except ValueError:
            return {"ok": False, "error": "invalid_handle", "citation": handle,
                    "message": "expected <scheme>://<path>@<generation>-<capture-id>"}
        resources = kind.resources(path)  # before any row is read: the audience only
        if not resources or not all(self.audience.allows(actor, r) for r in resources):
            return {"ok": False, "error": "not_visible", "citation": handle,
                    "message": "the cited record is not visible to this actor"}
        if binding is not None:
            return {"ok": False, "error": binding.code, "citation": handle,
                    "message": str(binding)}
        path_text = _path_text(path)
        # A reader who may see no version of this path learns nothing about it: a record
        # hidden from it by its own ACL and a path never captured answer alike, never a
        # confident absence (``not_visible``).
        hidden = {"ok": False, "error": "not_visible", "citation": handle,
                  "message": "the cited record is not visible to this actor"}
        if not self._path_visible(actor, kind, path, path_text):
            return hidden
        if generation != self.generation:
            return {"ok": False, "error": "capture_unknown",
                    "reason": "store_generation_mismatch", "citation": handle,
                    "store_generation": self.generation,
                    "message": "the handle was minted by another capture store generation"}
        row = self._db.execute("SELECT * FROM versions WHERE capture_id = ? AND kind = ? "
                               "AND path = ?", (capture_id, kind.name, path_text)).fetchone()
        if row is not None and not self._visible(actor, kind, path, row):
            return hidden
        if row is None and self._db.execute(
                "SELECT 1 FROM pruned WHERE capture_id = ? AND kind = ? AND path = ?",
                (capture_id, kind.name, path_text)).fetchone():
            return {"ok": False, "error": "capture_unknown", "reason": "pruned_by_retention",
                    "citation": handle,
                    "message": "this superseded version was dropped by the retention bound"}
        if row is None:
            return {"ok": False, "error": "capture_unknown", "citation": handle,
                    "message": "the record exists but no version has this capture id"}
        view = self._row_view(row, kind, path)
        superseded = row["superseded_at"] is not None
        current = row if not superseded else self._db.execute(
            "SELECT * FROM versions WHERE kind = ? AND path = ? AND superseded_at IS NULL",
            (kind.name, path_text)).fetchone()
        if current is not None and not self._visible(actor, kind, path, current):
            current = None  # a current version hidden from this reader is not named
        result = {"ok": True, "status": "superseded" if superseded else "current",
                  "changed_since": superseded, **view, "citation": handle,
                  "capture": {"capture_id": row["capture_id"], "run_id": row["run_id"],
                              "recorded_at": row["recorded_at"],
                              "superseded_at": row["superseded_at"],
                              "superseded_by": row["superseded_by"],
                              "last_seen_at": row["last_seen_at"]},
                  "store_generation": self.generation,
                  "current_citation": None if current is None else make_handle(
                      kind, path, self.generation, current["capture_id"]),
                  "atomic_upstream_snapshot": False, "deletions_proven": False}
        if superseded and current is not None:
            result["current_source_version"] = current["source_version"]
        return result

    def records(self, actor: Any, kind_name: str, unit: str, *,
                as_known_at: str | None = None, include_missing: bool = False,
                after_path: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Captured records of one unit as known at ``as_known_at`` (default: now), in
        path order, keyset-paged by ``after_path``. Each record is checked against the
        actor before it is returned or counted."""
        kind = source_kind(kind_name)
        self._require_unit(actor, kind_name, unit)
        if isinstance(limit, bool) or not isinstance(limit, int) \
                or not 1 <= limit <= MAX_RECORDS_LIMIT:
            raise CaptureError("limit_invalid", f"limit must be 1..{MAX_RECORDS_LIMIT}")
        params: list[Any] = [kind.name, unit]
        if as_known_at is None:
            where = "superseded_at IS NULL"
            if not include_missing:
                where += " AND missing_from_source_since IS NULL"
        else:
            known = _known_at(as_known_at)
            where = "recorded_at <= ? AND (superseded_at IS NULL OR superseded_at > ?)"
            params += [known, known]
        if after_path is not None:
            where += " AND path > ?"
            params.append(str(after_path))
        items, withheld, last = [], 0, None
        for row in self._db.execute(f"SELECT * FROM versions WHERE kind = ? AND unit = ? "
                                    f"AND {where} ORDER BY path", params):
            path = kind.canonical_path([unquote(p) for p in row["path"].split("/")])
            if not self._visible(actor, kind, path, row):
                withheld += 1
                continue
            if len(items) >= limit:
                break
            items.append(self._row_view(row, kind, path))
            last = row["path"]
        more = len(items) >= limit
        return {"unit": unit, "items": items, "next_after_path": last if more else None,
                "withheld_count": withheld, "as_known_at": as_known_at,
                "atomic_upstream_snapshot": False, "deletions_proven": False}

    def claim_rows(self, actor: Any) -> Callable[[str], Mapping[str, Any] | None]:
        """``operating.claim_checks`` ``rows`` callable over this store for ``actor``:
        handle -> ``{"citation", "row", "key", ...}`` or None (unreadable)."""
        def rows(handle: str) -> Mapping[str, Any] | None:
            result = self.resolve(handle, actor)
            if not result.get("ok"):
                return None
            return {k: result[k] for k in ("citation", "row", "key", "kind", "lineage",
                                           "digest", "source_version", "status")}
        return rows


def _known_at(value: Any) -> str:
    """A UTC ISO time (``...Z``) in the store's fixed-width form, comparable as text."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CaptureError("as_known_at_invalid", "UTC ISO time ending in Z")
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise CaptureError("as_known_at_invalid", "UTC ISO time ending in Z") from None
    return _iso(moment)


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# A ``versions`` row's closed removal stretches (since, until, basis), selected beside
# it (``SELECT *, REMOVALS_COLUMN FROM versions``) and read by :func:`removals_of`.
REMOVALS_COLUMN = ("(SELECT json_group_array(json_array(r.since, r.until, "
                   "'missing_from_source')) FROM removals r WHERE r.kind = versions.kind "
                   "AND r.path = versions.path) AS removals")


def removals_of(row: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """The removal stretches of a row selected with :data:`REMOVALS_COLUMN`."""
    return tuple(tuple(item) for item in json.loads(row["removals"] or "[]"))


__all__ = ["ADAPTER_MODULES", "BC_FIELD_PAGES", "BC_KIND", "BC_LINE_HEADERS", "CaptureError", "CaptureStore", "CapturedRecord",
           "FORMAT", "SourceKind", "bc_unit", "is_capture_handle", "lineage_ok",
           "make_handle", "parse_handle", "register_source_kind", "removals_of",
           "REMOVALS_COLUMN", "source_kind", "source_kinds", "sqlite_bytes", "wal_journal"]
